"""NtfyNotifier のテスト (issue #57 Phase 2)。

NTFY_TOPIC 未設定で no-op、設定時に ntfy へ POST することを opener 注入で検証する。
"""

from __future__ import annotations

from email.header import decode_header, make_header

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
    result = n.notify("t", "b", opener=lambda req, timeout=0: calls.append(req))
    assert result is False
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


def test_non_ascii_headers_are_latin1_safe(monkeypatch):
    """絵文字・日本語を含むタイトル/タグでも通知が送出できること。

    urllib は HTTP ヘッダを latin-1 で書き出すため、生の UTF-8 を渡すと送信前に
    UnicodeEncodeError となり通知が丸ごと失われる（本番で約定通知が全滅した）。
    ntfy は RFC 2047 エンコードヘッダを解釈するので、非 ASCII は必ず包んで送る。
    """
    monkeypatch.setenv("NTFY_TOPIC", "alpha-strike-test")
    n = NtfyNotifier()

    captured: dict = {}

    def _opener(req, timeout=0):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp()

    title = "✅ 注文 FILLED_ALL: US.GLD"
    ok = n.notify(title, "本文", tags=["白チェック✅"], priority="high", opener=_opener)
    assert ok is True

    # 実際に urllib が行う latin-1 エンコードが全ヘッダで通ること（本番失敗条件そのもの）。
    # エンコードできなければここで UnicodeEncodeError が送出されテストが落ちる。
    for value in captured["headers"].values():
        value.encode("latin-1")

    # ntfy 側で元の文字列に復元できること（受信側の見た目が壊れない）
    assert str(make_header(decode_header(captured["headers"]["title"]))) == title
    assert str(make_header(decode_header(captured["headers"]["tags"]))) == "白チェック✅"


def test_ascii_headers_are_sent_verbatim(monkeypatch):
    """ASCII のみのヘッダは素のまま送る（不要なエンコードで可読性を落とさない）。"""
    monkeypatch.setenv("NTFY_TOPIC", "alpha-strike-test")
    n = NtfyNotifier()

    captured: dict = {}

    def _opener(req, timeout=0):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp()

    ok = n.notify("Order FILLED_ALL", "body", tags=["white_check_mark"], opener=_opener)
    assert ok is True
    assert captured["headers"]["title"] == "Order FILLED_ALL"
    assert captured["headers"]["tags"] == "white_check_mark"


def test_notify_swallows_errors(monkeypatch):
    """通知失敗が発注フローを壊さないよう、例外を握りつぶして False を返す。"""
    monkeypatch.setenv("NTFY_TOPIC", "alpha-strike-test")
    n = NtfyNotifier()

    def _boom(req, timeout=0):
        raise OSError("network down")

    assert n.notify("t", "b", opener=_boom) is False
