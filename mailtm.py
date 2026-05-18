# mailtm.py — تكامل Mail.tm API
import asyncio
import logging
import re
import secrets
import string

import aiohttp

from config import MAILTM_API_BASE

logger = logging.getLogger(__name__)

CODE_PATTERN = re.compile(r"\b\d{5,6}\b")

# حد المعدل: 8 طلبات/ثانية — نترك هامشاً
_request_lock = asyncio.Lock()
_last_request_at = 0.0
_MIN_INTERVAL = 0.15


async def _throttle():
    global _last_request_at
    async with _request_lock:
        now = asyncio.get_running_loop().time()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = asyncio.get_running_loop().time()


async def _request(method: str, path: str, json_data=None, token: str = None) -> dict | list | None:
    await _throttle()
    headers = {"Accept": "application/ld+json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{MAILTM_API_BASE.rstrip('/')}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=json_data, headers=headers) as resp:
            if resp.status == 204:
                return None
            try:
                body = await resp.json()
            except Exception:
                body = {}
            if resp.status >= 400:
                detail = body.get("detail") or body.get("message") or str(body)
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=resp.status, message=str(detail)
                )
            return body


async def get_active_domains() -> list[str]:
    """جلب النطاقات النشطة من Mail.tm."""
    data = await _request("GET", "/domains")
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    domains = []
    for item in members:
        domain = item.get("domain")
        if domain and item.get("isActive", True):
            domains.append(domain)
    if not domains:
        raise RuntimeError("لا توجد نطاقات نشطة في Mail.tm")
    return domains


def _random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _random_password(length: int = 16) -> str:
    return secrets.token_urlsafe(length)[:length]


async def create_account(domain: str | None = None) -> dict:
    """
    إنشاء حساب Mail.tm جديد.
    يعيد: {"address": "...", "password": "..."}
    """
    domains = await get_active_domains()
    use_domain = domain or domains[0]
    address = f"{_random_local_part()}@{use_domain}"
    password = _random_password()
    await _request("POST", "/accounts", {"address": address, "password": password})
    return {"address": address, "password": password}


async def get_token(address: str, password: str) -> str:
    data = await _request("POST", "/token", {"address": address, "password": password})
    token = data.get("token")
    if not token:
        raise RuntimeError("فشل الحصول على توكن Mail.tm")
    return token


async def _extract_code_from_messages(token: str, seen_ids: set[str]) -> str | None:
    data = await _request("GET", "/messages", token=token)
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    for msg in members:
        msg_id = msg.get("id")
        if not msg_id or msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)
        # intro قد يحتوي الكود مباشرة
        intro = msg.get("intro") or ""
        match = CODE_PATTERN.search(intro)
        if match:
            return match.group(0)
        # جلب النص الكامل
        try:
            full = await _request("GET", f"/messages/{msg_id}", token=token)
            text = full.get("text") or ""
            match = CODE_PATTERN.search(text)
            if match:
                return match.group(0)
        except Exception as e:
            logger.debug("mailtm message fetch: %s", e)
    return None


async def fetch_code(
    address: str,
    password: str,
    attempts: int = 12,
    interval: int = 5,
) -> str | None:
    """Polling لكود تيليجرام من صندوق Mail.tm."""
    token = await get_token(address, password)
    seen_ids: set[str] = set()
    for _ in range(attempts):
        await asyncio.sleep(interval)
        code = await _extract_code_from_messages(token, seen_ids)
        if code:
            return code
    return None
