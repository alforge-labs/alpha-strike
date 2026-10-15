"""alpha-strike-watchdog（単発実行 CLI）のテスト。

WHY: systemd timer から呼ばれる。終了コードが非ゼロだと timer が failed 扱いになり、
「監視が動いている」ことと「途絶している」ことの区別がつかなくなる。途絶の有無は通知と
イベントログで表現し、終了コードには乗せない、という契約をここで固定する。
"""

from __future__ import annotations

import pytest

from alpha_strike.cli import watchdog_main


@pytest.fixture(autouse=True)
def _isolate_watchdog_io(monkeypatch, tmp_path):
    """このファイルのテストが実データと実通知に触れないようにする。

    watchdog_main は本物の JsonlEventLogger と NtfyNotifier を組み立てるため、
    隔離しないと開発者の実イベントログへ書き込み、ntfy へ通知が飛ぶ。
    """
    monkeypatch.setenv("LIVE_EVENTS_PATH", str(tmp_path))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


class TestWatchdogMain:
    def test_無効化されていれば何もせず0を返す(self, monkeypatch):
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")
        called: list[int] = []
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "0")
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once",
            lambda **kw: called.append(1),
        )
        assert watchdog_main() == 0
        assert called == []

    def test_途絶を検知しても0を返す(self, monkeypatch):
        """検知は通知とイベントログで表現する。終了コードには乗せない。"""
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")
        monkeypatch.setattr(
            "alpha_strike.cli.load_watchdog_state", lambda *a, **kw: object()
        )
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once", lambda **kw: object()
        )
        assert watchdog_main() == 0

    def test_例外が起きても0を返す(self, monkeypatch):
        """timer の次回実行を止めないため、失敗しても 0 で終わる。"""
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")

        def _boom(*a, **kw):
            raise RuntimeError("イベントログ読込失敗")

        monkeypatch.setattr("alpha_strike.cli.load_watchdog_state", _boom)
        assert watchdog_main() == 0

    def test_run_signal_watchdog_onceへ設定値が渡る(self, monkeypatch):
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")
        captured: dict = {}
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")
        monkeypatch.setenv("SIGNAL_WATCHDOG_THRESHOLD_HOURS", "72")
        monkeypatch.setenv("SIGNAL_WATCHDOG_RENOTIFY_HOURS", "12")
        monkeypatch.setenv("SIGNAL_WATCHDOG_BROKER", "moomoo")
        monkeypatch.setattr(
            "alpha_strike.cli.load_watchdog_state", lambda *a, **kw: "STATE"
        )
        monkeypatch.setattr(
            "alpha_strike.cli.run_signal_watchdog_once",
            lambda **kw: captured.update(kw),
        )
        assert watchdog_main() == 0
        assert captured["threshold_hours"] == 72.0
        assert captured["renotify_hours"] == 12.0
        assert captured["broker"] == "moomoo"
        assert captured["state"] == "STATE"


class TestWatchdogMainRunsBothChecks:
    """1 つの timer で 2 本のチェックを回すこと。

    timer を増やすと登録忘れで監視がサイレントに消えるリスクが倍になる。
    """

    def test_両方のチェックが呼ばれる(self, monkeypatch):
        from alpha_strike import cli

        calls = []
        monkeypatch.setattr(
            cli, "run_signal_watchdog_once", lambda **kw: calls.append("signal")
        )
        monkeypatch.setattr(
            cli, "run_order_watchdog_once", lambda **kw: calls.append("order")
        )
        monkeypatch.setenv("NTFY_TOPIC", "")

        assert cli.watchdog_main() == 0
        assert calls == ["signal", "order"]

    def test_signal側が失敗してもorder側は走る(self, monkeypatch):
        """片方の例外でもう片方の監視を落とさない。"""
        from alpha_strike import cli

        calls = []

        def _boom(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli, "run_signal_watchdog_once", _boom)
        monkeypatch.setattr(
            cli, "run_order_watchdog_once", lambda **kw: calls.append("order")
        )

        assert cli.watchdog_main() == 0
        assert calls == ["order"]

    def test_order側が失敗してもsignal側は走り0を返す(self, monkeypatch):
        from alpha_strike import cli

        calls = []

        def _boom(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            cli, "run_signal_watchdog_once", lambda **kw: calls.append("signal")
        )
        monkeypatch.setattr(cli, "run_order_watchdog_once", _boom)

        assert cli.watchdog_main() == 0
        assert calls == ["signal"]

    def test_無効化されていればorder側を呼ばない(self, monkeypatch):
        from alpha_strike import cli

        calls = []
        monkeypatch.setattr(cli, "run_signal_watchdog_once", lambda **kw: None)
        monkeypatch.setattr(
            cli, "run_order_watchdog_once", lambda **kw: calls.append("order")
        )
        monkeypatch.setenv("ORDER_WATCHDOG_ENABLED", "0")

        assert cli.watchdog_main() == 0
        assert calls == []

    def test_order側へ設定値とcarryover窓が渡る(self, monkeypatch):
        """watchdog と carryover が同じ lookback を使い、判断が食い違わないこと。"""
        from alpha_strike import cli

        captured: dict = {}
        monkeypatch.setattr(cli, "run_signal_watchdog_once", lambda **kw: None)
        monkeypatch.setattr(
            cli, "run_order_watchdog_once", lambda **kw: captured.update(kw)
        )
        monkeypatch.setenv("CARRYOVER_LOOKBACK_HOURS", "55")
        monkeypatch.setenv("ORDER_WATCHDOG_GRACE_MINUTES", "7")
        monkeypatch.setenv("ORDER_WATCHDOG_RENOTIFY_HOURS", "5")

        assert cli.watchdog_main() == 0
        assert captured["lookback_hours"] == 55.0
        assert captured["grace_minutes"] == 7.0
        assert captured["renotify_hours"] == 5.0


class TestWatchdogMainInitFailure:
    """共有の初期化が失敗しても 0 を返すこと。

    非ゼロ終了だと systemd が timer を failed 扱いにし、「監視が動いている」と
    「途絶している」の区別がつかなくなる。
    """

    @pytest.mark.parametrize("target", ["NtfyNotifier", "JsonlEventLogger"])
    def test_初期化が例外でも0を返す(self, monkeypatch, target):
        from alpha_strike import cli

        def _boom(*a, **kw):
            raise RuntimeError("init failed")

        monkeypatch.setattr(cli, target, _boom)
        assert cli.watchdog_main() == 0
