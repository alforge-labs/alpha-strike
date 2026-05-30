"""NtfyNotifier のテスト (issue #57 Phase 2)。

NTFY_TOPIC 未設定で no-op、設定時に ntfy へ POST することを opener 注入で検証する。
"""

from __future__ import annotations

from alpha_strike.services.notifier import NtfyNotifier


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_disabled_when_topic_unset(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    n = NtfyNotifier()
    assert n.enabled is False
    # 無効時は opener を呼ばず False を返す（no-op）
    calls: list = []
    assert n.notify("t", "b", opener=lambda req, timeout=0: calls.append(req)) is False
    assert calls == []


def test_enabled_posts_to_ntfy(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "alpha-strike-test")
    n = NtfyNotifier()
    assert n.enabled is True

    captured: dict = {}

    def _opener(req, timeout=0):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp()

    ok = n.notify("注文確定", "US.GLD CANCELLED_ALL", tags=["warning"], opener=_opener)
    assert ok is True
    assert captured["url"].endswith("/alpha-strike-test")
    assert b"CANCELLED_ALL" in captured["data"]
    assert captured["headers"].get("title")  # Title ヘッダが付く


def test_notify_swallows_errors(monkeypatch):
    """通知失敗が発注フローを壊さないよう、例外を握りつぶして False を返す。"""
    monkeypatch.setenv("NTFY_TOPIC", "alpha-strike-test")
    n = NtfyNotifier()

    def _boom(req, timeout=0):
        raise OSError("network down")

    assert n.notify("t", "b", opener=_boom) is False
