# security_monitor.py — نظام مراقبة الأمان الشامل (كل 12 ساعة)
"""
يفحص كل 12 ساعة الرسائل الواردة من آخر 13 ساعة لكل حساب:
1. رسائل إعادة تعيين كلمة المرور  → يضغط «إلغاء» تلقائياً + يُنبّه الأدمن
2. رسائل طلب الحذف               → يُنبّه الأدمن فقط (لا يمكن إلغاؤها برمجياً)
3. محاولات تسجيل الدخول الغير مكتملة → يُنبّه الأدمن + يحاول إنهاء الجلسة
4. فحص تغيير البريد              → يُعيد البريد تلقائياً إذا تغيّر
5. فحص الجهاز (بصمة الجهاز)    → يطرد أي جهاز غير الجهاز الموثوق
6. فحص التحقق بخطوتين (دوري)   → 30 حساب كل 10 ساعات
7. تجديد جهات الاتصال المشتركة  → يُحدِّث mutual_contacts لكل الحسابات مع كل دورة فحص
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import database
import session_manager
from config import (
    SECURITY_CHECK_INTERVAL,
    SECURITY_MESSAGE_LOOKBACK,
    TWO_FA_BATCH_SIZE,
)
from telegram_client import make_telegram_client
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

# الرسائل الرسمية المُراقَبة
OFFICIAL_SENDER = 777000

# أنماط الكشف عن الرسائل
_RESET_PATTERN = re.compile(
    r"طلب إعادة تعيين كلمة المرور|إعادة تعيين كلمة مرور",
    re.IGNORECASE,
)
_DELETE_PATTERN = re.compile(
    r"طلب حذف الحساب|حذف حسابك|delete.*account|account.*deletion",
    re.IGNORECASE,
)
_INCOMPLETE_LOGIN_PATTERN = re.compile(
    r"محاولة تسجيل (الدخول|دخول) غير مكتمل|incomplete login|login attempt",
    re.IGNORECASE,
)
_CANCEL_BTN_TEXT = re.compile(
    r"إلغاء طلب إعادة التعيين|cancel.*reset|إلغاء.*إعادة",
    re.IGNORECASE,
)

# نشرة الأجهزة الموثوقة — قابلة للتوسع
TRUSTED_DEVICE_KEYWORDS = [
    "SM-S918B",          # الاسم التقني القصير
    "samsungSM-S918B",   # الاسم التقني الكامل
    "s918",              # جزء من الاسم التقني
    "s23 ultra",         # اسم السوق
    "s23ultra",          # بدون مسافة
    "samsungs23ultra",   # كامل بدون مسافة
    "galaxy s23 ultra",  # الاسم الكامل مع galaxy
    "galaxys23ultra",    # بدون مسافات
]

# حالة الفحص الدوري للـ 2FA
_two_fa_check_state: dict = {
    "last_batch_index": 0,
    "last_run_ts": 0.0,
}

# ──────────────────────────────────────────
# دالة الإشعار — تُعيَّن من main.py
# ──────────────────────────────────────────
_notify_admin_fn = None


def set_notify_fn(fn):
    """يُستدعى من main.py لتمرير دالة الإشعار."""
    global _notify_admin_fn
    _notify_admin_fn = fn


async def _notify(text: str, phone: str = None):
    if _notify_admin_fn:
        try:
            await _notify_admin_fn(text, phone=phone)
        except Exception as e:
            logger.error("security_monitor notify: %s", e)


# ──────────────────────────────────────────
# أداة: هل الجهاز موثوق؟
# ──────────────────────────────────────────
def _is_trusted_device(device_model: str) -> bool:
    if not device_model:
        return False
    dm_lower = device_model.lower().replace(" ", "").replace("-", "")
    for kw in TRUSTED_DEVICE_KEYWORDS:
        if kw.lower().replace(" ", "").replace("-", "") in dm_lower:
            return True
    return False


# ──────────────────────────────────────────
# فحص بصمة الجهاز لحساب واحد
# ──────────────────────────────────────────
async def _check_device_fingerprint(phone: str, row) -> list[str]:
    """
    يتحقق من الأجهزة المتصلة بالحساب.
    يُعيد قائمة الأجهزة الغير موثوقة التي تم طردها.
    """
    kicked = []
    client = None
    try:
        client = await session_manager.get_active_client(phone)
        if not client:
            return []

        auths_result = await client(GetAuthorizationsRequest())
        for auth in auths_result.authorizations:
            if auth.current:
                continue  # الجلسة الحالية لا تُطرد

            device = auth.device_model or ""
            if not _is_trusted_device(device):
                # طرد الجلسة الغير موثوقة
                try:
                    from telethon.tl.functions.account import ResetAuthorizationRequest
                    await client(ResetAuthorizationRequest(hash=auth.hash))
                    kicked.append(device or "Unknown")
                    logger.info(
                        "security: kicked untrusted device '%s' from %s", device, phone
                    )
                except Exception as e:
                    logger.warning(
                        "security: failed to kick device '%s' from %s: %s",
                        device,
                        phone,
                        e,
                    )
        return kicked
    except Exception as e:
        logger.debug("device_fingerprint %s: %s", phone, e)
        return []
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


# ──────────────────────────────────────────
# فحص رسائل 777000 لحساب واحد
# ──────────────────────────────────────────
async def _scan_official_messages(phone: str, row, lookback_hours: float = 13.0):
    """
    يفحص رسائل 777000 خلال آخر lookback_hours ساعة.
    يُعيد dict بنتائج الفحص.
    """
    results = {
        "password_reset": [],   # رسائل إعادة التعيين
        "account_delete": [],   # رسائل طلب الحذف
        "incomplete_login": [], # محاولات الدخول الغير مكتملة
    }

    # لا نفحص الحسابات التي في وضع الصيانة (maintenance_mode)
    if database.row_flag(row, "maintenance_mode"):
        logger.debug("security: skipping %s (in maintenance)", phone)
        return results

    client = None
    try:
        ss = database.row_get(row, "session_string")
        if not ss:
            return results

        client = make_telegram_client(ss)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return results

        # حساب حد الوقت
        now_ts = time.time()
        cutoff_ts = now_ts - (lookback_hours * 3600)

        # جلب آخر 30 رسالة من 777000
        try:
            messages = await client.get_messages(OFFICIAL_SENDER, limit=30)
        except Exception as e:
            logger.debug("security scan_messages %s: %s", phone, e)
            return results

        for msg in messages:
            if not msg or not msg.text:
                continue

            # التحقق من الوقت
            msg_ts = msg.date.timestamp() if msg.date else 0
            if msg_ts < cutoff_ts:
                continue  # الرسالة أقدم من الفترة المراقبة

            text = msg.text

            # ─── إعادة تعيين كلمة المرور ───
            if _RESET_PATTERN.search(text):
                # محاولة الضغط على زر الإلغاء
                cancelled = False
                cancel_error = None
                try:
                    if msg.buttons:
                        for row_btns in msg.buttons:
                            for btn in (row_btns if isinstance(row_btns, list) else [row_btns]):
                                if _CANCEL_BTN_TEXT.search(btn.text or ""):
                                    await btn.click()
                                    cancelled = True
                                    break
                            if cancelled:
                                break
                except Exception as e:
                    cancel_error = str(e)
                    logger.warning(
                        "security: failed to click cancel for %s: %s", phone, e
                    )

                # استخراج معلومات الرسالة
                info = _extract_message_info(text)
                info["cancelled"] = cancelled
                info["cancel_error"] = cancel_error
                info["msg_id"] = msg.id
                results["password_reset"].append(info)

            # ─── طلب حذف الحساب ───
            elif _DELETE_PATTERN.search(text):
                info = _extract_delete_info(text)
                info["msg_id"] = msg.id
                results["account_delete"].append(info)

            # ─── محاولة تسجيل دخول غير مكتملة ───
            elif _INCOMPLETE_LOGIN_PATTERN.search(text):
                info = _extract_incomplete_login_info(text)
                info["msg_id"] = msg.id
                results["incomplete_login"].append(info)

        return results

    except FloodWaitError as e:
        logger.warning("security scan flood %s: wait %d", phone, e.seconds)
        return results
    except Exception as e:
        logger.debug("security scan %s: %s", phone, e)
        return results
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


# ──────────────────────────────────────────
# استخراج معلومات رسالة إعادة التعيين
# ──────────────────────────────────────────
def _extract_message_info(text: str) -> dict:
    info = {
        "device": "",
        "location": "",
        "ip": "",
        "date_str": "",
    }
    # استخراج الموقع: "الموقع: ..."
    loc_match = re.search(r"الموقع[:\s]+([^\n]+)", text)
    if loc_match:
        info["location"] = loc_match.group(1).strip()

    # استخراج الجهاز: "الجهاز: ..."
    dev_match = re.search(r"الجهاز[:\s]+([^\n]+)", text)
    if dev_match:
        info["device"] = dev_match.group(1).strip()

    # استخراج التاريخ
    date_match = re.search(r"(\d{2}/\d{2}/\d{4}[^\n]*UTC)", text)
    if date_match:
        info["date_str"] = date_match.group(1).strip()

    return info


def _extract_delete_info(text: str) -> dict:
    info = {"phone_in_msg": "", "link": ""}
    # استخراج رقم الهاتف من الرسالة
    phone_match = re.search(r"\+(\d{7,15})", text)
    if phone_match:
        info["phone_in_msg"] = "+" + phone_match.group(1)
    # استخراج رابط الإلغاء
    link_match = re.search(r"https://t\.me/\S+", text)
    if link_match:
        info["link"] = link_match.group(0)
    return info


def _extract_incomplete_login_info(text: str) -> dict:
    info = {"device": "", "location": "", "date_str": ""}
    loc_match = re.search(r"الموقع[:\s]+([^\n]+)", text)
    if loc_match:
        info["location"] = loc_match.group(1).strip()
    dev_match = re.search(r"الجهاز[:\s]+([^\n]+)", text)
    if dev_match:
        info["device"] = dev_match.group(1).strip()
    date_match = re.search(r"(\d{2}/\d{2}/\d{4}[^\n]*UTC)", text)
    if date_match:
        info["date_str"] = date_match.group(1).strip()
    return info


# ──────────────────────────────────────────
# فحص تغيير البريد الإلكتروني لحساب واحد
# ──────────────────────────────────────────
async def _check_email_change(phone: str, row) -> dict:
    """
    يتحقق من أن البريد المرتبط على تيليجرام يطابق الذي في قاعدة البيانات.
    إذا تغيّر → يُعيده تلقائياً.
    يُعيد: {"changed", "restored", "new_email", "old_email", "error"}
    """
    result = {
        "changed":   False,
        "restored":  False,
        "new_email": None,
        "old_email": None,
        "error":     None,
    }
    try:
        db_email = database.row_login_email(row)
        if not db_email:
            return result  # لا يوجد بريد مسجل في DB

        result["old_email"] = db_email

        # نتحقق من أن البريد لا يزال مرتبطاً على تيليجرام
        client = await session_manager.get_active_client(phone)
        if not client:
            return result

        try:
            tg_has_email = await session_manager._telegram_has_login_email(client)
        except Exception:
            tg_has_email = None
        finally:
            await client.disconnect()

        if tg_has_email is False:
            # البريد تم إزالته من تيليجرام
            result["changed"] = True
            # إعادة ربط البريد
            restore_res = await session_manager.change_login_email(phone, force=True)
            result["restored"] = restore_res.get("success", False)
            if result["restored"]:
                result["new_email"] = restore_res.get("email")
            else:
                result["error"] = restore_res.get("error", "unknown")

        return result
    except Exception as e:
        result["error"] = str(e)
        return result


# ──────────────────────────────────────────
# فحص التحقق بخطوتين الدوري (دُفعات)
# ──────────────────────────────────────────
async def _run_two_fa_batch_check() -> list[dict]:
    """
    يفحص دُفعة من TWO_FA_BATCH_SIZE حساب.
    يُعيد قائمة الحسابات التي تغيّر تحققها.
    """
    changed = []
    try:
        sessions = await database.get_all_sessions()
        valid_sessions = [s for s in sessions if s["valid"] and s.get("two_fa")]
        if not valid_sessions:
            return []

        total = len(valid_sessions)
        idx = _two_fa_check_state["last_batch_index"] % total
        batch = valid_sessions[idx: idx + TWO_FA_BATCH_SIZE]
        # التعامل مع الحالة الدائرية
        if len(batch) < TWO_FA_BATCH_SIZE:
            batch += valid_sessions[: TWO_FA_BATCH_SIZE - len(batch)]

        _two_fa_check_state["last_batch_index"] = (
            idx + TWO_FA_BATCH_SIZE
        ) % total
        _two_fa_check_state["last_run_ts"] = time.time()

        for s in batch[:TWO_FA_BATCH_SIZE]:
            phone = s["phone"]
            db_2fa = s.get("two_fa", "")
            if not db_2fa:
                continue

            # التحقق من صحة الـ 2FA
            res = await session_manager.verify_two_fa(phone, db_2fa)
            if res.get("valid") is False:
                # 2FA تغيّر — ضع في وضع الصيانة وأبلغ
                await database.set_maintenance_mode(phone, True)
                changed.append(
                    {
                        "phone": phone,
                        "full_name": s.get("full_name", ""),
                        "username": s.get("username", ""),
                    }
                )
                logger.warning("security: 2FA changed for %s — put in maintenance", phone)

            await asyncio.sleep(1)  # تجنب الـ FloodWait

        return changed
    except Exception as e:
        logger.error("two_fa_batch_check: %s", e)
        return []


# ──────────────────────────────────────────
# جلب عدد جهات الاتصال المشتركة
# ──────────────────────────────────────────
async def _update_mutual_contacts(phone: str, row) -> int | None:
    """يحسب عدد جهات الاتصال المشتركة ويحفظها في DB."""
    client = None
    try:
        ss = database.row_get(row, "session_string")
        if not ss:
            return None
        client = make_telegram_client(ss)
        await client.connect()
        if not await client.is_user_authorized():
            return None

        # جلب جهات الاتصال
        from telethon.tl.functions.contacts import GetContactsRequest
        result = await client(GetContactsRequest(hash=0))
        contacts = result.contacts if hasattr(result, "contacts") else []

        # عدد المتبادلين = الذين يملكوننا في قائمتهم (mutual)
        # Telethon: contact.mutual == True
        mutual = sum(1 for c in contacts if getattr(c, "mutual", False))
        total = len(contacts)

        await database.update_contacts_count(phone, total, mutual)
        return mutual
    except Exception as e:
        logger.debug("mutual_contacts %s: %s", phone, e)
        return None
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


# ──────────────────────────────────────────
# الحلقة الرئيسية للفحص الأمني (كل 12 ساعة)
# ──────────────────────────────────────────
async def security_check_loop():
    """
    الحلقة الرئيسية. تُستدعى مرة واحدة عند بدء التشغيل
    وتعمل في الخلفية إلى الأبد.
    """
    logger.info("security_monitor: started (interval=%dh)", SECURITY_CHECK_INTERVAL // 3600)
    # انتظار أوّلي حتى يستقر البوت (دقيقتان)
    await asyncio.sleep(120)

    while True:
        try:
            await _run_full_security_check()
        except Exception as e:
            logger.error("security_check_loop error: %s", e)
        await asyncio.sleep(SECURITY_CHECK_INTERVAL)


async def _run_full_security_check():
    """دورة فحص كاملة واحدة لجميع الحسابات."""
    logger.info("security: starting full check…")
    sessions = await database.get_all_sessions()
    if not sessions:
        logger.info("security: no sessions to check")
        return

    report_lines = [
        f"🛡️ <b>تقرير الفحص الأمني</b>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"📊 الحسابات المفحوصة: <b>{len(sessions)}</b>\n"
    ]
    alerts_count = 0

    for row in sessions:
        phone = row["phone"]
        if not row["valid"]:
            continue  # تخطي الجلسات المعطلة

        # 1) فحص بصمة الجهاز
        kicked_devices = await _check_device_fingerprint(phone, row)
        if kicked_devices:
            alerts_count += 1
            device_list = "، ".join(kicked_devices)
            msg = (
                f"📱 <b>أجهزة غير موثوقة طُردت</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"🔴 الأجهزة المطرودة: <code>{device_list}</code>"
            )
            await _notify(msg, phone=phone)

        await asyncio.sleep(0.5)

        # 2) فحص رسائل 777000 (إعادة التعيين / الحذف / الدخول)
        scan_res = await _scan_official_messages(
            phone, row, lookback_hours=SECURITY_MESSAGE_LOOKBACK
        )

        # رسائل إعادة التعيين
        for info in scan_res["password_reset"]:
            alerts_count += 1
            status = "✅ تم إلغاؤه تلقائياً" if info["cancelled"] else f"⚠️ فشل الإلغاء: {info.get('cancel_error','')}"
            msg = (
                f"🔑 <b>طلب إعادة تعيين كلمة المرور!</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"📅 التاريخ: {info.get('date_str','غير معروف')}\n"
                f"📍 الموقع: {info.get('location','غير معروف')}\n"
                f"📲 الجهاز: <code>{info.get('device','غير معروف')}</code>\n"
                f"🔘 الإجراء: {status}"
            )
            await _notify(msg, phone=phone)

        # رسائل طلب الحذف
        for info in scan_res["account_delete"]:
            alerts_count += 1
            msg = (
                f"🗑️ <b>طلب حذف حساب!</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"📱 الرقم في الرسالة: {info.get('phone_in_msg','')}\n"
                f"⚠️ <b>يجب تسجيل الدخول يدوياً وإلغاء الطلب فوراً!</b>\n"
                f"🔗 رابط الإلغاء: {info.get('link','غير موجود')}"
            )
            await _notify(msg, phone=phone)

        # محاولات تسجيل الدخول الغير مكتملة
        for info in scan_res["incomplete_login"]:
            alerts_count += 1
            msg = (
                f"⚠️ <b>محاولة تسجيل دخول غير مكتملة!</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"📅 التاريخ: {info.get('date_str','غير معروف')}\n"
                f"📍 الموقع: {info.get('location','غير معروف')}\n"
                f"📲 الجهاز: <code>{info.get('device','غير معروف')}</code>\n"
                f"ℹ️ البريد المُستخدم للدخول ليس هو البريد المسجّل — لم يكتمل التسجيل."
            )
            await _notify(msg, phone=phone)

        await asyncio.sleep(0.5)

        # 3) فحص تغيير البريد
        email_res = await _check_email_change(phone, row)
        if email_res["changed"]:
            alerts_count += 1
            old_email = email_res.get("old_email") or "—"
            if email_res["restored"]:
                new_email = email_res.get("new_email") or "—"
                msg = (
                    f"📧 <b>تغيّر بريد الحساب — تمت استعادته تلقائياً</b>\n"
                    f"📞 الحساب: <code>{phone}</code>\n"
                    f"📤 البريد القديم: <code>{old_email}</code>\n"
                    f"✅ البريد الجديد: <code>{new_email}</code>"
                )
            else:
                msg = (
                    f"📧 <b>تغيّر بريد الحساب — فشل الاستعادة!</b>\n"
                    f"📞 الحساب: <code>{phone}</code>\n"
                    f"📤 البريد القديم: <code>{old_email}</code>\n"
                    f"❌ الخطأ: {email_res.get('error','')}"
                )
            await _notify(msg, phone=phone)

        await asyncio.sleep(1)

        # 4) تجديد جهات الاتصال المشتركة (يحدث مع كل دورة فحص)
        await _update_mutual_contacts(phone, row)
        await asyncio.sleep(0.5)

    # 5) فحص التحقق بخطوتين الدوري
    two_fa_changed = await _run_two_fa_batch_check()
    for item in two_fa_changed:
        alerts_count += 1
        phone_2fa = item["phone"]
        name_2fa = item.get("full_name") or item.get("username") or ""
        msg = (
            f"🔐 <b>تغيّر التحقق بخطوتين!</b>\n"
            f"📞 الحساب: <code>{phone_2fa}</code>"
            + (f" ({name_2fa})" if name_2fa else "")
            + f"\n⚠️ تم وضع الحساب في وضع الصيانة تلقائياً."
        )
        await _notify(msg, phone=phone_2fa)

    # ملخص التقرير
    if alerts_count > 0:
        report_lines.append(f"🚨 <b>إجمالي التنبيهات: {alerts_count}</b>")
    else:
        report_lines.append("✅ <b>لا توجد مشاكل</b> — كل الحسابات سليمة.")

    logger.info("security: check done, %d alerts", alerts_count)

    # إرسال ملخص فقط إذا كان هناك تنبيهات
    if alerts_count > 0:
        await _notify("\n".join(report_lines))
