# Phase 1: alpha-strike SOLID リファクタリング 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** alpha-strike の `webhook_server.py` を薄いHTTPレイヤーに絞り込み、ブローカーハンドラーをProtocol + Strategyパターンで拡張可能にする。

**Architecture:** `BrokerHandler` Protocolで抽象インターフェースを定義。`OrderRouter`（ブローカー登録辞書）で注文ルーティングを担当。`FillEventService`でFillEvent構築・配分・TradeClosedイベント生成を集約。

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pytest, uv

---

## ファイルマップ

| 操作 | パス | 内容 |
|------|------|------|
| 新規作成 | `handlers/base.py` | `BrokerHandler` Protocol |
| 修正 | `handlers/oanda_handler.py` | `oanda_order_handler` → `OandaHandler` クラス |
| 修正 | `handlers/moomoo_handler.py` | `moomoo_order_handler` → `MoomooHandler` クラス |
| 修正 | `handlers/__init__.py` | クラスをエクスポート |
| 新規作成 | `services/__init__.py` | パッケージ |
| 新規作成 | `services/order_service.py` | `OrderRouter`, `build_default_router()` |
| 新規作成 | `services/fill_service.py` | `FillEventService` |
| 修正 | `webhook_server.py` | `OrderRouter`/`FillEventService`へ委譲 |
| 新規作成 | `tests/test_order_service.py` | `OrderRouter` 単体テスト |
| 新規作成 | `tests/test_fill_service.py` | `FillEventService` 単体テスト |
| 修正 | `tests/test_oanda_handler.py` | `OandaHandler` クラス対応 |
| 修正 | `tests/test_moomoo_handler.py` | `MoomooHandler` クラス対応 |
| 修正 | `tests/test_webhook_server.py` | 最小限の修正（HTTPレイヤーテストは変更なし） |

---

### Task 1: BrokerHandler Protocol を作成する

**Files:**
- Create: `handlers/base.py`

- [ ] **Step 1: `handlers/base.py` を作成する**

```python
# handlers/base.py
"""ブローカーハンドラーの抽象インターフェース"""
from typing import Protocol

from models import WebhookPayload


class BrokerHandler(Protocol):
    """ブローカー注文ハンドラーの共通インターフェース。
    
    新しいブローカーを追加するには、このProtocolを満たすクラスを
    `handlers/` に追加し `build_default_router()` に登録するだけでよい。
    """

    def execute(self, payload: WebhookPayload) -> dict:
        """注文を実行し、結果dictを返す。
        
        Raises:
            ImportError: 必要なライブラリが未インストールの場合
            ValueError: 環境変数が不足または不正な場合
            RuntimeError: APIがエラーを返した場合
        """
        ...
```

- [ ] **Step 2: コミットする**

```bash
cd /Users/sakae/dev/alpha-trade/alpha-strike
git add handlers/base.py
git commit -m "feat: BrokerHandler Protocol を追加"
```

---

### Task 2: OandaHandler クラスへリファクタリングする

**Files:**
- Modify: `handlers/oanda_handler.py`
- Modify: `tests/test_oanda_handler.py`

- [ ] **Step 1: テストを先にリファクタリングする（OandaHandler クラス対応）**

`tests/test_oanda_handler.py` の `oanda_order_handler` を `OandaHandler().execute` に変更する。インポート行を修正:

```python
# 変更前
from handlers.oanda_handler import _call_oanda_api, _to_oanda_instrument, oanda_order_handler

# 変更後
from handlers.oanda_handler import _call_oanda_api, _to_oanda_instrument, OandaHandler
```

`TestOandaOrderHandler` クラス内の全テストで `oanda_order_handler(payload)` を `OandaHandler().execute(payload)` に変更する:

```python
class TestOandaOrderHandler:
    def test_missing_api_key_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("OANDA_API_KEY", raising=False)
        monkeypatch.setenv("OANDA_ACCOUNT_ID", "123")
        with pytest.raises(ValueError, match="OANDA_API_KEY"):
            OandaHandler().execute(_make_payload())

    def test_missing_account_id_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("OANDA_API_KEY", "key")
        monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
        with pytest.raises(ValueError, match="OANDA_ACCOUNT_ID"):
            OandaHandler().execute(_make_payload())

    # ... 他のテストも同様に OandaHandler().execute(payload) に変更
```

- [ ] **Step 2: テストが失敗することを確認する**

```bash
cd /Users/sakae/dev/alpha-trade/alpha-strike
uv run pytest tests/test_oanda_handler.py -v
```
期待: `ImportError: cannot import name 'OandaHandler'`

- [ ] **Step 3: `handlers/oanda_handler.py` を `OandaHandler` クラスに変更する**

既存の `oanda_order_handler` 関数を `OandaHandler` クラスの `execute` メソッドに変換する。`_to_oanda_instrument`、`_call_oanda_api`、`_is_retryable_oanda_error` はモジュールレベル関数として残す（テストから直接参照されているため）。

```python
# handlers/oanda_handler.py
"""OANDA証券 REST API v20 を使用した注文ハンドラー"""

import logging
import os

import requests
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from models import WebhookPayload

logger = logging.getLogger(__name__)

OANDA_PRACTICE_URL = "https://api-fxpractice.oanda.com"
OANDA_LIVE_URL = "https://api-fxtrade.oanda.com"


def _is_retryable_oanda_error(exc: Exception) -> bool:
    """5xx エラーおよびネットワーク一時障害のみリトライ対象とする。"""
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


@retry(
    retry=retry_if_exception(_is_retryable_oanda_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _call_oanda_api(url: str, body: dict, headers: dict) -> dict:
    """OANDA REST API を呼び出す。"""
    response = requests.post(url, json=body, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def _to_oanda_instrument(ticker: str, asset_class: str) -> str:
    """TradingViewのティッカーをOANDA instrument形式に変換する。"""
    if asset_class in ("FX", "COMMODITY") and len(ticker) == 6 and "_" not in ticker:
        return f"{ticker[:3]}_{ticker[3:]}"
    if asset_class in ("US", "INDEX") and "_" not in ticker:
        return f"{ticker}_USD"
    if asset_class in ("FX", "COMMODITY") and len(ticker) != 6:
        logger.warning(
            "FX/COMMODITYの ticker が6文字ではありません。変換せずに送信します: %s",
            ticker,
        )
    return ticker


class OandaHandler:
    """OANDA証券への注文を実行するハンドラー。"""

    def execute(self, payload: WebhookPayload) -> dict:
        """OANDA証券に成行注文を送信する。

        Returns:
            {"order_id": str, "instrument": str, ...}

        Raises:
            ValueError: 環境変数が不足または不正な場合
            requests.RequestException: API呼び出しに失敗した場合
        """
        api_key = os.getenv("OANDA_API_KEY", "")
        account_id = os.getenv("OANDA_ACCOUNT_ID", "")
        oanda_env = os.getenv("OANDA_ENV", "PRACTICE").upper()

        if not api_key:
            raise ValueError("環境変数 OANDA_API_KEY が設定されていません")
        if not account_id:
            raise ValueError("環境変数 OANDA_ACCOUNT_ID が設定されていません")
        if oanda_env not in ("PRACTICE", "LIVE"):
            raise ValueError(
                f"OANDA_ENV は PRACTICE または LIVE である必要があります: {oanda_env!r}"
            )

        base_url = OANDA_PRACTICE_URL if oanda_env == "PRACTICE" else OANDA_LIVE_URL
        instrument = _to_oanda_instrument(payload.ticker, payload.asset_class)
        units = payload.quantity if payload.action == "buy" else -payload.quantity

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
            }
        }

        url = f"{base_url}/v3/accounts/{account_id}/orders"
        logger.info(
            "OANDA注文送信: instrument=%s units=%s env=%s", instrument, units, oanda_env
        )

        data = _call_oanda_api(url, body, headers)
        order_id = data.get("orderCreateTransaction", {}).get("id", "unknown")
        fill_tx = data.get("orderFillTransaction", {})
        filled_qty = abs(float(fill_tx["units"])) if fill_tx.get("units") is not None else None
        filled_price = float(fill_tx["price"]) if fill_tx.get("price") is not None else None
        fill_id = str(fill_tx["id"]) if fill_tx.get("id") is not None else None

        logger.info("OANDA注文成功: order_id=%s instrument=%s", order_id, instrument)
        return {
            "order_id": order_id,
            "instrument": instrument,
            "fill_id": fill_id,
            "filled_qty": filled_qty,
            "filled_price": filled_price,
        }
```

- [ ] **Step 4: テストが通ることを確認する**

```bash
uv run pytest tests/test_oanda_handler.py -v
```
期待: PASSED

- [ ] **Step 5: コミットする**

```bash
git add handlers/oanda_handler.py tests/test_oanda_handler.py
git commit -m "refactor: OandaHandler クラスに変換（OCP/DIP対応）"
```

---

### Task 3: MoomooHandler クラスへリファクタリングする

**Files:**
- Modify: `handlers/moomoo_handler.py`
- Modify: `tests/test_moomoo_handler.py`

- [ ] **Step 1: テストを先にリファクタリングする**

`tests/test_moomoo_handler.py` のインポートと呼び出しを更新する:

```python
# 変更前
from handlers.moomoo_handler import moomoo_order_handler

# 変更後
from handlers.moomoo_handler import MoomooHandler
```

全テストで `moomoo_order_handler(payload)` を `MoomooHandler().execute(payload)` に変更する。

- [ ] **Step 2: テストが失敗することを確認する**

```bash
uv run pytest tests/test_moomoo_handler.py -v
```
期待: `ImportError: cannot import name 'MoomooHandler'`

- [ ] **Step 3: `handlers/moomoo_handler.py` を `MoomooHandler` クラスに変換する**

既存の `moomoo_order_handler` 関数をクラスの `execute` メソッドに変換する。`_check_opend_connection`、`_get_trade_context` はモジュールレベル関数として残す。

```python
# handlers/moomoo_handler.py
"""moomoo証券（Futu OpenAPI）アダプター"""

import logging
import os
import socket
from typing import TYPE_CHECKING, Union

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from models import WebhookPayload

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import futu

try:
    import futu
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False
    logger.warning(
        "futu-api がインポートできません。moomoo注文は実行時に失敗します。"
    )


@retry(
    retry=retry_if_exception_type((OSError, socket.timeout)),
    wait=wait_fixed(2),
    stop=stop_after_attempt(3),
    reraise=True,
)
def _check_opend_connection(host: str, port: int) -> None:
    """OpenD への TCP 接続確認。"""
    with socket.create_connection((host, port), timeout=3):
        pass


def _get_trade_context(
    asset_class: str,
    host: str,
    port: int,
) -> "Union[futu.OpenUSTradeContext, futu.OpenHKTradeContext]":
    """asset_class に基づいてトレードコンテキストを返す。"""
    if asset_class.upper() == "HK":
        return futu.OpenHKTradeContext(host=host, port=port)
    return futu.OpenUSTradeContext(host=host, port=port)


class MoomooHandler:
    """moomoo証券（Futu OpenAPI）への注文を実行するハンドラー。"""

    def execute(self, payload: WebhookPayload) -> dict:
        """moomoo証券（Futu OpenAPI）へ注文を送信する。

        Raises:
            ImportError: futu-api が利用不可の場合
            ValueError: 必須設定が不足している場合
            RuntimeError: 注文APIがエラーを返した場合
        """
        if not FUTU_AVAILABLE:
            raise ImportError(
                "futu-api が利用できません。`uv add futu-api` でインストール後、OpenDを起動してください。"
            )

        host = os.getenv("MOOMOO_HOST", "127.0.0.1")
        try:
            port = int(os.getenv("MOOMOO_PORT", "11111"))
        except ValueError as e:
            raise ValueError("MOOMOO_PORT に不正な値が設定されています") from e

        trd_env_str = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").upper()
        trd_env_map = {
            "SIMULATE": futu.TrdEnv.SIMULATE,
            "REAL": futu.TrdEnv.REAL,
        }
        if trd_env_str not in trd_env_map:
            raise ValueError(
                f"MOOMOO_TRD_ENV は SIMULATE または REAL を指定してください（現在: {trd_env_str}）"
            )

        trd_env = trd_env_map[trd_env_str]
        trd_side = futu.TrdSide.BUY if payload.action == "buy" else futu.TrdSide.SELL

        logger.info(
            "moomoo注文開始: trd_env=%s ticker=%s action=%s qty=%s",
            trd_env_str, payload.ticker, payload.action, payload.quantity,
        )

        try:
            _check_opend_connection(host, port)
        except (OSError, socket.timeout) as e:
            logger.error("OpenD に接続できません (%s:%s): %s", host, port, e)
            raise RuntimeError(
                f"OpenD ({host}:{port}) が起動していません。先にOpenDを起動してください。"
            ) from e

        try:
            ctx = _get_trade_context(payload.asset_class, host, port)
            with ctx:
                ret_code, data = ctx.place_order(
                    price=0,
                    qty=payload.quantity,
                    code=payload.ticker,
                    trd_side=trd_side,
                    order_type=futu.OrderType.MARKET,
                    trd_env=trd_env,
                )

                if ret_code != futu.RET_OK:
                    logger.error(
                        "moomoo注文失敗: ticker=%s ret_code=%s data=%s",
                        payload.ticker, ret_code, data,
                    )
                    raise RuntimeError(f"moomoo注文エラー: {data}")

                try:
                    if hasattr(data, "empty") and not data.empty and "order_id" in data.columns:
                        order_id = str(data["order_id"].iloc[0])
                    else:
                        order_id = str(data)
                        logger.warning("order_idの取得に失敗。レスポンス全体を使用: %s", data)
                except (AttributeError, KeyError, IndexError) as e:
                    logger.warning("order_idのパース失敗: %s。レスポンス全体を使用。", e)
                    order_id = str(data)

                filled_qty = None
                filled_price = None
                if hasattr(data, "empty") and not data.empty:
                    if "dealt_qty" in data.columns:
                        filled_qty = float(data["dealt_qty"].iloc[0])
                    if "dealt_avg_price" in data.columns:
                        filled_price = float(data["dealt_avg_price"].iloc[0])

                logger.info("moomoo注文成功: order_id=%s", order_id)
                return {
                    "order_id": order_id,
                    "ret_code": ret_code,
                    "filled_qty": filled_qty,
                    "filled_price": filled_price,
                }

        except Exception as e:
            if isinstance(e, (ImportError, ValueError, RuntimeError)):
                raise
            logger.error("moomoo注文で予期しないエラー: ticker=%s error=%s", payload.ticker, e)
            raise RuntimeError(f"moomoo注文失敗: {e}") from e
```

- [ ] **Step 4: `handlers/__init__.py` を更新する**

```python
# handlers/__init__.py
from .base import BrokerHandler
from .oanda_handler import OandaHandler
from .moomoo_handler import MoomooHandler

__all__ = ["BrokerHandler", "OandaHandler", "MoomooHandler"]
```

- [ ] **Step 5: テストが通ることを確認する**

```bash
uv run pytest tests/test_moomoo_handler.py tests/test_oanda_handler.py -v
```
期待: PASSED

- [ ] **Step 6: コミットする**

```bash
git add handlers/moomoo_handler.py handlers/__init__.py tests/test_moomoo_handler.py
git commit -m "refactor: MoomooHandler クラスに変換、handlers/__init__.py を更新"
```

---

### Task 4: OrderRouter サービスを作成する

**Files:**
- Create: `services/__init__.py`
- Create: `services/order_service.py`
- Create: `tests/test_order_service.py`

- [ ] **Step 1: テストファイルを先に作成する**

```python
# tests/test_order_service.py
"""OrderRouter の単体テスト"""
from unittest.mock import MagicMock

import pytest

from models import WebhookPayload
from services.order_service import OrderRouter, build_default_router


def _make_payload(**kwargs) -> WebhookPayload:
    defaults = {
        "passphrase": "test",
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
    }
    return WebhookPayload(**(defaults | kwargs))


class TestOrderRouter:
    def test_route_dispatches_to_correct_handler(self):
        mock_handler = MagicMock()
        mock_handler.execute.return_value = {"order_id": "123"}
        router = OrderRouter({"oanda": mock_handler})
        payload = _make_payload(broker="oanda")

        result = router.route(payload)

        mock_handler.execute.assert_called_once_with(payload)
        assert result == {"order_id": "123"}

    def test_route_raises_for_unknown_broker(self):
        router = OrderRouter({})
        # WebhookPayload はバリデーションで "oanda"/"moomoo" のみ許可するが、
        # ルーターは登録済みブローカーにのみ委譲する
        mock_payload = MagicMock()
        mock_payload.broker = "unknown_broker"
        with pytest.raises(ValueError, match="未対応ブローカー"):
            router.route(mock_payload)

    def test_route_moomoo_dispatches_to_moomoo_handler(self):
        mock_oanda = MagicMock()
        mock_moomoo = MagicMock()
        mock_moomoo.execute.return_value = {"order_id": "456"}
        router = OrderRouter({"oanda": mock_oanda, "moomoo": mock_moomoo})
        payload = _make_payload(broker="moomoo")

        result = router.route(payload)

        mock_moomoo.execute.assert_called_once_with(payload)
        mock_oanda.execute.assert_not_called()
        assert result == {"order_id": "456"}

    def test_build_default_router_returns_router_with_oanda_and_moomoo(self):
        router = build_default_router()
        assert "oanda" in router._handlers
        assert "moomoo" in router._handlers
```

- [ ] **Step 2: テストが失敗することを確認する**

```bash
uv run pytest tests/test_order_service.py -v
```
期待: `ModuleNotFoundError: No module named 'services'`

- [ ] **Step 3: `services/__init__.py` と `services/order_service.py` を作成する**

```python
# services/__init__.py
```

```python
# services/order_service.py
"""注文ルーティングサービス"""
from handlers.base import BrokerHandler
from handlers.oanda_handler import OandaHandler
from handlers.moomoo_handler import MoomooHandler
from models import WebhookPayload


class OrderRouter:
    """ブローカー名を元に適切なハンドラーへ注文をルーティングする。
    
    新しいブローカーを追加するには:
    1. `handlers/` に新しい `XxxHandler` クラスを作成する
    2. `build_default_router()` に登録する
    3. `webhook_server.py` は変更不要
    """

    def __init__(self, handlers: dict[str, BrokerHandler]) -> None:
        self._handlers = handlers

    def route(self, payload: WebhookPayload) -> dict:
        """payloadのブローカーに対応するハンドラーへ委譲する。
        
        Raises:
            ValueError: 未登録のブローカーの場合
        """
        handler = self._handlers.get(payload.broker)
        if handler is None:
            raise ValueError(f"未対応ブローカー: {payload.broker}")
        return handler.execute(payload)


def build_default_router() -> OrderRouter:
    """デフォルトのブローカーハンドラーを登録したルーターを返す。"""
    return OrderRouter({
        "oanda": OandaHandler(),
        "moomoo": MoomooHandler(),
    })
```

- [ ] **Step 4: テストが通ることを確認する**

```bash
uv run pytest tests/test_order_service.py -v
```
期待: PASSED

- [ ] **Step 5: コミットする**

```bash
git add services/__init__.py services/order_service.py tests/test_order_service.py
git commit -m "feat: OrderRouter サービスを追加（Strategy/OCP パターン）"
```

---

### Task 5: FillEventService を作成する

**Files:**
- Create: `services/fill_service.py`
- Create: `tests/test_fill_service.py`

- [ ] **Step 1: テストファイルを先に作成する**

```python
# tests/test_fill_service.py
"""FillEventService の単体テスト"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from models import FillEvent, WebhookPayload
from services.fill_service import FillEventService


def _make_payload(**kwargs) -> WebhookPayload:
    defaults = {
        "passphrase": "test",
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
    }
    return WebhookPayload(**(defaults | kwargs))


def _make_fill_event(**kwargs) -> FillEvent:
    defaults = {
        "event_id": "evt_001",
        "signal_id": "sig_001",
        "order_id": "ord_001",
        "fill_id": "fill_001",
        "occurred_at": datetime(2026, 1, 1, 9, 0),
        "broker": "oanda",
        "asset_class": "FX",
        "action": "buy",
        "ticker": "USDJPY",
        "quantity": 1000.0,
        "filled_qty": 1000.0,
        "filled_price": 150.0,
        "trade_id": "trd_001",
        "run_mode": "live",
    }
    return FillEvent(**(defaults | kwargs))


class TestFillEventServiceBuild:
    def test_build_returns_none_when_filled_qty_missing(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        payload = _make_payload()
        result = svc.build(
            payload=payload,
            result={},
            signal_id="sig_001",
            internal_order_id="ord_001",
            broker_order_id=None,
        )
        assert result is None

    def test_build_returns_fill_event_when_filled_qty_and_price_present(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        payload = _make_payload()
        result = svc.build(
            payload=payload,
            result={"filled_qty": 1000.0, "filled_price": 150.0},
            signal_id="sig_001",
            internal_order_id="ord_001",
            broker_order_id="brk_001",
        )
        assert result is not None
        assert result.filled_qty == 1000.0
        assert result.filled_price == 150.0
        assert result.broker_order_id == "brk_001"


class TestFillEventServiceAllocate:
    def test_allocate_returns_original_when_no_recent_fills(self):
        mock_logger = MagicMock()
        mock_logger.load_events.return_value = []
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(action="sell")

        result = svc.allocate(fill)

        assert len(result) == 1
        assert result[0] is fill

    def test_allocate_returns_original_for_unknown_broker(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(broker="ig")  # type: ignore[arg-type]

        result = svc.allocate(fill)

        assert len(result) == 1
        assert result[0] is fill
        mock_logger.load_events.assert_not_called()


class TestFillEventServiceBuildTradeClosed:
    def test_returns_none_when_trade_id_is_none(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(trade_id=None)

        result = svc.build_trade_closed(fill)

        assert result is None

    def test_returns_none_when_broker_not_supported(self):
        mock_logger = MagicMock()
        svc = FillEventService(mock_logger)
        fill = _make_fill_event(broker="ig")  # type: ignore[arg-type]

        result = svc.build_trade_closed(fill)

        assert result is None
```

- [ ] **Step 2: テストが失敗することを確認する**

```bash
uv run pytest tests/test_fill_service.py -v
```
期待: `ModuleNotFoundError: No module named 'services.fill_service'`

- [ ] **Step 3: `services/fill_service.py` を作成する（`webhook_server.py` のロジックを移植）**

```python
# services/fill_service.py
"""FillEvent 構築・配分・TradeClosedイベント生成サービス"""
from datetime import datetime
from typing import TYPE_CHECKING

from models import FillEvent, TradeClosedEvent, WebhookPayload

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
```

- [ ] **Step 4: テストが通ることを確認する**

```bash
uv run pytest tests/test_fill_service.py -v
```
期待: PASSED

- [ ] **Step 5: コミットする**

```bash
git add services/fill_service.py tests/test_fill_service.py
git commit -m "feat: FillEventService を抽出（SRP対応）"
```

---

### Task 6: webhook_server.py をリファクタリングして全テストを通す

**Files:**
- Modify: `webhook_server.py`
- Modify: `tests/test_webhook_server.py`（最小限の修正のみ）

- [ ] **Step 1: `webhook_server.py` を `OrderRouter`/`FillEventService` を使う形に書き換える**

```python
# webhook_server.py
"""TradingView Webhook サーバー"""

import hmac
import logging
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from event_logger import JsonlEventLogger
from models import (
    EventIngestResult,
    OrderEvent,
    OrderResult,
    SignalEvent,
    TradeClosedEvent,
    TradeClosedPayload,
    WebhookPayload,
)
from services.fill_service import FillEventService, _generate_id
from services.order_service import OrderRouter, build_default_router

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)
event_logger = JsonlEventLogger()


def _verify_passphrase(passphrase: str) -> None:
    expected_passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not hmac.compare_digest(passphrase, expected_passphrase):
        logger.warning("不正なパスフレーズでアクセスがありました")
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """起動時に必須環境変数を検証し、サービスを初期化する。"""
    passphrase = os.getenv("WEBHOOK_PASSPHRASE", "")
    if not passphrase:
        logger.critical("WEBHOOK_PASSPHRASE が設定されていません。サーバーを起動できません。")
        sys.exit(1)
    app.state.order_router = build_default_router()
    app.state.fill_service = FillEventService(event_logger)
    logger.info("Alpha-Strike Webhook サーバー起動完了")
    yield
    logger.info("Alpha-Strike Webhook サーバー停止")


app = FastAPI(
    title="Alpha-Strike Webhook Server",
    description="TradingViewアラートをOANDA証券・moomoo証券へ自動ルーティングするWebhookサーバー",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.post("/webhook", response_model=OrderResult, status_code=200)
@limiter.limit("10/minute")
async def receive_webhook(
    request: Request, payload: WebhookPayload
) -> OrderResult:
    """TradingViewからのWebhookを受け取り、指定ブローカーへ注文を送信する。"""
    _verify_passphrase(payload.passphrase)

    order_router: OrderRouter = request.app.state.order_router
    fill_service: FillEventService = request.app.state.fill_service

    signal_id = payload.signal_id or _generate_id("sig")
    signal_event = SignalEvent(
        event_id=_generate_id("evt"),
        signal_id=signal_id,
        occurred_at=datetime.now(),
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        timeframe=payload.timeframe,
        alert_timestamp=payload.alert_timestamp,
        run_mode=payload.run_mode,
        alert_name=payload.alert_name,
    )
    event_logger.append(signal_event)

    logger.info(
        "Webhook受信: broker=%s ticker=%s action=%s qty=%s",
        payload.broker, payload.ticker, payload.action, payload.quantity,
    )

    started_at = perf_counter()
    internal_order_id = _generate_id("ord")

    try:
        result = order_router.route(payload)

        latency_ms = int((perf_counter() - started_at) * 1000)
        _oid = result.get("order_id") if isinstance(result, dict) else None
        broker_order_id = str(_oid) if _oid is not None else None
        order_event = OrderEvent(
            event_id=_generate_id("evt"),
            signal_id=signal_id,
            order_id=internal_order_id,
            occurred_at=datetime.now(),
            broker=payload.broker,
            asset_class=payload.asset_class,
            action=payload.action,
            ticker=payload.ticker,
            quantity=payload.quantity,
            status="accepted",
            request_latency_ms=latency_ms,
            broker_order_id=broker_order_id,
            strategy_id=payload.strategy_id,
            strategy_version=payload.strategy_version,
            snapshot_id=payload.snapshot_id,
            run_mode=payload.run_mode,
        )
        event_logger.append(order_event)

        fill_event = fill_service.build(
            payload=payload,
            result=result if isinstance(result, dict) else {},
            signal_id=signal_id,
            internal_order_id=internal_order_id,
            broker_order_id=broker_order_id,
        )
        if fill_event is not None:
            for allocated_fill_event in fill_service.allocate(fill_event):
                event_logger.append(allocated_fill_event)
                trade_closed_event = fill_service.build_trade_closed(allocated_fill_event)
                if trade_closed_event is not None:
                    event_logger.append(trade_closed_event)

        logger.info(
            "注文成功: broker=%s ticker=%s action=%s qty=%s",
            payload.broker, payload.ticker, payload.action, payload.quantity,
        )
        return OrderResult(
            status="success",
            broker=payload.broker,
            ticker=payload.ticker,
            message=str(result),
            signal_id=signal_id,
            order_id=internal_order_id,
            broker_order_id=broker_order_id,
            event_id=order_event.event_id,
        )

    except HTTPException:
        raise
    except (ValueError, ImportError) as e:
        latency_ms = int((perf_counter() - started_at) * 1000)
        event_logger.append(
            OrderEvent(
                event_id=_generate_id("evt"),
                signal_id=signal_id,
                order_id=internal_order_id,
                occurred_at=datetime.now(),
                broker=payload.broker,
                asset_class=payload.asset_class,
                action=payload.action,
                ticker=payload.ticker,
                quantity=payload.quantity,
                status="failed",
                request_latency_ms=latency_ms,
                strategy_id=payload.strategy_id,
                strategy_version=payload.strategy_version,
                snapshot_id=payload.snapshot_id,
                run_mode=payload.run_mode,
                error_type=type(e).__name__,
            )
        )
        logger.error("設定エラー: %s", e)
        raise HTTPException(
            status_code=500,
            detail="設定エラーが発生しました。管理者にお問い合わせください。",
        ) from e
    except Exception as e:
        latency_ms = int((perf_counter() - started_at) * 1000)
        event_logger.append(
            OrderEvent(
                event_id=_generate_id("evt"),
                signal_id=signal_id,
                order_id=internal_order_id,
                occurred_at=datetime.now(),
                broker=payload.broker,
                asset_class=payload.asset_class,
                action=payload.action,
                ticker=payload.ticker,
                quantity=payload.quantity,
                status="failed",
                request_latency_ms=latency_ms,
                strategy_id=payload.strategy_id,
                strategy_version=payload.strategy_version,
                snapshot_id=payload.snapshot_id,
                run_mode=payload.run_mode,
                error_type=type(e).__name__,
            )
        )
        logger.error(
            "注文失敗: broker=%s ticker=%s error=%s",
            payload.broker, payload.ticker, e, exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail="注文の実行に失敗しました。しばらくしてから再試行してください。",
        ) from e


@app.post("/events/trade-closed", response_model=EventIngestResult, status_code=200)
@limiter.limit("30/minute")
async def ingest_trade_closed_event(
    request: Request, payload: TradeClosedPayload
) -> EventIngestResult:
    """broker poller / callback 由来の trade_closed を保存する。"""
    _verify_passphrase(payload.passphrase)

    event = TradeClosedEvent(
        event_id=_generate_id("evt"),
        signal_id=payload.signal_id,
        trade_id=payload.trade_id,
        occurred_at=datetime.now(),
        closed_at=payload.closed_at,
        broker=payload.broker,
        asset_class=payload.asset_class,
        action=payload.action,
        ticker=payload.ticker,
        quantity=payload.quantity,
        entry_price=payload.entry_price,
        exit_price=payload.exit_price,
        gross_pnl=payload.gross_pnl,
        net_pnl=payload.net_pnl,
        strategy_id=payload.strategy_id,
        strategy_version=payload.strategy_version,
        snapshot_id=payload.snapshot_id,
        run_mode=payload.run_mode,
        commission=payload.commission,
        exit_reason=payload.exit_reason,
    )
    event_logger.append(event)

    logger.info(
        "trade_closed 保存: broker=%s ticker=%s trade_id=%s",
        payload.broker, payload.ticker, payload.trade_id,
    )
    return EventIngestResult(
        status="accepted",
        event_id=event.event_id,
        message="trade_closed event recorded",
    )


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict:
    checks: dict[str, dict] = {}

    oanda_key = os.getenv("OANDA_API_KEY", "")
    oanda_account = os.getenv("OANDA_ACCOUNT_ID", "")
    if oanda_key and oanda_account:
        checks["oanda"] = {"status": "ok"}
    else:
        logger.warning(
            "OANDA 設定が不完全です: API_KEY=%s ACCOUNT_ID=%s",
            bool(oanda_key), bool(oanda_account),
        )
        checks["oanda"] = {"status": "error", "detail": "OANDA の設定が不完全です"}

    moomoo_host = os.getenv("MOOMOO_HOST", "127.0.0.1")
    try:
        moomoo_port = int(os.getenv("MOOMOO_PORT", "11111"))
    except ValueError:
        checks["moomoo"] = {"status": "error", "detail": "MOOMOO_PORT が不正な値です"}
        moomoo_port = None  # type: ignore[assignment]

    if moomoo_port is not None:
        try:
            with socket.create_connection((moomoo_host, moomoo_port), timeout=3):
                pass
            checks["moomoo"] = {"status": "ok"}
        except (OSError, socket.timeout) as e:
            logger.warning("OpenD への接続確認に失敗: %s", e)
            checks["moomoo"] = {"status": "error", "detail": "OpenD に接続できません"}

    all_ok = all(c["status"] == "ok" for c in checks.values())
    status = "ready" if all_ok else "degraded"
    http_status = 200 if all_ok else 503

    return JSONResponse(
        status_code=http_status, content={"status": status, "checks": checks}
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host="0.0.0.0", port=8080, reload=False)
```

- [ ] **Step 2: 全テストが通ることを確認する**

```bash
uv run pytest tests/ -v
```
期待: PASSED（既存の `test_webhook_server.py` のHTTPテストはそのまま通過）

- [ ] **Step 3: Linter を実行して問題がないことを確認する**

```bash
uv run ruff check .
```
期待: No errors

- [ ] **Step 4: コミットする**

```bash
git add webhook_server.py
git commit -m "refactor: webhook_server を薄いHTTPレイヤーに整理（SRP/OCP対応完了）"
```
