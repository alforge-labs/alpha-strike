"""シグナルと発注を対応づける照合キー。

``carryover``（再発注の解消判定）と ``order_watchdog``（発注途絶の検知）の双方が
同じ規則で照合する必要があるため共有モジュールに置く。片方で再実装すると必ず食い違う。
"""

from __future__ import annotations

# carry-over 再発注の signal_id サフィックス。ユーザー signal_id との衝突（偽解消）を
# 避けるため衝突しにくい語にする。
CO_SUFFIX = "__carryover"


def carryover_signal_id(signal_id: str) -> str:
    """carry-over 再発注時に使う signal_id。"""
    return f"{signal_id}{CO_SUFFIX}"


def intent_key(event: dict) -> tuple[str, str]:
    """シグナル / 発注 / carry-over の識別単位 ``(signal_id, ticker)`` (#126)。

    signal_id は bar 単位で払い出され、同一バーの銘柄別シグナルが同じ値を共有するため、
    単独では識別できない（1 銘柄の発注で他銘柄まで解消済みと誤判定する）。

    ``action`` を含めないのは、発注時に target_qty closed-loop (#80) や over-sell ガード
    (#74) が方向を反転させうるため。queued が buy でも order_recorded は sell になり得る
    ので、action を照合に使うと解消判定が成立せず再発注が繰り返される。
    """
    return (str(event.get("signal_id", "")), str(event.get("ticker", "")))


def carryover_key(key: tuple[str, str]) -> tuple[str, str]:
    """シグナル側の識別キーを、対応する carry-over 発注側のキーへ変換する。"""
    signal_id, ticker = key
    return (carryover_signal_id(signal_id), ticker)
