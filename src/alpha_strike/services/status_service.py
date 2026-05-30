"""read-only トレード状況取得サービス (issue #57 Phase 1)。

broker（moomoo OpenD）から口座サマリ・保有建玉・直近注文を集約して返す。

設計原則: webhook 受信ログではなく **broker 由来の実 order/fill ステータス** を正とする。
``recent_orders`` には order_status（FILLED_ALL / CANCELLED_ALL 等）と dealt_qty を含め、
「submission 成功 ≠ fill 成功」の盲点を可視化する。

provider は Protocol で抽象化し、テストでは FakeProvider を注入できる。実装は
``MoomooStatusProvider``（moomoo SDK を遅延 import）。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from pydantic import BaseModel


class AccountSummary(BaseModel):
    """口座サマリ。値が取得できないフィールドは None。"""

    total_assets: float | None = None
    cash: float | None = None
    power: float | None = None
    market_val: float | None = None
    frozen_cash: float | None = None
    currency: str | None = None


class PositionRecord(BaseModel):
    """保有建玉 1 件。"""

    code: str
    qty: float = 0.0
    can_sell_qty: float = 0.0
    cost_price: float | None = None
    nominal_price: float | None = None
    market_val: float | None = None
    pl_val: float | None = None
    pl_ratio: float | None = None


class OrderRecord(BaseModel):
    """注文 1 件（実ステータス付き）。"""

    code: str
    trd_side: str = ""
    order_type: str = ""
    qty: float = 0.0
    order_status: str = ""
    dealt_qty: float = 0.0
    dealt_avg_price: float = 0.0
    order_id: str = ""
    create_time: str = ""


class AccountStatus(BaseModel):
    """status API 応答のトップレベル。"""

    broker: str
    trd_env: str
    account: AccountSummary
    positions: list[PositionRecord]
    recent_orders: list[OrderRecord]


class StatusProvider(Protocol):
    """トレード状況取得の抽象。テストで差し替え可能にする。"""

    def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
        """broker から口座サマリ・建玉・直近注文を取得する。"""


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "" or str(value).upper() == "N/A":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class MoomooStatusProvider:
    """moomoo OpenD から口座状態を取得する StatusProvider 実装。

    moomoo SDK は遅延 import（テスト環境・OANDA 専用構成で import を強制しないため）。
    """

    _MARKET_NAME = "US"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        trd_env: str | None = None,
        history_days: int = 7,
    ) -> None:
        self.host = host or os.getenv("MOOMOO_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("MOOMOO_PORT", "11111"))
        self.default_trd_env = (trd_env or os.getenv("MOOMOO_TRD_ENV", "SIMULATE")).upper()
        self.history_days = history_days

    def get_status(self, *, trd_env: str | None = None) -> AccountStatus:
        from datetime import datetime, timedelta

        import futu  # type: ignore[import-not-found]  # 本体と統一 (moomoo と二重 import すると protobuf 重複登録で衝突)

        env_str = (trd_env or self.default_trd_env).upper()
        env = (
            futu.TrdEnv.SIMULATE
            if env_str == "SIMULATE"
            else futu.TrdEnv.REAL
        )
        ctx = futu.OpenSecTradeContext(
            filter_trdmarket=futu.TrdMarket.US, host=self.host, port=self.port
        )
        try:
            account = self._query_account(ctx, env)
            positions = self._query_positions(ctx, env)
            now = datetime.now()
            start = (now - timedelta(days=self.history_days)).strftime("%Y-%m-%d 00:00:00")
            end = now.strftime("%Y-%m-%d %H:%M:%S")
            orders = self._query_orders(ctx, env, start, end)
        finally:
            ctx.close()

        return AccountStatus(
            broker="moomoo",
            trd_env=env_str,
            account=account,
            positions=positions,
            recent_orders=orders,
        )

    @staticmethod
    def _query_account(ctx: Any, env: Any) -> AccountSummary:
        import futu  # type: ignore[import-not-found]  # 本体と統一 (moomoo と二重 import すると protobuf 重複登録で衝突)

        ret, df = ctx.accinfo_query(trd_env=env)
        if ret != futu.RET_OK or df is None or df.empty:
            return AccountSummary()
        row = df.iloc[0]
        return AccountSummary(
            total_assets=_as_float(row.get("total_assets")),
            cash=_as_float(row.get("cash")),
            power=_as_float(row.get("power")),
            market_val=_as_float(row.get("market_val")),
            frozen_cash=_as_float(row.get("frozen_cash")),
            currency=(str(row.get("currency")) if row.get("currency") is not None else None),
        )

    @staticmethod
    def _query_positions(ctx: Any, env: Any) -> list[PositionRecord]:
        import futu  # type: ignore[import-not-found]  # 本体と統一 (moomoo と二重 import すると protobuf 重複登録で衝突)

        ret, df = ctx.position_list_query(trd_env=env)
        if ret != futu.RET_OK or df is None or df.empty:
            return []
        out: list[PositionRecord] = []
        for _, row in df.iterrows():
            out.append(
                PositionRecord(
                    code=str(row.get("code", "")),
                    qty=_as_float(row.get("qty")) or 0.0,
                    can_sell_qty=_as_float(row.get("can_sell_qty")) or 0.0,
                    cost_price=_as_float(row.get("cost_price")),
                    nominal_price=_as_float(row.get("nominal_price")),
                    market_val=_as_float(row.get("market_val")),
                    pl_val=_as_float(row.get("pl_val")),
                    pl_ratio=_as_float(row.get("pl_ratio")),
                )
            )
        return out

    @staticmethod
    def _query_orders(
        ctx: Any, env: Any, start: str, end: str
    ) -> list[OrderRecord]:
        import futu  # type: ignore[import-not-found]  # 本体と統一 (moomoo と二重 import すると protobuf 重複登録で衝突)

        ret, df = ctx.order_list_query(trd_env=env, start=start, end=end)
        if ret != futu.RET_OK or df is None or df.empty:
            return []
        out: list[OrderRecord] = []
        for _, row in df.iterrows():
            out.append(
                OrderRecord(
                    code=str(row.get("code", "")),
                    trd_side=str(row.get("trd_side", "")),
                    order_type=str(row.get("order_type", "")),
                    qty=_as_float(row.get("qty")) or 0.0,
                    order_status=str(row.get("order_status", "")),
                    dealt_qty=_as_float(row.get("dealt_qty")) or 0.0,
                    dealt_avg_price=_as_float(row.get("dealt_avg_price")) or 0.0,
                    order_id=str(row.get("order_id", "")),
                    create_time=str(row.get("create_time", "")),
                )
            )
        return out


def build_default_status_provider() -> StatusProvider:
    """環境変数からデフォルトの StatusProvider を構築する。"""
    return MoomooStatusProvider()
