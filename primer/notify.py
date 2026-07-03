"""Plain Telegram notifications for prime/scheduler paths and the CLI.

Kept deliberately synchronous and exception-free at the boundary:
notifications must never crash priming.
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from primer.settings import Config


def _is_transient(exc: BaseException) -> bool:
    """Retry network blips / 5xx; a definitive Telegram 4xx answer is final."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(
        exc, urllib.error.URLError | TimeoutError | ConnectionError | http.client.HTTPException
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=5.0),
    retry=retry_if_exception(_is_transient),
)
def tg_api(token: str, method: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:  # noqa: S310 - fixed https:// Telegram API host
        return cast("dict[str, Any]", json.loads(resp.read().decode()))


def send_notification(cfg: Config, text: str, chat_id: str | int | None = None) -> bool:
    token = cfg.telegram_token
    chat_id = chat_id or cfg.telegram_chat_id
    if not token or not chat_id:
        logger.info("Telegram not configured (no token/chat_id) - skipping notification")
        return False
    try:
        res = tg_api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001 - notifications must never crash priming
        logger.error(f"Telegram send failed: {e}")
        return False
    if not res.get("ok"):
        logger.warning(f"Telegram sendMessage ok=false: {res}")
    return bool(res.get("ok", False))
