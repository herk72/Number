# mailtm.py — تكامل Mail.tm API
import asyncio
import logging
import re
import secrets
import string

import aiohttp

from config import MAILTM_API_BASE

logger = logging.getLogger(__name__)

CODE_PATTERN = re.compile(r"\b(\d{5,7})\b")
_TELEGRAM_CODE_HINT = re.compile(
    r"(?:telegram|login|verification|confirm)[^\d]{0,60}(\d{5,7})",
    re.IGNORECASE,
)
_HYPHENATED_CODE = re.compile(r"\b(\d(?:\s*-\s*\d){4,6})\b")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_request_lock = asyncio.Lock()
_last_request_at = 0.0
_MIN_INTERVAL = 0.15

# حد أقصى للانتظار عند 429 (بالثواني)
_MAILTM_429_BASE_WAIT = 5.0
_MAILTM_429_MAX_WAIT  = 120.0
_MAILTM_MAX_RETRIES   = 6


async def _throttle():
    global _last_request_at
    async with _request_lock:
        now = asyncio.get_running_loop().time()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = asyncio.get_running_loop().time()


async def _request(method: str, path: str, json_data=None, token: str = None) -> dict | list | None:
    headers = {"Accept": "application/ld+json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{MAILTM_API_BASE.rstrip('/')}{path}"

    wait = _MAILTM_429_BASE_WAIT
    for attempt in range(_MAILTM_MAX_RETRIES):
        await _throttle()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, json=json_data, headers=headers) as resp:
                    if resp.status == 204:
                        return None
                    try:
                        body = await resp.json()
                    except Exception:
                        body = {}
                    if resp.status == 429:
                        # معالجة Rate Limit — انتظر وأعد المحاولة
                        retry_after = float(
                            resp.headers.get("Retry-After", wait)
                        )
                        actual_wait = min(max(retry_after, wait), _MAILTM_429_MAX_WAIT)
                        logger.warning(
                            "mailtm 429 on %s %s — retry in %.1fs (attempt %d/%d)",
                            method, path, actual_wait, attempt + 1, _MAILTM_MAX_RETRIES,
                        )
                        await asyncio.sleep(actual_wait)
                        wait = min(wait * 2, _MAILTM_429_MAX_WAIT)
                        continue
                    if resp.status >= 400:
                        detail = body.get("detail") or body.get("message") or str(body)
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history,
                            status=resp.status, message=str(detail),
                        )
                    return body
        except aiohttp.ClientResponseError:
            raise
        except aiohttp.ClientConnectorError as e:
            logger.warning("mailtm connection error (attempt %d): %s", attempt + 1, e)
            if attempt + 1 >= _MAILTM_MAX_RETRIES:
                raise
            await asyncio.sleep(wait)
            wait = min(wait * 2, _MAILTM_429_MAX_WAIT)
    # لو وصلنا هنا كل المحاولات فشلت بسبب 429
    raise aiohttp.ClientResponseError(
        None, [], status=429, message="mailtm: too many requests after retries"
    )


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


def _code_ok(digits: str, length: int | None) -> bool:
    if len(digits) < 5 or len(digits) > 7:
        return False
    if length is not None and len(digits) != length:
        return False
    return True


def extract_codes_from_text(*chunks: str, length: int | None = None) -> list[str]:
    """استخراج أكواد محتملة — الأقرب لطول تيليجرام أولاً."""
    plain = _strip_html(" ".join(str(c) for c in chunks if c))
    if not plain:
        return []

    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        digits = re.sub(r"\D", "", raw)
        if not _code_ok(digits, length) or digits in seen:
            return
        seen.add(digits)
        ordered.append(digits)

    for match in _TELEGRAM_CODE_HINT.finditer(plain):
        add(match.group(1))
    for match in _HYPHENATED_CODE.finditer(plain):
        add(match.group(1))
    for match in CODE_PATTERN.finditer(plain):
        add(match.group(1))

    if length is not None:
        exact = [c for c in ordered if len(c) == length]
        rest = [c for c in ordered if len(c) != length]
        return exact + rest
    return ordered


def _pick_code_from_text(*chunks: str, length: int | None = None) -> str | None:
    codes = extract_codes_from_text(*chunks, length=length)
    return codes[0] if codes else None


async def snapshot_message_ids(address: str, password: str) -> set[str]:
    """معرّفات الرسائل الحالية — لتجاهل أكواد قديمة عند الإنعاش."""
    token = await get_token(address, password)
    data = await _request("GET", "/messages", token=token)
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    return {str(m["id"]) for m in members if m.get("id")}


async def _extract_codes_from_message(
    token: str, msg: dict, length: int | None = None
) -> list[str]:
    intro = msg.get("intro") or ""
    subject = msg.get("subject") or ""
    codes = extract_codes_from_text(intro, subject, length=length)
    if codes:
        return codes
    msg_id = msg.get("id")
    if not msg_id:
        return []
    try:
        full = await _request("GET", f"/messages/{msg_id}", token=token)
        if not full:
            return []
        return extract_codes_from_text(
            full.get("text") or "",
            full.get("html") or "",
            full.get("intro") or "",
            length=length,
        )
    except Exception as e:
        logger.debug("mailtm message fetch %s: %s", msg_id, e)
        return []


async def _poll_new_messages(
    token: str,
    exclude_ids: set[str],
    processed_ids: set[str],
    length: int | None = None,
) -> list[str]:
    data = await _request("GET", "/messages", token=token)
    members = data.get("hydra:member", []) if isinstance(data, dict) else []
    for msg in members:
        msg_id = str(msg.get("id") or "")
        if not msg_id or msg_id in exclude_ids or msg_id in processed_ids:
            continue
        processed_ids.add(msg_id)
        codes = await _extract_codes_from_message(token, msg, length=length)
        if codes:
            logger.info("mailtm codes in message %s: %s", msg_id, codes)
            return codes
    return []


async def fetch_codes(
    address: str,
    password: str,
    attempts: int = 36,
    interval: int = 5,
    exclude_ids: set[str] | None = None,
    code_length: int | None = None,
) -> list[str]:
    """
    انتظار رسالة جديدة وإرجاع كل الأكواد المحتملة (الأدق أولاً).
    """
    token = await get_token(address, password)
    exclude = {str(i) for i in (exclude_ids or set())}
    processed: set[str] = set()
    for attempt in range(attempts):
        if attempt > 0:
            await asyncio.sleep(interval)
        codes = await _poll_new_messages(token, exclude, processed, length=code_length)
        if codes:
            return codes
    return []


async def fetch_code(
    address: str,
    password: str,
    attempts: int = 36,
    interval: int = 5,
    exclude_ids: set[str] | None = None,
    code_length: int | None = None,
) -> str | None:
    """أول كود من fetch_codes."""
    codes = await fetch_codes(
        address,
        password,
        attempts,
        interval,
        exclude_ids=exclude_ids,
        code_length=code_length,
    )
    return codes[0] if codes else None


async def verify_mailbox(address: str, password: str) -> bool:
    try:
        await get_token(address, password)
        return True
    except Exception as e:
        logger.debug("mailbox verify %s: %s", address, e)
        return False
