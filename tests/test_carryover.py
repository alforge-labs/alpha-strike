"""carry-over エミュレーション (#89) のユニットテスト。

WHY: moomoo SIMULATE は GTC/DAY のどちらでも post-close シグナルを翌寄付に約定させ
られない（実機検証で確定）。本機能が「クローズ後にキュー → オープンで受信順に再発注 →
解消」を、二重発注なく・over-sell なく・stale を出さず・REAL を変えずに行うことを固定する。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from alpha_strike.event_logger import JsonlEventLogger
from alpha_strike.models import OrderEvent, SignalCarryoverQueuedEvent, WebhookPayload
from alpha_strike.services.carryover import (
    DEFAULT_MAX_RESUBMITS,
    _co_signal_id,
    build_carryover_queued_event,
    carryover_resubmit_loop,
    find_carryover_intents,
    run_carryover_resubmit_once,
    should_carryover,
)
from alpha_strike.services.fill_service import FillEventService
from alpha_strike.services.idempotency import IdempotencyStore
from alpha_strike.services.status_service import (
    AccountStatus,
    AccountSummary,
    OrderRecord,
    PositionRecord,
)


# --- fakes ---------------------------------------------------------------


class _FakeMarketState:
    """code -> MarketState を返す Fake。呼び出し回数を記録する。"""

    def __init__(self, states: dict[str, str] | None = None, *, raises: bool = False):
        self._states = states or {}
        self.raises = raises
        self.calls = 0

    def get_market_state(self, codes: list[str]) -> dict[str, str]:
        self.calls += 1
        if self.raises:
            raise RuntimeError("OpenD quote 障害")
        return {c: self._states[c] for c in codes if c in self._states}


class _FakeRouter:
    """route 呼び出しを記録する Fake。"""

    def __init__(self, *, result: dict | None = None, raises: bool = False):
        self.result = result if result is not None else {"order_id": "br_999", "filled_qty": 0.0, "filled_price": 0.0}
        self.raises = raises
        self.calls: list[WebhookPayload] = []

    def route(self, payload: WebhookPayload) -> dict:
        self.calls.append(payload)
        if self.raises:
            raise RuntimeError("broker 拒否")
        return dict(self.result)


def _status_provider(
    *, positions: list[PositionRecord] | None = None, orders: list[OrderRecord] | None = None
):
    class _P:
        def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
            return AccountStatus(
                broker="moomoo",
                trd_env="SIMULATE",
                account=AccountSummary(),
                positions=positions or [],
                recent_orders=orders or [],
            )

    return _P()


def _payload(action="buy", ticker="US.TLT", qty=2.0, asset_class="US", broker="moomoo", target_qty=None):
    return WebhookPayload(
        passphrase="x",
        broker=broker,
        asset_class=asset_class,
        action=action,
        ticker=ticker,
        quantity=qty,
        target_qty=target_qty,
        strategy_id="beat_qqq_hedged_v1",
        run_mode="paper",
        signal_id="20260608-093000",
    )


def _seed_queued(logger, signal_id, *, ticker="US.TLT", action="buy", qty=2.0, occurred_at=None, state="queued", target_qty=None):
    logger.append(
        SignalCarryoverQueuedEvent(
            event_id=f"evt_{signal_id}",
            signal_id=signal_id,
            occurred_at=occurred_at or datetime.now(),
            broker="moomoo",
            asset_class="US",
            action=action,
            ticker=ticker,
            quantity=qty,
            target_qty=target_qty,
            carryover_state=state,
            strategy_id="beat_qqq_hedged_v1",
            run_mode="paper",
        )
    )


def _seed_order_recorded(logger, co_signal_id, *, status="accepted", ticker="US.TLT"):
    logger.append(
        OrderEvent(
            event_id=f"evt_o_{co_signal_id}_{status}",
            signal_id=co_signal_id,
            order_id=f"ord_{co_signal_id}",
            occurred_at=datetime.now(),
            broker="moomoo",
            asset_class="US",
            action="buy",
            ticker=ticker,
            quantity=2.0,
            status=status,
            broker_order_id="br_1" if status == "accepted" else None,
            run_mode="paper",
        )
    )


# --- should_carryover ----------------------------------------------------


def test_should_carryover_simulate_closed_true(monkeypatch):
    """SIMULATE × moomoo × US × クローズ後 → carry-over する。これが効かないと
    post-close シグナルが永久に約定しない（#89 の核心症状）。"""
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    ms = _FakeMarketState({"US.TLT": "CLOSED"})
    assert should_carryover(_payload(), ms, trd_env="SIMULATE") is True


def test_should_carryover_market_open_false(monkeypatch):
    """開場中(AFTERNOON)は即時 DAY 約定するため carry-over しない（不要な遅延/二重発注の温床）。"""
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    assert should_carryover(_payload(), ms, trd_env="SIMULATE") is False


def test_should_carryover_real_false(monkeypatch):
    """REAL は GTC carry-over が効くため対象外。二重適用すると GTC+DAY で二重ポジになる。"""
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    ms = _FakeMarketState({"US.TLT": "CLOSED"})
    assert should_carryover(_payload(), ms, trd_env="REAL") is False


@pytest.mark.parametrize("ac,ticker", [("HK", "HK.00700"), ("CRYPTO", "CC.BTC")])
def test_should_carryover_non_us_false(monkeypatch, ac, ticker):
    """HK は当日のみ・CRYPTO は 24/365 でクローズ概念が無く、US 専用ロジックを誤適用しない。"""
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    ms = _FakeMarketState({ticker: "CLOSED"})
    assert should_carryover(_payload(asset_class=ac, ticker=ticker), ms, trd_env="SIMULATE") is False


def test_should_carryover_disabled_false(monkeypatch):
    """CARRYOVER_ENABLED=0 で完全に無効化できる（運用安全弁）。"""
    monkeypatch.setenv("CARRYOVER_ENABLED", "0")
    ms = _FakeMarketState({"US.TLT": "CLOSED"})
    assert should_carryover(_payload(), ms, trd_env="SIMULATE") is False


def test_should_carryover_market_unknown_false(monkeypatch):
    """市場状態が判定不能なら従来どおり即発注に倒す（誤キューで発注機会を逃さない）。"""
    monkeypatch.delenv("CARRYOVER_ENABLED", raising=False)
    ms = _FakeMarketState(raises=True)
    assert should_carryover(_payload(), ms, trd_env="SIMULATE") is False


def test_build_queued_event_carries_metadata():
    """queued イベントは原シグナルのメタ（target_qty/strategy 等）を保持する。"""
    ev = build_carryover_queued_event(_payload(target_qty=3.0), "sig_1")
    assert ev.carryover_state == "queued"
    assert ev.target_qty == 3.0
    assert ev.ticker == "US.TLT"
    assert ev.run_mode == "paper"


# --- find_carryover_intents ----------------------------------------------


def test_find_unresolved_intent(tmp_path):
    """order_recorded(co) が無い queued は未解消＝再発注対象。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    to_resubmit, to_abandon = find_carryover_intents(logger)
    assert [e["signal_id"] for e in to_resubmit] == ["20260608-093000"]
    assert to_abandon == []


def test_find_resolved_intent_excluded(tmp_path):
    """co の order_recorded(accepted) があれば解消済み＝再発注しない（二重発注防止の要）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    _seed_order_recorded(logger, _co_signal_id("20260608-093000"), status="accepted")
    to_resubmit, _ = find_carryover_intents(logger)
    assert to_resubmit == []


def test_find_skipped_intent_excluded(tmp_path):
    """co の order_recorded(skipped)（target 到達/建玉なし）も解消とみなす。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    _seed_order_recorded(logger, _co_signal_id("20260608-093000"), status="skipped")
    to_resubmit, _ = find_carryover_intents(logger)
    assert to_resubmit == []


def test_find_stale_intent_abandoned(tmp_path):
    """実効 lookback(48h、土日除外) 超の queued は stale として打ち切り。古い intent を
    突然約定させない。now を固定し平日のみで 50h 経過させる(土日非依存の決定論テスト)。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    now = datetime(2026, 6, 17, 10, 0)  # 水曜
    old = now - timedelta(hours=50)  # 月曜 08:00（土日を挟まず実効 50h）
    _seed_queued(logger, "old-sig", occurred_at=old)
    to_resubmit, to_abandon = find_carryover_intents(logger, lookback_hours=48, now=now)
    assert to_resubmit == []
    assert [e["signal_id"] for e in to_abandon] == ["old-sig"]


def test_find_friday_signal_survives_weekend(tmp_path):
    """金曜クローズ後シグナルは土日(市場休場)を挟んでも実効 48h 以内なら再発注対象。

    暦では金17:00→月10:00=65h(>48)だが、土日48hを除けば実効17h。土日を除外しないと
    週末跨ぎシグナルが月曜寄付前に stale 判定され取りこぼされる(本 PR が直す回帰)。
    """
    logger = JsonlEventLogger(base_path=tmp_path)
    fri = datetime(2026, 6, 12, 17, 0)  # 金曜 17:00（クローズ後）
    mon = datetime(2026, 6, 15, 10, 0)  # 月曜 10:00 に評価
    _seed_queued(logger, "20260612-093000", occurred_at=fri)
    to_resubmit, to_abandon = find_carryover_intents(logger, lookback_hours=48, now=mon)
    assert [e["signal_id"] for e in to_resubmit] == ["20260612-093000"]
    assert to_abandon == []


def test_find_stale_over_business_hours_abandoned(tmp_path):
    """土日を除いた実効時間が lookback 超なら、週末跨ぎでも abandon する。

    金17:00→火18:00 は実効 49h(金7h+月24h+火18h、土日除外) > 48 → 古すぎる intent は
    打ち切る(週末除外が「無期限に再発注」へ緩むのを防ぐ)。
    """
    logger = JsonlEventLogger(base_path=tmp_path)
    fri = datetime(2026, 6, 12, 17, 0)  # 金曜 17:00
    tue = datetime(2026, 6, 16, 18, 0)  # 火曜 18:00 に評価（実効 49h）
    _seed_queued(logger, "old-fri", occurred_at=fri)
    to_resubmit, to_abandon = find_carryover_intents(logger, lookback_hours=48, now=tue)
    assert to_resubmit == []
    assert [e["signal_id"] for e in to_abandon] == ["old-fri"]


def test_find_max_resubmits_abandoned(tmp_path):
    """co の failed が上限に達した intent は abandon（毎オープン誤発注連打を止める）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-fail")
    for _ in range(DEFAULT_MAX_RESUBMITS):
        _seed_order_recorded(logger, _co_signal_id("sig-fail"), status="failed")
    to_resubmit, to_abandon = find_carryover_intents(logger, max_resubmits=DEFAULT_MAX_RESUBMITS)
    assert to_resubmit == []
    assert [e["signal_id"] for e in to_abandon] == ["sig-fail"]


def test_find_replays_all_in_received_order(tmp_path):
    """同一 ticker に複数 intent が溜まったら受信順(ASC)で全件返す（delta シグナルの意図を再現）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    t0 = datetime.now() - timedelta(minutes=3)
    _seed_queued(logger, "sig-A", action="buy", occurred_at=t0)
    _seed_queued(logger, "sig-B", action="sell", occurred_at=t0 + timedelta(minutes=1))
    _seed_queued(logger, "sig-C", action="buy", occurred_at=t0 + timedelta(minutes=2))
    to_resubmit, _ = find_carryover_intents(logger)
    assert [e["signal_id"] for e in to_resubmit] == ["sig-A", "sig-B", "sig-C"]


def test_find_abandoned_state_excluded(tmp_path):
    """既に abandoned とマークされた signal_id は再度拾わない。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-ab", state="queued")
    _seed_queued(logger, "sig-ab", state="abandoned")
    to_resubmit, _ = find_carryover_intents(logger)
    assert to_resubmit == []


# --- run_carryover_resubmit_once -----------------------------------------


def test_run_resubmits_when_open(tmp_path):
    """オープン中の未解消 intent は route で再発注され、co の order_recorded(accepted) が残る。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    prov = _status_provider(orders=[OrderRecord(code="US.TLT", order_id="br_999", order_status="FILLED_ALL", dealt_qty=2.0, dealt_avg_price=90.0)])
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=prov, event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=IdempotencyStore(),
    )
    assert n == 1
    assert len(router.calls) == 1
    # 再発注で解消 → 2 回目の find は空
    to_resubmit, _ = find_carryover_intents(logger)
    assert to_resubmit == []


def test_run_skips_when_market_closed(tmp_path):
    """market closed のままなら route を呼ばない（pre-market 誤約定の防止）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "CLOSED"})
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=_status_provider(), event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=IdempotencyStore(),
    )
    assert n == 0
    assert router.calls == []


def test_run_no_pending_skips_market_query(tmp_path):
    """未解消ゼロなら OpenD(market state) へ問い合わせない（無駄接続回避）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=_status_provider(), event_logger=logger,
        order_router=router, fill_service=FillEventService(logger),
    )
    assert n == 0
    assert ms.calls == 0


def test_run_idempotency_blocks_double_submit(tmp_path):
    """同一 co_signal_id は idempotency で 1 回に収束（再起動直後+通常サイクルの重なり対策）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    idem = IdempotencyStore()
    idem.check_and_record(_co_signal_id("20260608-093000"))  # 既に発注済み相当
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=_status_provider(), event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=idem,
    )
    assert n == 0
    assert router.calls == []


def test_run_sell_guard_clamps_resubmit(tmp_path):
    """再発注 sell が can_sell_qty を超えると sell_guard で clamp される（over-sell を境界で根絶）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-sell", action="sell", qty=5.0)
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    # 実保有 2 株のみ → 5 売り要求は 2 に clamp
    prov = _status_provider(
        positions=[PositionRecord(code="US.TLT", qty=2.0, can_sell_qty=2.0)],
        orders=[OrderRecord(code="US.TLT", order_id="br_999", order_status="FILLED_ALL", dealt_qty=2.0)],
    )
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=prov, event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=IdempotencyStore(),
    )
    assert n == 1
    assert router.calls[0].quantity == 2.0


def test_run_failed_resubmit_records_failed(tmp_path):
    """broker 拒否は order_recorded(failed) を残し、max_resubmits 到達で打ち切り対象になる。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-x")
    router = _FakeRouter(raises=True)
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=_status_provider(), event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=IdempotencyStore(),
    )
    assert n == 0
    failed = logger.load_events(broker="moomoo", event_type="order_recorded")
    assert any(
        e["signal_id"] == _co_signal_id("sig-x") and e["status"] == "failed" for e in failed
    )


def test_run_retry_after_failed_not_blocked_by_idempotency(tmp_path):
    """route 失敗後は冪等キーを解放し、次スイープで即リトライする（TTL 満了まで ~30 分止めない）。

    WHY: 失敗試行は注文を出していないため、broker の一時障害から open 中に回復したら
    速やかに再発注したい。failed カウントも進むので max_resubmits の打ち切りも機能する。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-retry")
    router = _FakeRouter(raises=True)
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    idem = IdempotencyStore(ttl_seconds=600)
    kwargs = dict(
        market_state_provider=ms, status_provider=_status_provider(), event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=idem,
    )
    run_carryover_resubmit_once(**kwargs)
    run_carryover_resubmit_once(**kwargs)
    assert len(router.calls) == 2  # TTL 内でも 2 回呼ばれる（forget で解放）


def test_run_target_reconcile_resolves_side_and_qty(tmp_path):
    """target_qty 付き intent は再発注時に open 時点の実保有との差分で side/qty を再解決する (#80 継承)。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "sig-t", action="buy", qty=5.0, target_qty=3.0)
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    # 実保有 1・target 3 → buy 2 に再解決される
    prov = _status_provider(
        positions=[PositionRecord(code="US.TLT", qty=1.0, can_sell_qty=1.0)],
        orders=[OrderRecord(code="US.TLT", order_id="br_999", order_status="FILLED_ALL", dealt_qty=2.0)],
    )
    n = run_carryover_resubmit_once(
        market_state_provider=ms, status_provider=prov, event_logger=logger,
        order_router=router, fill_service=FillEventService(logger), idempotency=IdempotencyStore(),
    )
    assert n == 1
    assert router.calls[0].action == "buy"
    assert router.calls[0].quantity == 2.0


# --- loop ----------------------------------------------------------------


def test_loop_runs_immediately_and_cancels(tmp_path):
    """起動直後に 1 回実行し、cancel で素直に終了する（取りこぼし回収 + zombie task 防止）。"""
    logger = JsonlEventLogger(base_path=tmp_path)
    _seed_queued(logger, "20260608-093000")
    router = _FakeRouter()
    ms = _FakeMarketState({"US.TLT": "AFTERNOON"})
    prov = _status_provider(orders=[OrderRecord(code="US.TLT", order_id="br_999", order_status="FILLED_ALL", dealt_qty=2.0)])

    async def _run():
        task = asyncio.create_task(
            carryover_resubmit_loop(
                market_state_provider=ms, status_provider=prov, event_logger=logger,
                order_router=router, fill_service=FillEventService(logger),
                idempotency=IdempotencyStore(), interval_seconds=3600,
            )
        )
        # 起動直後の即時実行を待つ
        for _ in range(50):
            await asyncio.sleep(0.02)
            if router.calls:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(router.calls) == 1
