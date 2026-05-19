# mailtm.py — تكامل Mail.tm API
import asyncio
import logging
import re
import secrets
import string

import aiohttp

from config import MAILTM_API_BASE

logger = logging.getLogger(__name__)

CODE_PATTERN = re.compile(r"\b(\d{5,6})\b")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

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


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "")


def _pick_code_from_text(*chunks: str) -> str | None:
    for chunk in chunks:
        if not chunk:
            continue
        plain = _strip_html(str(chunk))
        match = CODE_PATTERN.search(plain)
        if match:
            digits = match.group(1)
            return digits[:5] if len(digits) >= 5 else digits
    return None


async def snapshot_message_ids(address: str, password: str) -> set[str]:
    """معرّفات الرسائل الحالية — لتجاهل أكواد قديمة عند الإنعاش."""
    token = await get_token(address, password)
    data = await _request("GET", "/messages", token=token)
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    return {str(m["id"]) for m in members if m.get("id")}


async def _extract_code_from_message(token: str, msg: dict) -> str | None:
    intro = msg.get("intro") or ""
    subject = msg.get("subject") or ""
    code = _pick_code_from_text(intro, subject)
    if code:
        return code
    msg_id = msg.get("id")
    if not msg_id:
        return None
    try:
        full = await _request("GET", f"/messages/{msg_id}", token=token)
        if not full:
            return None
        return _pick_code_from_text(
            full.get("text") or "",
            full.get("html") or "",
            full.get("intro") or "",
        )
    except Exception as e:
        logger.debug("mailtm message fetch %s: %s", msg_id, e)
        return None


async def _poll_new_messages(
    token: str, exclude_ids: set[str], processed_ids: set[str]
) -> str | None:
    data = await _request("GET", "/messages", token=token)
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    for msg in members:
        msg_id = str(msg.get("id") or "")
        if not msg_id or msg_id in exclude_ids or msg_id in processed_ids:
            continue
        processed_ids.add(msg_id)
        code = await _extract_code_from_message(token, msg)
        if code:
            logger.info("mailtm code found in message %s", msg_id)
            return code
    return None


async def fetch_code(
    address: str,
    password: str,
    attempts: int = 36,
    interval: int = 5,
    exclude_ids: set[str] | None = None,
) -> str | None:
    """
    انتظار كود تيليجرام الجديد فقط (بعد exclude_ids).
    أول فحص فوري ثم كل interval ثانية.
    """
    token = await get_token(address, password)
    exclude = {str(i) for i in (exclude_ids or set())}
    processed: set[str] = set()
    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(interval)
        code = await _poll_new_messages(token, exclude, processed)
        if code:
            return code
    return None


async def verify_mailbox(address: str, password: str) -> bool:
    try:
        await get_token(address, password)
        return True
    except Exception as e:
        logger.debug("mailbox verify %s: %s", address, e)
        return False
