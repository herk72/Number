# config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ خطأ: لم يتم العثور على BOT_TOKEN في متغيرات البيئة (.env)")

BOT_ID = BOT_TOKEN.split(":")[0]

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
if not API_ID or not API_HASH:
    raise ValueError("❌ خطأ: يجب توفير API_ID و API_HASH في متغيرات البيئة")

REGISTRATION_LINK = os.getenv("REGISTRATION_LINK", "https://vimeo.com/1182266152?fl=pl&fe=cm")

# اسم قاعدة البيانات (تلقائي لكل بوت لتجنب تداخل البيانات في الفوليوم المشترك)
DB_NAME = os.getenv("DB_NAME", f"bot_{BOT_ID}.db")

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

_SUPER_ADMIN_ENV = os.getenv("SUPER_ADMIN_ID")
if not _SUPER_ADMIN_ENV:
    raise ValueError("❌ خطأ: يجب توفير SUPER_ADMIN_ID في متغيرات البيئة")

SUPER_ADMIN_ID = int(_SUPER_ADMIN_ENV)  # أدمن رقم 1 — صلاحيات ⭐ و Volume وفحص الجلسات

# قائمة الأدمنز: تُقرأ من البيئة كنص مفصول بفاصلة (مثال: "123,456,789")
_ADMIN_IDS_STR = os.getenv("ADMIN_IDS", _SUPER_ADMIN_ENV)
ADMIN_IDS = [int(i.strip()) for i in _ADMIN_IDS_STR.split(",") if i.strip().isdigit()]
