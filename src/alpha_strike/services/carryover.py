"""クローズ後着の SIMULATE シグナルを次の市場オープンで自動約定させる carry-over (#89)。

背景: 実機検証で確定したとおり moomoo SIMULATE は GTC を約定させられず（OpenD 10.5 は
SUBMITTED 受理するが翌寄付で約定せず・OpenD 10.7 は発注時点で拒否）、DAY もクローズ後は
失効する。TradingView 日足アラート(Once Per Bar Close)は 16:00 ET = クローズ後に届くため、
paper では post-close シグナルが一切約定しない。

本モジュールは app 層で carry-over をエミュレートする:
  A) 受信時: SIMULATE × moomoo × US 系のクローズ後シグナルは broker へ投げず
     ``SignalCarryoverQueuedEvent``(queued) を永続化する（webhook_server から呼ぶ）。
  B) 再発注ループ: pending_reconcile (#79) と同型の常駐 asyncio タスクで、市場オープン時に
     未解消 intent を ``order_router.route`` 経由で再発注する（sell_guard #74 / target_reconcile
     #80 / order_reconcile #57 / pending_reconcile #79 を継承）。

スコープ: SIMULATE 限定。REAL は GTC carry-over が効くため対象外（二重ポジ防止）。
解消判定: append-only JSONL 上で、queued intent ``X`` に対し ``f"{X}_co"`` の order_recorded
(accepted/skipped) が現れたら解消とみなす（last-wins）。再発注上限超過 / stale は abandoned 化。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from alpha_strike.models import OrderEvent, SignalCarryoverQueuedEvent, WebhookPayload
from alpha_strike.services.fill_service import _generate_id
from alpha_strike.services.idempotency import IdempotencyKey
from alpha_strike.services.market_state import is_market_open, market_open_map
from alpha_strike.services.order_reconcile import reconcile_order_once
from alpha_strike.services.sell_guard import is_sell_guard_enabled, resolve_sell_quantity
from alpha_strike.services.target_reconcile import (
    is_target_reconcile_enabled,
    resolve_target_order,
)

logger = logging.getLogger(__name__)

_ENABLED_ENV_VAR = "CARRYOVER_ENABLED"
_INTERVAL_ENV_VAR = "CARRYOVER_RESUBMIT_INTERVAL_SECONDS"
_LOOKBACK_ENV_VAR = "CARRYOVER_LOOKBACK_HOURS"
_MAX_RESUBMITS_ENV_VAR = "CARRYOVER_MAX_RESUBMITS"
_TRUTHY = {"1", "true", "yes", "on"}

DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_LOOKBACK_HOURS = 48.0
DEFAULT_MAX_RESUBMITS = 3
# queued イベントの走査上限（lookback_hours と二重の安全弁）。日次シグナルは数十件/日
# 程度のため 500 で 10 日分以上をカバーする。
_SCAN_LIMIT = 500
# 解消判定に使う order_recorded の走査上限。queued より大きめに取り、バックテスト等で
# 一時的に発注イベントが急増しても古い accepted(=解消マーカー)を取りこぼさないようにする。
_ORDER_SCAN_LIMIT = 2000
# carry-over 再発注の signal_id サフィックス。ユーザー signal_id との衝突（偽解消）を
# 避けるため衝突しにくい語にする。
_CO_SUFFIX = "__carryover"

# moomoo_handler._MARKET_MAP と同じく US 以外（HK / CRYPTO）を判別する。
# US / INDEX / COMMODITY / FX は US 市場扱い。
_NON_US_ASSET_CLASSES = {"HK", "CRYPTO"}


def is_carryover_enabled() -> bool:
    """carry-over エミュレーションの有効可否（既定 ON）。SIMULATE 限定は呼び出し側で判定。"""
    return os.getenv(_ENABLED_ENV_VAR, "1").strip().lower() in _TRUTHY


def get_carryover_interval() -> float:
    """再発注スイープ間隔（秒）。既定 300 秒。不正値は既定。"""
    return _float_env(_INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS)


def get_carryover_lookback_hours() -> float:
    """queued intent の有効期限（時間）。既定 48h（週末またぎをカバー）。不正値は既定。"""
    return _float_env(_LOOKBACK_ENV_VAR, DEFAULT_LOOKBACK_HOURS)


def get_carryover_max_resubmits() -> int:
    """同一 intent の再発注上限。既定 3。超過で abandoned 化。不正値は既定。"""
    raw = os.getenv(_MAX_RESUBMITS_ENV_VAR, str(DEFAULT_MAX_RESUBMITS))
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning("%s が数値でないため既定 %s を使用", _MAX_RESUBMITS_ENV_VAR, DEFAULT_MAX_RESUBMITS)
        return DEFAULT_MAX_RESUBMITS
    return value if value > 0 else DEFAULT_MAX_RESUBMITS


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s が数値でないため既定 %s を使用", name, default)
        return default
    return value if value > 0 else default


def _maps_to_us(asset_class: str) -> bool:
    """asset_class が US 市場（carry-over 対象）にマップされるか。"""
    return asset_class.upper() not in _NON_US_ASSET_CLASSES


def _co_signal_id(signal_id: str) -> str:
    return f"{signal_id}{_CO_SUFFIX}"


def _intent_key(event: dict) -> tuple[str, str]:
    """carry-over intent の識別単位 ``(signal_id, ticker)`` (#126)。

    signal_id は bar 単位で払い出され、同一バーの銘柄別シグナルが同じ値を共有するため、
    単独では識別できない（1 銘柄の発注で他銘柄まで解消済みと誤判定する）。

    ``action`` を含めないのは、発注時に target_qty closed-loop (#80) や over-sell ガード
    (#74) が方向を反転させうるため。queued が buy でも order_recorded は sell になり得る
    ので、action を照合に使うと解消判定が成立せず再発注が繰り返される。
    """
    return (str(event.get("signal_id", "")), str(event.get("ticker", "")))


def _co_key(key: tuple[str, str]) -> tuple[str, str]:
    """queued 側の識別キーを、対応する carry-over 発注側のキーへ変換する。"""
    signal_id, ticker = key
    return (_co_signal_id(signal_id), ticker)


def _weekend_hours_between(start: datetime, end: datetime) -> float:
    """``start``〜``end`` に含まれる土日(市場休場)の時間数を返す。

    carry-over の lookback を「市場が動いている実効時間」で測るために使う。金曜
    クローズ後シグナルは翌月曜寄付まで暦では 48h を超えるが、その大半は土日(市場
    休場)で実効では数時間しか経っていない。土日を除外しないと週末跨ぎシグナルが
    寄付前に stale 判定され取りこぼされる。祝日は考慮しない(YAGNI。必要なら
    market_state 連携で拡張)。
    """
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        day_end = min(
            end,
            (cur + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
        )
        if cur.weekday() >= 5:  # 5=土, 6=日
            total += (day_end - cur).total_seconds() / 3600.0
        cur = day_end
    return total


def should_carryover(
    payload: WebhookPayload,
    market_state_provider: Any,
    *,
    trd_env: str,
) -> bool:
    """このシグナルを carry-over キューへ入れるべきか（受信時判定）。

    True の条件をすべて満たすとき:
      - ``CARRYOVER_ENABLED`` が真
      - broker == moomoo
      - ``trd_env`` == SIMULATE（REAL は GTC carry-over があるため対象外）
      - asset_class が US 市場にマップされる（HK / CRYPTO は対象外）
      - 市場が **明確にクローズ中**（``is_market_open`` が False）

    市場状態が判定不能（``None``）の場合は False を返す＝従来どおり即発注に倒す
    （誤って carry-over キューへ入れて発注機会を逃すより、通常フローに委ねる方が安全）。
    """
    if not is_carryover_enabled():
        return False
    if payload.broker != "moomoo":
        return False
    if trd_env.upper() != "SIMULATE":
        return False
    if not _maps_to_us(payload.asset_class):
        return False
    return is_market_open(market_state_provider, payload.ticker) is False


def build_carryover_queued_event(
    payload: WebhookPayload, signal_id: str
) -> SignalCarryoverQueuedEvent:
    """受信したシグナルを保留する queued イベントを構築する。"""
    return SignalCarryoverQueuedEvent(
        event_id=_generate_id("evt"),
        signal_id=signal_id,
        occurred_at=datetime.now(),
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        target_qty=payload.target_qty,
        carryover_state="queued",
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        timeframe=payload.timeframe,
        alert_timestamp=payload.alert_timestamp,
        run_mode=payload.run_mode,
        alert_name=payload.alert_name,
        portfolio_id=payload.portfolio_id,
        sub_strategy_id=payload.sub_strategy_id,
    )


def find_carryover_intents(
    event_logger: Any,
    *,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    max_resubmits: int = DEFAULT_MAX_RESUBMITS,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """未解消の carry-over intent を返す。

    Returns:
        ``(to_resubmit, to_abandon)``。
        - ``to_resubmit``: 受信時刻 ASC（古い順）の未解消 intent。delta シグナルの順序を
          保つため受信順に再発注する。
        - ``to_abandon``: 再発注上限超過 / stale（実効 lookback 超＝土日除外）で打ち切る intent。

    解消判定（append-only・last-wins）。判定単位は ``(signal_id, ticker, action)`` で、
    signal_id 単独ではない。signal_id は bar 単位で払い出されるため、同一バーの銘柄別
    シグナルが同じ値を共有し、単独をキーにすると 1 銘柄の発注で他銘柄まで解消済みと
    誤判定して取りこぼす (#126):
      - queued ``X`` に対し ``f"{X}_co"`` の order_recorded が accepted/skipped → 解消（除外）
      - ``f"{X}_co"`` の order_recorded(failed) 件数 ≥ ``max_resubmits`` → abandon
      - 既に carryover_state=abandoned の queued がある → 除外
    """
    queued = event_logger.load_events(
        broker="moomoo", event_type="signal_carryover_queued", limit=_SCAN_LIMIT
    )
    orders = event_logger.load_events(
        broker="moomoo", event_type="order_recorded", limit=_ORDER_SCAN_LIMIT
    )

    resolved_co: set[tuple[str, str]] = set()
    failed_co: dict[tuple[str, str], int] = {}
    for oe in orders:
        key = _intent_key(oe)
        status = oe.get("status")
        if status in ("accepted", "skipped"):
            resolved_co.add(key)
        elif status == "failed":
            failed_co[key] = failed_co.get(key, 0) + 1

    abandoned: set[tuple[str, str]] = {
        _intent_key(e) for e in queued if e.get("carryover_state") == "abandoned"
    }

    current = now if now is not None else datetime.now()
    seen: set[tuple[str, str]] = set()
    to_resubmit: list[dict] = []
    to_abandon: list[dict] = []
    for e in queued:  # 新しい順
        if e.get("carryover_state") != "queued":
            continue
        sid = str(e.get("signal_id", ""))
        key = _intent_key(e)
        if not sid or key in seen:
            continue
        seen.add(key)
        if key in abandoned:
            continue
        co = _co_key(key)
        if co in resolved_co:
            continue  # 再発注済み（accepted）/ skip 済み → 解消
        try:
            occurred = datetime.fromisoformat(str(e.get("occurred_at")))
        except (TypeError, ValueError):
            continue
        # lookback は「市場が動いている実効時間」で測る。暦時間で測ると金曜クローズ後
        # シグナルが土日(市場休場)だけで 48h を超え、月曜寄付前に stale 化して取りこぼす。
        elapsed_hours = (current - occurred).total_seconds() / 3600.0
        effective_hours = elapsed_hours - _weekend_hours_between(occurred, current)
        if effective_hours > lookback_hours:
            to_abandon.append(e)  # stale: 実効 lookback(土日除外) 超は打ち切り
            continue
        if failed_co.get(co, 0) >= max_resubmits:
            to_abandon.append(e)  # 再発注上限超過
            continue
        to_resubmit.append(e)

    to_resubmit.sort(key=lambda e: str(e.get("occurred_at")))
    return to_resubmit, to_abandon


def _append_abandoned(event_logger: Any, intent: dict) -> None:
    """intent を abandoned としてマークする（以後 find が拾わない）。"""
    try:
        event_logger.append(
            SignalCarryoverQueuedEvent(
                event_id=_generate_id("evt"),
                signal_id=str(intent.get("signal_id", "")),
                occurred_at=datetime.now(),
                broker=intent.get("broker", "moomoo"),
                asset_class=intent.get("asset_class", "US"),
                action=intent.get("action", "buy"),
                ticker=str(intent.get("ticker", "")),
                quantity=float(intent.get("quantity") or 0.0),
                target_qty=intent.get("target_qty"),
                carryover_state="abandoned",
                strategy_id=intent.get("strategy_id"),
                strategy_version=intent.get("strategy_version"),
                snapshot_id=intent.get("snapshot_id"),
                timeframe=intent.get("timeframe"),
                run_mode=intent.get("run_mode", "paper"),
                alert_name=intent.get("alert_name"),
                portfolio_id=intent.get("portfolio_id"),
                sub_strategy_id=intent.get("sub_strategy_id"),
            )
        )
        logger.info(
            "carryover abandon: %s %s qty=%s (signal_id=%s)",
            intent.get("ticker"),
            intent.get("action"),
            intent.get("quantity"),
            intent.get("signal_id"),
        )
    except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を止めない
        logger.warning("carryover abandon の記録に失敗: %s", exc)


def _reconstruct_payload(intent: dict) -> WebhookPayload:
    """queued intent から発注用 WebhookPayload を再構築する。

    ``signal_id`` は付けない（route()/handler は参照せず、イベント記録には co_signal_id を
    明示で渡すため。WebhookPayload.signal_id の長さ制約も回避する）。passphrase は
    route()/handler が参照しないためダミー。
    """
    return WebhookPayload(
        passphrase="carryover",
        broker=intent.get("broker", "moomoo"),
        asset_class=intent.get("asset_class", "US"),
        action=intent.get("action", "buy"),
        ticker=str(intent.get("ticker", "")),
        quantity=float(intent.get("quantity") or 0.0),
        target_qty=intent.get("target_qty"),
        strategy_id=intent.get("strategy_id"),
        strategy_version=intent.get("strategy_version"),
        snapshot_id=intent.get("snapshot_id"),
        timeframe=intent.get("timeframe"),
        alert_timestamp=intent.get("alert_timestamp"),
        run_mode=intent.get("run_mode", "paper"),
        alert_name=intent.get("alert_name"),
        portfolio_id=intent.get("portfolio_id"),
        sub_strategy_id=intent.get("sub_strategy_id"),
    )


def _record_order(
    event_logger: Any,
    payload: WebhookPayload,
    *,
    signal_id: str,
    internal_order_id: str,
    status: str,
    broker_order_id: str | None = None,
    error_type: str | None = None,
) -> None:
    """carry-over 再発注の order_recorded を記録する（signal_id は co_signal_id）。"""
    try:
        event_logger.append(
            OrderEvent(
                event_id=_generate_id("evt"),
                signal_id=signal_id,
                order_id=internal_order_id,
                occurred_at=datetime.now(),
                broker=payload.broker,
                asset_class=payload.asset_class,
                action=payload.action,
                ticker=payload.ticker,
                quantity=payload.quantity,
                status=status,  # type: ignore[arg-type]
                broker_order_id=broker_order_id,
                strategy_id=payload.strategy_id,
                strategy_version=payload.strategy_version,
                snapshot_id=payload.snapshot_id,
                run_mode=payload.run_mode,
                error_type=error_type,
                portfolio_id=payload.portfolio_id,
                sub_strategy_id=payload.sub_strategy_id,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("carryover order_recorded の記録に失敗: %s", exc)


def _resubmit_intent(
    intent: dict,
    *,
    status_provider: Any,
    event_logger: Any,
    order_router: Any,
    fill_service: Any,
    idempotency: Any = None,
    notifier: Any = None,
) -> bool:
    """1 件の carry-over intent をオープン時に再発注する。

    既存ガードを継承: target_qty 再解決 (#80) → over-sell ガード (#74) → route → events →
    同期 reconcile（権威 order_reconciled をシード。残りの約定追跡は #79 に委譲）。

    Returns:
        intent を解消できたら True（accepted または skip）。dup / failed は False。
    """
    orig_signal_id = str(intent.get("signal_id", ""))
    co_id = _co_signal_id(orig_signal_id)
    # 冪等キーは銘柄まで含める。同一バーの銘柄別 intent は co_id が同じになるため、
    # co_id 単独では 2 銘柄目以降が二重発注扱いで弾かれる (#126)。
    idem_key = IdempotencyKey(
        signal_id=co_id,
        broker=str(intent.get("broker", "moomoo")),
        ticker=str(intent.get("ticker", "")),
    )

    # 同一プロセス内の二重発注防止（永続的な解消判定は order_recorded(co) の有無）
    if idempotency is not None and not idempotency.check_and_record(idem_key):
        return False

    payload = _reconstruct_payload(intent)
    internal_order_id = _generate_id("ord")

    # target_qty 再解決（#80 を継承）。fail-open（判定不能なら従来 delta で発注継続）。
    if (
        payload.target_qty is not None
        and is_target_reconcile_enabled()
        and payload.broker == "moomoo"
    ):
        try:
            decision = resolve_target_order(payload, status_provider)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("carryover target reconcile 失敗 (fail-open): %s", exc)
        else:
            if decision.action == "skip":
                _record_order(
                    event_logger, payload, signal_id=co_id,
                    internal_order_id=internal_order_id, status="skipped",
                )
                return True  # target 到達済み → 解消
            payload = payload.model_copy(
                update={"action": decision.side, "quantity": decision.quantity}
            )

    # over-sell ガード（#74 を継承）。fail-open。
    if (
        is_sell_guard_enabled()
        and payload.broker == "moomoo"
        and payload.action == "sell"
    ):
        try:
            decision = resolve_sell_quantity(payload, status_provider)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("carryover sell guard 失敗 (fail-open): %s", exc)
        else:
            if decision.action == "skip":
                _record_order(
                    event_logger, payload, signal_id=co_id,
                    internal_order_id=internal_order_id, status="skipped",
                )
                return True  # 建玉なし → 解消
            if decision.action == "clamp":
                payload = payload.model_copy(update={"quantity": decision.quantity})

    try:
        result = order_router.route(payload)
    except Exception as exc:  # noqa: BLE001 — 失敗を記録して継続（max_resubmits で打ち切り）
        logger.warning(
            "carryover 再発注に失敗: %s %s: %s", payload.ticker, payload.action, exc
        )
        # 失敗した試行は注文を出していない。冪等キーを解放して次スイープで即リトライ
        # 可能にする（TTL 満了まで ~30 分リトライが止まり、failed カウントも進まないのを防ぐ）。
        if idempotency is not None:
            idempotency.forget(idem_key)
        _record_order(
            event_logger, payload, signal_id=co_id,
            internal_order_id=internal_order_id, status="failed",
            error_type=type(exc).__name__,
        )
        return False

    _oid = result.get("order_id") if isinstance(result, dict) else None
    broker_order_id = str(_oid) if _oid is not None else None
    _record_order(
        event_logger, payload, signal_id=co_id,
        internal_order_id=internal_order_id, status="accepted",
        broker_order_id=broker_order_id,
    )

    fill_event = fill_service.build(
        payload=payload,
        result=result if isinstance(result, dict) else {},
        signal_id=co_id,
        internal_order_id=internal_order_id,
        broker_order_id=broker_order_id,
    )
    if fill_event is not None:
        for allocated in fill_service.allocate(fill_event):
            event_logger.append(allocated)
            trade_closed = fill_service.build_trade_closed(allocated)
            if trade_closed is not None:
                event_logger.append(trade_closed)

    # 権威 order_reconciled を同期でシード（残りの約定追跡は #79 pending_reconcile に委譲）
    if broker_order_id and status_provider is not None:
        reconcile_order_once(
            provider=status_provider,
            event_logger=event_logger,
            notifier=notifier,
            broker_order_id=broker_order_id,
            signal_id=co_id,
            order_id=internal_order_id,
            broker=payload.broker,
            asset_class=payload.asset_class,
            ticker=payload.ticker,
            action=payload.action,
            quantity=payload.quantity,
            strategy_id=payload.strategy_id,
            strategy_version=payload.strategy_version,
            snapshot_id=payload.snapshot_id,
            run_mode=payload.run_mode,
            portfolio_id=payload.portfolio_id,
            sub_strategy_id=payload.sub_strategy_id,
        )

    logger.info(
        "carryover 再発注: %s %s qty=%s (co_signal_id=%s broker_order_id=%s)",
        payload.ticker,
        payload.action,
        payload.quantity,
        co_id,
        broker_order_id,
    )
    return True


def run_carryover_resubmit_once(
    *,
    market_state_provider: Any,
    status_provider: Any,
    event_logger: Any,
    order_router: Any,
    fill_service: Any,
    idempotency: Any = None,
    notifier: Any = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    max_resubmits: int = DEFAULT_MAX_RESUBMITS,
) -> int:
    """未解消の carry-over intent を 1 スイープ処理する。

    Returns:
        再発注（または skip で解消）した intent 件数。

    abandon 対象（上限超過 / stale）はマークし、未解消 intent は **市場オープン中の
    ものだけ** 受信順に再発注する。未解消ゼロなら OpenD（market state）へ問い合わせない。
    """
    to_resubmit, to_abandon = find_carryover_intents(
        event_logger, lookback_hours=lookback_hours, max_resubmits=max_resubmits
    )
    for intent in to_abandon:
        _append_abandoned(event_logger, intent)

    if not to_resubmit:
        return 0

    # 市場オープン判定はスイープ先頭で 1 回だけ（intent 件数分の OpenD 接続を避ける）。
    open_map = market_open_map(
        market_state_provider, [str(i.get("ticker", "")) for i in to_resubmit]
    )
    resubmitted = 0
    for intent in to_resubmit:
        ticker = str(intent.get("ticker", ""))
        if not open_map.get(ticker, False):
            continue  # まだオープンしていない / 判定不能 → 次スイープで再試行
        if _resubmit_intent(
            intent,
            status_provider=status_provider,
            event_logger=event_logger,
            order_router=order_router,
            fill_service=fill_service,
            idempotency=idempotency,
            notifier=notifier,
        ):
            resubmitted += 1
    return resubmitted


async def carryover_resubmit_loop(
    *,
    market_state_provider: Any,
    status_provider: Any,
    event_logger: Any,
    order_router: Any,
    fill_service: Any,
    idempotency: Any = None,
    notifier: Any = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    max_resubmits: int = DEFAULT_MAX_RESUBMITS,
) -> None:
    """carry-over 再発注の常駐ループ。lifespan の background task として起動する。

    起動直後に 1 回実行（サーバー停止中にオープンが来た取りこぼしを回収）し、以後
    ``interval_seconds`` ごとに繰り返す。例外はログに残して継続し、``CancelledError``
    （shutdown）でのみ終了する（pending_reconcile_loop と同型）。
    """
    while True:
        try:
            resubmitted = await asyncio.to_thread(
                run_carryover_resubmit_once,
                market_state_provider=market_state_provider,
                status_provider=status_provider,
                event_logger=event_logger,
                order_router=order_router,
                fill_service=fill_service,
                idempotency=idempotency,
                notifier=notifier,
                lookback_hours=lookback_hours,
                max_resubmits=max_resubmits,
            )
            if resubmitted:
                logger.info("carryover: %d 件を再発注", resubmitted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ループは止めない
            logger.warning("carryover loop でエラー: %s", exc)
        await asyncio.sleep(interval_seconds)
