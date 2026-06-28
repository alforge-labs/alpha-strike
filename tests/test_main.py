"""alpha_strike.cli エントリポイントテスト"""

from unittest.mock import patch

import pytest

from alpha_strike import cli


def test_cli_invokes_uvicorn_with_defaults(monkeypatch):
    """引数なし呼び出しでデフォルトホスト/ポートで起動する"""
    # 環境変数を未設定にしてデフォルト値の挙動を確認
    monkeypatch.delenv("ALPHA_STRIKE_HOST", raising=False)
    monkeypatch.delenv("ALPHA_STRIKE_PORT", raising=False)

    with patch("alpha_strike.cli.uvicorn.run") as mock_run:
        rc = cli.main([])

    assert rc == 0
    mock_run.assert_called_once_with(
        "alpha_strike.webhook_server:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
    )


def test_main_prints_alphaforge_cta(capsys):
    """起動時に AlphaForge への送客 CTA を表示する（C3）。"""
    with patch("alpha_strike.cli.uvicorn.run"):
        cli.main([])
    out = capsys.readouterr().out
    assert "alforgelabs.com" in out
    assert "AlphaForge" in out


def test_cli_respects_command_line_arguments():
    """--host / --port / --reload が反映される"""
    with patch("alpha_strike.cli.uvicorn.run") as mock_run:
        cli.main(["--host", "127.0.0.1", "--port", "9000", "--reload"])

    mock_run.assert_called_once_with(
        "alpha_strike.webhook_server:app",
        host="127.0.0.1",
        port=9000,
        reload=True,
    )


def test_cli_respects_env_variables(monkeypatch):
    """ALPHA_STRIKE_HOST / ALPHA_STRIKE_PORT 環境変数が反映される"""
    monkeypatch.setenv("ALPHA_STRIKE_HOST", "127.0.0.1")
    monkeypatch.setenv("ALPHA_STRIKE_PORT", "8765")

    with patch("alpha_strike.cli.uvicorn.run") as mock_run:
        cli.main([])

    mock_run.assert_called_once_with(
        "alpha_strike.webhook_server:app",
        host="127.0.0.1",
        port=8765,
        reload=False,
    )


def test_cli_version_flag_exits_zero(capsys):
    """--version で version を表示して exit 0"""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "alpha-strike" in out
