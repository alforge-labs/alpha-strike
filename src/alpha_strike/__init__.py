"""alpha-strike — TradingView Webhook を moomoo / OANDA へルーティングする FastAPI サーバー."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("alpha-strike")
except PackageNotFoundError:  # editable install / source 直接実行時
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
