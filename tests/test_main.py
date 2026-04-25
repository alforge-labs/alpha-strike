"""main.py のエントリポイントテスト"""
import runpy
from pathlib import Path
from unittest.mock import ANY, patch

ROOT = Path(__file__).parent.parent


def test_main_does_not_call_uvicorn_on_import():
    """インポートだけでは uvicorn.run が呼ばれないことを確認する"""
    with patch("uvicorn.run") as mock_run:
        import importlib
        import sys

        sys.modules.pop("main", None)
        importlib.import_module("main")
    mock_run.assert_not_called()


def test_main_calls_uvicorn_run_with_correct_args():
    """__main__ として実行したとき uvicorn.run が正しい引数で呼ばれることを確認する"""
    with patch("uvicorn.run") as mock_run:
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
    mock_run.assert_called_once_with(
        ANY,
        host="0.0.0.0",
        port=8080,
        reload=False,
    )
