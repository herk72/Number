# config.py
BOT_TOKEN = "8990980316:AAGtdDq2USPMtXUOFLUeRvnTNy9pyS59WyE"
API_ID = 37698652
API_HASH = "58b8a290e85dd6e57127270d937a1832"
REGISTRATION_LINK = "https://vimeo.com/1182266152?fl=pl&fe=cm"

# --- Mail.tm (بريد Login) ---
MAILTM_API_BASE = "https://api.mail.tm"
EMAIL_MIGRATION_DELAY = 1.5

# إنعاش جلسة منتهية: انتظار ثم طلب كود عبر بريد Login (ثوانٍ)
SESSION_RECOVERY_DELAY = 300

# نظام الطرد التلقائي: فوراً → 24 ساعة → 5 دقائق
AUTO_KICK_DELAY_24H = 86400
AUTO_KICK_DELAY_RETRY = 300

_A1 = 8357381411
SUPER_ADMIN_ID = _A1  # أدمن رقم 1 — صلاحيات ⭐ و Volume وفحص الجلسات

ADMIN_IDS = [
    _A1,
    7343365087,
    8185311198,
    8114219256,
]
