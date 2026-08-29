"""alpha-strike-watchdog（単発実行 CLI）のテスト。

WHY: systemd timer から呼ばれる。終了コードが非ゼロだと timer が failed 扱いになり、
「監視が動いている」ことと「途絶している」ことの区別がつかなくなる。途絶の有無は通知と
イベントログで表現し、終了コードには乗せない、という契約をここで固定する。
"""

from __future__ import annotations

from alpha_strike.cli import watchdog_main


class TestWatchdogMain:
    def test_無効化されていれば何もせず0を返す(self, monkeypatch):
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
        monkeypatch.setenv("SIGNAL_WATCHDOG_ENABLED", "1")

        def _boom(*a, **kw):
            raise RuntimeError("イベントログ読込失敗")

        monkeypatch.setattr("alpha_strike.cli.load_watchdog_state", _boom)
        assert watchdog_main() == 0

    def test_run_signal_watchdog_onceへ設定値が渡る(self, monkeypatch):
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
