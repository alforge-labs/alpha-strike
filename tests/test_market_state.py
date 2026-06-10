"""market_state の市場オープン判定テスト (#89)。

WHY: carry-over 再発注は「市場が開場中か」の判定に依存する。AFTERNOON を open と
誤らず・pre-market を open と誤認せず・判定不能を None で安全側に倒す、という
境界を固定することで、オープン前の誤約定やクローズ後の取りこぼしを防ぐ。
"""

from __future__ import annotations

import pytest

from alpha_strike.services.market_state import is_market_open


class _FakeProvider:
    def __init__(self, states: dict[str, str] | None = None, *, raises: bool = False):
        self._states = states or {}
        self._raises = raises

    def get_market_state(self, codes: list[str]) -> dict[str, str]:
        if self._raises:
            raise RuntimeError("OpenD 接続失敗")
        return {c: self._states[c] for c in codes if c in self._states}


def test_afternoon_is_open():
    """US RTH は moomoo では AFTERNOON。これを open と判定できないと carry-over した
    注文がオープン時に再発注されず永久に約定しない。"""
    assert is_market_open(_FakeProvider({"US.AAPL": "AFTERNOON"}), "US.AAPL") is True


@pytest.mark.parametrize("state", ["CLOSED", "REST", "PRE_MARKET", "AFTER_HOURS", "NIGHT_OPEN"])
def test_non_rth_states_are_closed(state):
    """pre-market / after-hours / closed を open と誤認するとオープン前に約定してしまう
    （日足戦略が想定する RTH 寄付からズレる）。"""
    assert is_market_open(_FakeProvider({"US.AAPL": state}), "US.AAPL") is False


def test_missing_ticker_returns_none():
    """応答に当該 ticker が無い場合は判定不能。fail-safe に倒すため False ではなく None。"""
    assert is_market_open(_FakeProvider({"US.SPY": "AFTERNOON"}), "US.AAPL") is None


def test_provider_failure_returns_none():
    """OpenD 障害時は例外を伝播させず None。ループを止めず・誤発注もしない。"""
    assert is_market_open(_FakeProvider(raises=True), "US.AAPL") is None


def test_case_insensitive_state():
    """SDK が小文字/混在で返しても AFTERNOON を open と判定する（堅牢性）。"""
    assert is_market_open(_FakeProvider({"US.AAPL": "afternoon"}), "US.AAPL") is True
