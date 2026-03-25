"""IG証券 REST API アダプター

デモ口座: https://demo-api.ig.com/gateway/deal
ライブ口座: https://api.ig.com/gateway/deal

テスト時は必ず IG_ACC_TYPE=DEMO を設定してください。
"""

import logging
import os

import requests

from models import WebhookPayload

logger = logging.getLogger(__name__)

_BASE_URLS = {
    "DEMO": "https://demo-api.ig.com/gateway/deal",
    "LIVE": "https://api.ig.com/gateway/deal",
}


def _ig_login(base_url: str, api_key: str, username: str, password: str) -> tuple[str, str]:
    """IG APIへログインし、CST と X-SECURITY-TOKEN を返す。"""
    url = f"{base_url}/session"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "Version": "2",
    }
    body = {
        "identifier": username,
        "password": password,
    }

    response = requests.post(url, json=body, headers=headers, timeout=10)
    response.raise_for_status()

    cst = response.headers.get("CST", "")
    security_token = response.headers.get("X-SECURITY-TOKEN", "")

    if not cst or not security_token:
        raise ValueError("IG APIログインレスポンスにセッショントークンが含まれていません")

    logger.info("IG APIログイン成功")
    return cst, security_token


def ig_order_handler(payload: WebhookPayload) -> dict:
    """IG証券へ注文を送信する。

    Args:
        payload: Webhookペイロード（broker="ig" 前提）

    Returns:
        APIレスポンスのJSON dict

    Raises:
        ValueError: 必須環境変数が未設定の場合
        requests.HTTPError: APIがエラーを返した場合
    """
    api_key = os.getenv("IG_API_KEY", "")
    username = os.getenv("IG_USERNAME", "")
    password = os.getenv("IG_PASSWORD", "")
    acc_type = os.getenv("IG_ACC_TYPE", "DEMO").upper()

    missing = [k for k, v in {
        "IG_API_KEY": api_key,
        "IG_USERNAME": username,
        "IG_PASSWORD": password,
    }.items() if not v]
    if missing:
        raise ValueError(f"IG証券の設定が不足しています: {missing}")

    if acc_type not in _BASE_URLS:
        raise ValueError(f"IG_ACC_TYPE は DEMO または LIVE を指定してください（現在: {acc_type}）")

    base_url = _BASE_URLS[acc_type]
    logger.info("IG注文開始: acc_type=%s ticker=%s action=%s qty=%s",
                acc_type, payload.ticker, payload.action, payload.quantity)

    try:
        cst, security_token = _ig_login(base_url, api_key, username, password)
    except requests.RequestException as e:
        logger.error("IGログイン失敗: %s", e)
        raise

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "X-IG-API-KEY": api_key,
        "Version": "2",
        "CST": cst,
        "X-SECURITY-TOKEN": security_token,
    }

    order_body = {
        "epic": payload.ticker,
        "direction": payload.action.upper(),  # "BUY" or "SELL"
        "size": payload.quantity,
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": "USD",
    }

    try:
        url = f"{base_url}/positions/otc"
        response = requests.post(url, json=order_body, headers=headers, timeout=10)
        response.raise_for_status()
        result = response.json()
        logger.info("IG注文成功: dealReference=%s", result.get("dealReference"))
        return result
    except requests.RequestException as e:
        logger.error("IG注文失敗: ticker=%s error=%s", payload.ticker, e)
        raise
