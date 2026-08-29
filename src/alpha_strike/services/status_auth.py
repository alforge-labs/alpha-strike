"""read-only status API の Bearer トークン認証 (issue #57 Phase 1)。

二重防御の「コード層」を担う:

- ``STATUS_API_TOKEN`` 未設定時はエンドポイントを **503 で無効化**（fail-safe。
  デフォルトでは機微な口座情報を公開しない）。
- 設定時は ``Authorization: Bearer <token>`` を必須化し、定数時間比較で検証する。

ネットワーク層（Cloudflare Access 等）と併用する前提だが、Cloudflare 未設定でも
本トークン認証によりエンドポイントが無防備に晒されないようにする。
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException


async def require_status_token(authorization: str | None = Header(default=None)) -> None:
    """status 系エンドポイントの FastAPI 依存。検証失敗時に HTTPException を送出する。"""
    token = os.getenv("STATUS_API_TOKEN", "")
    if not token:
        # fail-safe: トークン未設定なら status API はデフォルト無効
        raise HTTPException(
            status_code=503,
            detail="status API is disabled (STATUS_API_TOKEN is not set)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len("Bearer ") :]
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="invalid token")
