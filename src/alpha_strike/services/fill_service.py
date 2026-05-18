"""FillEvent 構築・配分・TradeClosedイベント生成サービス"""
from datetime import datetime
from typing import TYPE_CHECKING

from alpha_strike.models import FillEvent, TradeClosedEvent, WebhookPayload

if TYPE_CHECKING:
    from event_logger import JsonlEventLogger


def _generate_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


def _resolve_trade_id(event: dict) -> str:
    return str(event.get("trade_id") or f"trd_{event.get('fill_id')}")


class FillEventService:
    """FillEvent の構築・配分・TradeClosedイベント生成を担当するサービス。"""

    def __init__(self, event_logger: "JsonlEventLogger") -> None:
        self._event_logger = event_logger

    def build(
        self,
        payload: WebhookPayload,
        result: dict,
        signal_id: str,
        internal_order_id: str,
        broker_order_id: str | None,
    ) -> FillEvent | None:
        """OrderResult から FillEvent を構築する。filled_qty/filled_price がなければ None を返す。"""
        filled_qty_raw = result.get("filled_qty")
        filled_price_raw = result.get("filled_price")
        if filled_qty_raw is None or filled_price_raw is None:
            return None

        fill_id = str(result.get("fill_id") or _generate_id("fill"))
        trade_id = str(result.get("trade_id") or f"trd_{fill_id}")
        return FillEvent(
            event_id=_generate_id("evt"),
            signal_id=signal_id,
            order_id=internal_order_id,
            fill_id=fill_id,
            occurred_at=datetime.now(),
            broker=payload.broker,
            asset_class=payload.asset_class,
            action=payload.action,
            ticker=payload.ticker,
            quantity=payload.quantity,
            filled_qty=float(filled_qty_raw),
            filled_price=float(filled_price_raw),
            broker_order_id=broker_order_id,
            trade_id=trade_id,
            strategy_id=payload.strategy_id,
            strategy_version=payload.strategy_version,
            snapshot_id=payload.snapshot_id,
            run_mode=payload.run_mode,
            commission=float(result["commission"]) if result.get("commission") is not None else None,
            slippage_bps=float(result["slippage_bps"]) if result.get("slippage_bps") is not None else None,
        )

    def allocate(self, fill_event: FillEvent) -> list[FillEvent]:
        """クローズ約定を対応するエントリートレードに配分する。

        複数エントリーがある場合、FIFO で数量を割り当てる。
        未対応ブローカーまたはマッチするエントリーがない場合は元の FillEvent をそのまま返す。
        """
        if fill_event.broker not in {"moomoo", "oanda"}:
            return [fill_event]

        recent_fills = self._event_logger.load_events(
            broker=fill_event.broker,
            event_type="fill_received",
            ticker=fill_event.ticker,
            strategy_id=fill_event.strategy_id,
            limit=200,
        )

        opening_action = "sell" if fill_event.action == "buy" else "buy"
        entry_by_trade_id: dict[str, dict] = {}
        for event in recent_fills:
            trade_id = _resolve_trade_id(event)
            if event.get("run_mode") != fill_event.run_mode:
                continue
            if event.get("action") != opening_action:
                continue
            if event.get("filled_qty") is None:
                continue
            summary = entry_by_trade_id.setdefault(
                trade_id,
                {
                    "trade_id": trade_id,
                    "signal_id": event.get("signal_id"),
                    "filled_qty": 0.0,
                    "first_occurred_at": event.get("occurred_at"),
                },
            )
            summary["filled_qty"] += float(event.get("filled_qty") or 0.0)
            if summary["first_occurred_at"] is None or (
                event.get("occurred_at") is not None
                and event["occurred_at"] < summary["first_occurred_at"]
            ):
                summary["first_occurred_at"] = event.get("occurred_at")

        matched_exit_qty_by_trade_id: dict[str, float] = {}
        for event in recent_fills:
            trade_id = _resolve_trade_id(event)
            if event.get("action") != fill_event.action:
                continue
            if event.get("filled_qty") is None:
                continue
            matched_exit_qty_by_trade_id[trade_id] = (
                matched_exit_qty_by_trade_id.get(trade_id, 0.0)
                + float(event.get("filled_qty") or 0.0)
            )

        candidate_entries: list[dict] = []
        for summary in entry_by_trade_id.values():
            remaining_qty = summary["filled_qty"] - matched_exit_qty_by_trade_id.get(
                summary["trade_id"], 0.0
            )
            if remaining_qty <= 0:
                continue
            candidate_entries.append({**summary, "remaining_qty": remaining_qty})

        candidate_entries.sort(key=lambda item: item["first_occurred_at"] or "")
        if not candidate_entries:
            return [fill_event]

        remaining_close_qty = float(fill_event.filled_qty)
        allocated_events: list[FillEvent] = []
        for index, candidate in enumerate(candidate_entries, start=1):
            if remaining_close_qty <= 0:
                break
            allocated_qty = min(remaining_close_qty, float(candidate["remaining_qty"]))
            if allocated_qty <= 0:
                continue
            allocated_events.append(
                fill_event.model_copy(
                    update={
                        "event_id": fill_event.event_id if index == 1 else _generate_id("evt"),
                        "fill_id": fill_event.fill_id if index == 1 else f"{fill_event.fill_id}_{index}",
                        "trade_id": candidate["trade_id"],
                        "signal_id": candidate.get("signal_id") or fill_event.signal_id,
                        "quantity": allocated_qty,
                        "filled_qty": allocated_qty,
                    }
                )
            )
            remaining_close_qty -= allocated_qty

        if remaining_close_qty > 0:
            allocated_events.append(
                fill_event.model_copy(
                    update={
                        "event_id": _generate_id("evt") if allocated_events else fill_event.event_id,
                        "fill_id": (
                            f"{fill_event.fill_id}_residual"
                            if allocated_events
                            else fill_event.fill_id
                        ),
                        "trade_id": f"trd_{fill_event.fill_id}_reversal",
                        "quantity": remaining_close_qty,
                        "filled_qty": remaining_close_qty,
                    }
                )
            )

        return allocated_events or [fill_event]

    def build_trade_closed(self, fill_event: FillEvent) -> TradeClosedEvent | None:
        """クローズ約定から TradeClosedEvent を構築する。

        対象ブローカーでない、trade_id がない、エントリーが見つからない場合は None を返す。
        """
        if fill_event.broker not in {"moomoo", "oanda"}:
            return None
        if fill_event.trade_id is None:
            return None

        recent_trade_closed = self._event_logger.load_events(
            broker=fill_event.broker,
            event_type="trade_closed",
            ticker=fill_event.ticker,
            strategy_id=fill_event.strategy_id,
            limit=200,
        )
        closed_trade_ids = {
            str(event["trade_id"])
            for event in recent_trade_closed
            if event.get("trade_id") is not None
        }
        if fill_event.trade_id in closed_trade_ids:
            return None

        recent_fills = self._event_logger.load_events(
            broker=fill_event.broker,
            event_type="fill_received",
            ticker=fill_event.ticker,
            strategy_id=fill_event.strategy_id,
            limit=200,
        )
        trade_fills = [
            event for event in recent_fills if _resolve_trade_id(event) == fill_event.trade_id
        ]
        if not trade_fills:
            return None

        entry_fills = [event for event in trade_fills if event.get("action") != fill_event.action]
        if not entry_fills:
            return None

        opening_action = str(entry_fills[0]["action"])
        exit_fills = [event for event in trade_fills if event.get("action") == fill_event.action]
        entry_qty = sum(float(event.get("filled_qty") or 0.0) for event in entry_fills)
        total_exit_qty = sum(float(event.get("filled_qty") or 0.0) for event in exit_fills)
        if total_exit_qty < entry_qty or entry_qty <= 0:
            return None

        entry_notional = sum(
            float(event["filled_price"]) * float(event["filled_qty"])
            for event in entry_fills
            if event.get("filled_price") is not None and event.get("filled_qty") is not None
        )
        exit_notional = sum(
            float(event["filled_price"]) * float(event["filled_qty"])
            for event in exit_fills
            if event.get("filled_price") is not None and event.get("filled_qty") is not None
        )
        entry_price = entry_notional / entry_qty
        exit_price = exit_notional / total_exit_qty
        qty = entry_qty

        gross_pnl = (
            (exit_price - entry_price) * qty
            if opening_action == "buy"
            else (entry_price - exit_price) * qty
        )
        gross_pnl = round(gross_pnl, 10)
        total_commission = sum(
            float(event["commission"])
            for event in trade_fills
            if event.get("commission") is not None
        )
        net_pnl = round(gross_pnl - total_commission, 10)
        first_entry_fill = min(
            entry_fills,
            key=lambda event: event.get("occurred_at") or "",
        )

        return TradeClosedEvent(
            event_id=_generate_id("evt"),
            signal_id=str(first_entry_fill["signal_id"]),
            trade_id=fill_event.trade_id,
            occurred_at=datetime.now(),
            closed_at=fill_event.occurred_at,
            broker=fill_event.broker,
            asset_class=fill_event.asset_class,
            action=opening_action,
            ticker=fill_event.ticker,
            quantity=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            strategy_id=fill_event.strategy_id,
            strategy_version=fill_event.strategy_version,
            snapshot_id=fill_event.snapshot_id,
            run_mode=fill_event.run_mode,
            commission=total_commission,
            exit_reason="opposite_fill",
        )
