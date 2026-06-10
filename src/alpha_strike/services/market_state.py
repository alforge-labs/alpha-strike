"""US 市場のオープン/クローズ判定サービス (#89 carry-over の前提)。

moomoo OpenD の ``OpenQuoteContext.get_market_state`` で対象 ticker の市場状態を取得する。
US 市場の通常立会 (RTH) は moomoo では MarketState ``AFTERNOON`` で表現される
（実機検証: US.AAPL / US.SPY / US.TLT が開場中に ``AFTERNOON`` を返すことを確認）。
PRE_MARKET / AFTER_HOURS / CLOSED / REST 等はクローズ扱い（日足戦略の RTH 寄付に寄せる）。

raw な状態取得は Provider（Protocol、テストで Fake 注入可）に隔離し、「どの状態を open と
みなすか」のポリシーは ``is_market_open`` に分離する（SRP・境界テスト容易）。実装は
``OpenSecTradeContext`` (status_service) とは別に ``OpenQuoteContext`` を使うが、同一 OpenD
(127.0.0.1:11111) に同居でき追加デプロイ不要。
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

logger = logging.getLogger(__name__)

# US 通常立会 (RTH) を表す moomoo MarketState（実機検証で AFTERNOON を確認）。
# pre-market / after-hours は意図的に open とみなさない。
US_OPEN_MARKET_STATES = frozenset({"AFTERNOON"})


class MarketStateProvider(Protocol):
    """市場状態取得の抽象。テストで Fake に差し替え可能にする。"""

    def get_market_state(self, codes: list[str]) -> dict[str, str]:
        """``code -> MarketState 文字列`` のマップを返す。取得不能な code は欠落する。"""


class MoomooMarketStateProvider:
    """moomoo OpenD の ``OpenQuoteContext`` で市場状態を取得する実装。

    moomoo SDK は遅延 import（OANDA 専用構成・テスト環境で import を強制しない）。
    本体と統一して ``futu`` を import する（``moomoo`` と二重 import すると protobuf
    重複登録で衝突するため）。
    """

    def __init__(self, *, host: str | None = None, port: int | None = None) -> None:
        self.host = host or os.getenv("MOOMOO_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("MOOMOO_PORT", "11111"))

    def get_market_state(self, codes: list[str]) -> dict[str, str]:
        import futu  # type: ignore[import-not-found]

        ctx = futu.OpenQuoteContext(host=self.host, port=self.port)
        try:
            ret, df = ctx.get_market_state(codes)
            if ret != futu.RET_OK or df is None or getattr(df, "empty", True):
                return {}
            out: dict[str, str] = {}
            for _, row in df.iterrows():
                code = str(row.get("code", ""))
                state = str(row.get("market_state", ""))
                if code:
                    out[code] = state
            return out
        finally:
            ctx.close()


def is_market_open(provider: MarketStateProvider, ticker: str) -> bool | None:
    """``ticker`` の市場が開場中か。

    Returns:
        - ``True``: 開場中（MarketState が ``US_OPEN_MARKET_STATES`` に含まれる）
        - ``False``: クローズ中
        - ``None``: 判定不能（取得失敗 / 当該 ticker が応答に無い）

    判定不能 (``None``) は fail-safe として呼び出し側で「再発注しない / 通常発注する」など
    安全側に倒すために使う。
    """
    try:
        states = provider.get_market_state([ticker])
    except Exception as exc:  # noqa: BLE001 — 取得失敗は None（不明）で返す
        logger.warning("market state 取得失敗 (%s): %s", ticker, exc)
        return None
    state = states.get(ticker)
    if state is None:
        return None
    return state.upper() in US_OPEN_MARKET_STATES


def build_default_market_state_provider() -> MarketStateProvider:
    """環境変数からデフォルトの MarketStateProvider を構築する。"""
    return MoomooMarketStateProvider()
