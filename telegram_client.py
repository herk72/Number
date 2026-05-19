# telegram_client.py — عميل Telethon ببصمة جهاز واقعية
import random

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import (
    API_ID,
    API_HASH,
    TELEGRAM_DEVICE_PROFILES,
    TELEGRAM_LANG_CODE,
    TELEGRAM_SYSTEM_LANG_CODE,
)


def pick_device_profile() -> dict:
    pool = TELEGRAM_DEVICE_PROFILES or []
    if not pool:
        return {
            "device_model": "Samsung SM-S911B",
            "system_version": "SDK 34",
            "app_version": "11.4.2",
        }
    return dict(random.choice(pool))


def make_telegram_client(session_string: str | None = None) -> TelegramClient:
    """جلسة جديدة أو محفوظة — دائماً بمعلومات جهاز أندرويد حقيقية."""
    session = StringSession(session_string or "")
    profile = pick_device_profile()
    return TelegramClient(
        session,
        API_ID,
        API_HASH,
        device_model=profile.get("device_model", "POCO X3 Pro"),
        system_version=profile.get("system_version", "SDK 33"),
        app_version=profile.get("app_version", "10.14.5"),
        lang_code=profile.get("lang_code", TELEGRAM_LANG_CODE),
        system_lang_code=profile.get("system_lang_code", TELEGRAM_SYSTEM_LANG_CODE),
    )
