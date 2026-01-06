#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
from typing import Any

import requests


logger = logging.getLogger(__name__)


def send_telegram_message(
    message: str,
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str | None = None,
    timeout: int = 15,
) -> dict[str, Any] | None:
    token = (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    target = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target:
        logger.warning("Telegram token/chat_id not configured.")
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": target, "text": message}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    proxies = _get_proxies()
    try:
        response = requests.post(url, json=payload, timeout=timeout, proxies=proxies)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.warning("Telegram send failed: %s", exc)
        return None


def _get_proxies() -> dict[str, str] | None:
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    send_telegram_message("Telegram integration check.")
