# security_monitor.py — نظام مراقبة الأمان الشامل (كل 12 ساعة)
"""
يفحص كل 12 ساعة الرسائل الواردة من آخر 13 ساعة لكل حساب:
1. رسائل إعادة تعيين كلمة المرور  → زر إلغاء (متعدد اللغات) + DeclinePasswordReset API + تنبيه
2. رسائل طلب الحذف               → محاولة إلغاء إعادة التعيين API/زر + تنبيه عاجل برابط الإلغاء
3. محاولات تسجيل الدخول الغير مكتملة → يُنبّه الأدمن
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

# أنماط الكشف — تيليجرام يرسل رسائل 777000 بلغة واجهة الحساب
# مصادر: translations.telegram.org + نصوص الخدمة الرسمية
_RESET_PATTERN = re.compile(
    r"("
    r"طلب\s*إعادة\s*تعيين\s*كلمة\s*المرور|"
    r"إعادة\s*تعيين\s*كلمة\s*مرور|"
    r"password\s*reset|"
    r"reset(?:ting)?\s+(?:your\s+)?(?:2[- ]?step|two[- ]?step)\s*(?:verification\s+)?password|"
    r"reset(?:ting)?\s+(?:your\s+)?password|"
    r"сброс(?:а)?\s+парол|"
    r"сбросить\s+парол|"
    r"şifre\s*sıfır|"
    r"réinitialisation.*mot\s*de\s*passe|"
    r"restablecer.*contraseña|"
    r"passwort.*zurücksetzen|"
    r"بازنشانی\s*رمز|"
    r"重置.*密码|密码.*重置"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_DELETE_PATTERN = re.compile(
    r"("
    r"طلب\s*حذف\s*الحساب|"
    r"حذف\s*حسابك|"
    r"حذف\s*حسابك\s*في\s*تيليجرام|"
    r"delete(?:d|ing)?\s+(?:your\s+)?(?:telegram\s+)?account|"
    r"requested\s+to\s+delete|"
    r"account\s*(?:deletion|reset)|"
    r"удал(?:ить|ение)\s+(?:ваш(?:его)?\s+)?аккаунт|"
    r"hesab(?:ınızı|ını)?\s*sil|"
    r"supprimer\s+(?:votre\s+)?compte|"
    r"eliminar\s+(?:su\s+|tu\s+)?cuenta|"
    r"konto\s*löschen|"
    r"حذف\s*حساب|"
    r"删除.*账户|账户.*删除"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_INCOMPLETE_LOGIN_PATTERN = re.compile(
    r"("
    r"محاولة\s*تسجيل\s*(?:الدخول|دخول)\s*غير\s*مكتمل|"
    r"incomplete\s+login|"
    r"login\s+attempt|"
    r"unfinished\s+login|"
    r"незавершенн\w*\s+вход|"
    r"попытка\s+входа|"
    r"tamamlanmamış\s*giriş|"
    r"tentative\s+de\s+connexion|"
    r"intento\s+de\s+inicio"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL_BTN_TEXT = re.compile(
    r"("
    r"إلغاء|"
    r"الغاء|"
    r"cancel|"
    r"decline|"
    r"abort|"
    r"отмен|"
    r"iptal|"
    r"annuler|"
    r"cancelar|"
    r"abbrechen|"
    r"取消"
    r")",
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


async def _rotate_email_after_kick(phone: str, row) -> dict:
    """تغيير البريد مباشرة بعد طرد أجهزة غير موثوقة."""
    db_email = database.row_login_email(row)
    try:
        mail_res = await session_manager.change_login_email(phone, force=True)
        return {
            "success": bool(mail_res.get("success")),
            "old_email": mail_res.get("old_email") or db_email,
            "tg_pattern": mail_res.get("tg_pattern"),
            "new_email": mail_res.get("email"),
            "error": mail_res.get("error"),
        }
    except Exception as e:
        return {
            "success": False,
            "old_email": db_email,
            "tg_pattern": None,
            "new_email": None,
            "error": str(e),
        }


async def _try_decline_password_reset(client, phone: str) -> dict:
    """
    يلغي طلب إعادة تعيين 2FA عبر API إن وُجد (account.declinePasswordReset).
    أكثر موثوقية من الاعتماد على لغة زر الرسالة فقط.
    """
    out = {
        "had_pending": False,
        "declined": False,
        "error": None,
        "pending_reset_date": None,
    }
    try:
        from telethon.tl.functions.account import (
            GetPasswordRequest,
            DeclinePasswordResetRequest,
        )
        pwd = await client(GetPasswordRequest())
        pending = getattr(pwd, "pending_reset_date", None)
        if not pending:
            return out
        out["had_pending"] = True
        out["pending_reset_date"] = str(pending)
        await client(DeclinePasswordResetRequest())
        out["declined"] = True
        logger.info("security: declined pending password reset via API for %s", phone)
    except Exception as e:
        err = str(e)
        if "RESET_REQUEST_MISSING" in err or "ResetRequestMissing" in err:
            out["error"] = None
        else:
            out["error"] = err
            logger.warning("security: declinePasswordReset %s: %s", phone, e)
    return out


async def _click_cancel_button(msg) -> tuple[bool, str | None]:
    """يضغط زر الإلغاء بأي لغة مدعومة."""
    if not msg.buttons:
        return False, None
    try:
        for row_btns in msg.buttons:
            for btn in (row_btns if isinstance(row_btns, list) else [row_btns]):
                label = (getattr(btn, "text", None) or "")
                if _CANCEL_BTN_TEXT.search(label):
                    await btn.click()
                    return True, None
    except Exception as e:
        return False, str(e)
    return False, None


async def _cancel_reset_on_message(client, phone: str, msg) -> dict:
    """زر الإلغاء + DeclinePasswordReset API."""
    clicked, click_err = await _click_cancel_button(msg)
    api = await _try_decline_password_reset(client, phone)
    cancelled = bool(clicked or api.get("declined"))
    err = click_err or api.get("error")
    if not cancelled and not err:
        err = "لم يُعثر على زر إلغاء ولم يوجد طلب معلّق في API"
    return {
        "cancelled": cancelled,
        "api_declined": bool(api.get("declined")),
        "error": err if not cancelled else None,
    }


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

        # كشف API موثوق: pending_reset_date في account.Password
        api_declined = await _try_decline_password_reset(client, phone)
        if api_declined.get("had_pending"):
            info = {
                "device": "",
                "location": "",
                "ip": "",
                "date_str": "",
                "cancelled": bool(api_declined.get("declined")),
                "cancel_error": api_declined.get("error"),
                "msg_id": 0,
                "via_api": True,
                "api_declined": bool(api_declined.get("declined")),
                "pending_reset_date": api_declined.get("pending_reset_date"),
            }
            results["password_reset"].append(info)

        for msg in messages:
            if not msg or not msg.text:
                continue

            msg_ts = msg.date.timestamp() if msg.date else 0
            if msg_ts < cutoff_ts:
                continue

            text = msg.text
            is_delete = bool(_DELETE_PATTERN.search(text))
            is_reset = bool(_RESET_PATTERN.search(text))
            is_incomplete = bool(_INCOMPLETE_LOGIN_PATTERN.search(text))

            # الحذف أولوية (غالباً يجمع حذف الحساب + إعادة تعيين 2FA)
            if is_delete:
                info = _extract_delete_info(text)
                info["msg_id"] = msg.id
                cancel_res = await _cancel_reset_on_message(client, phone, msg)
                info["cancelled"] = cancel_res.get("cancelled", False)
                info["cancel_error"] = cancel_res.get("error")
                info["api_declined"] = cancel_res.get("api_declined", False)
                results["account_delete"].append(info)
            elif is_reset:
                info = _extract_message_info(text)
                cancel_res = await _cancel_reset_on_message(client, phone, msg)
                info["cancelled"] = cancel_res.get("cancelled", False)
                info["cancel_error"] = cancel_res.get("error")
                info["api_declined"] = cancel_res.get("api_declined", False)
                info["msg_id"] = msg.id
                results["password_reset"].append(info)
            elif is_incomplete:
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
def _extract_field(text: str, labels: list[str]) -> str:
    for lab in labels:
        m = re.search(rf"(?:^|\n)\s*{re.escape(lab)}\s*[:：]\s*([^\n]+)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _extract_message_info(text: str) -> dict:
    info = {
        "device": _extract_field(
            text, ["الجهاز", "Device", "device", "Устройство", "Cihaz", "Gerät", "Appareil"]
        ),
        "location": _extract_field(
            text, ["الموقع", "Location", "location", "Местоположение", "Konum", "Standort", "Lieu"]
        ),
        "ip": "",
        "date_str": "",
    }
    date_match = re.search(r"(\d{2}/\d{2}/\d{4}[^\n]*UTC)", text)
    if date_match:
        info["date_str"] = date_match.group(1).strip()
    return info


def _extract_delete_info(text: str) -> dict:
    info = {"phone_in_msg": "", "link": ""}
    phone_match = re.search(r"\+(\d{7,15})", text)
    if phone_match:
        info["phone_in_msg"] = "+" + phone_match.group(1)
    link_match = re.search(
        r"https?://(?:t\.me|telegram\.me|telegram\.org)/\S+", text, re.IGNORECASE
    )
    if link_match:
        info["link"] = link_match.group(0).rstrip(").,]")
    return info


def _extract_incomplete_login_info(text: str) -> dict:
    return _extract_message_info(text)


# ──────────────────────────────────────────
# فحص تغيير البريد الإلكتروني لحساب واحد
# ──────────────────────────────────────────
async def _check_email_change(phone: str, row) -> dict:
    """
    يتحقق من أن قناع بريد Login على تيليجرام يطابق البريد في القاعدة.
    إذا تغيّر أو أُزيل → يُعاد ربطه تلقائياً.
    يُعيد: {"changed", "restored", "new_email", "old_email", "tg_pattern", "error"}
    """
    result = {
        "changed": False,
        "restored": False,
        "new_email": None,
        "old_email": None,
        "tg_pattern": None,
        "error": None,
    }
    try:
        db_email = database.row_login_email(row)
        result["old_email"] = db_email

        client = await session_manager.get_active_client(phone)
        if not client:
            return result

        try:
            tg_pattern = await session_manager.get_telegram_login_email_pattern(client)
        except Exception:
            tg_pattern = None
        finally:
            await client.disconnect()

        result["tg_pattern"] = tg_pattern

        needs_restore = False
        if not tg_pattern:
            # لا يوجد بريد Login على تيليجرام
            if db_email:
                needs_restore = True
        elif db_email and not session_manager.email_matches_login_pattern(
            db_email, tg_pattern
        ):
            # القناع الفعلي لا يطابق بريد القاعدة (تغيير يدوي مثلاً)
            needs_restore = True
            logger.info(
                "security: email mismatch %s db=%s tg=%s",
                phone,
                db_email,
                tg_pattern,
            )
        elif not db_email and tg_pattern:
            # يوجد بريد على TG وليس في القاعدة — أعد الربط لنملك صندوق Mail.tm
            needs_restore = True

        if not needs_restore:
            return result

        result["changed"] = True
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
        valid_sessions = [
            s for s in sessions
            if s["valid"] and database.row_get(s, "two_fa") and database.row_flag(s, "secured")
        ]
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
            db_2fa = database.row_get(s, "two_fa") or ""
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
                        "full_name": database.row_get(s, "full_name", ""),
                        "username": database.row_get(s, "username", ""),
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
    """
    دورة فحص كاملة:
    - المؤمّنة الصالحة: فحص أمني كامل
    - غير المؤمّنة الصالحة: فحص جلسة عادي فقط
    - المعطّلة: تُتخطى
    """
    logger.info("security: starting full check…")
    sessions = await database.get_all_sessions()
    if not sessions:
        logger.info("security: no sessions to check")
        return

    secured_rows = [
        s for s in sessions
        if s["valid"] and database.row_flag(s, "secured")
    ]
    unsecured_rows = [
        s for s in sessions
        if s["valid"] and not database.row_flag(s, "secured")
    ]

    report_lines = [
        f"🛡️ <b>تقرير الفحص الأمني</b>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"📊 فحص أمني (مؤمّنة): <b>{len(secured_rows)}</b>\n"
        f"🔎 فحص جلسة (غير مؤمّنة): <b>{len(unsecured_rows)}</b>\n"
    ]
    alerts_count = 0

    # غير المؤمّنة: فحص حيوية الجلسة فقط
    for row in unsecured_rows:
        phone = row["phone"]
        try:
            alive = await session_manager.check_session_alive(phone)
            if not alive:
                alerts_count += 1
                await _notify(
                    f"🔴 <b>جلسة غير مؤمّنة متوقفة</b>\n"
                    f"📞 الحساب: <code>{phone}</code>",
                    phone=phone,
                )
        except Exception as e:
            logger.debug("unsecured alive check %s: %s", phone, e)
        await asyncio.sleep(0.2)

    for row in secured_rows:
        phone = row["phone"]

        # 1) فحص بصمة الجهاز
        kicked_devices = await _check_device_fingerprint(phone, row)
        if kicked_devices:
            alerts_count += 1
            device_list = "، ".join(kicked_devices)
            mail_block = ""
            mail_res = await _rotate_email_after_kick(phone, row)
            old_disp = mail_res.get("tg_pattern") or mail_res.get("old_email") or "—"
            if mail_res.get("success"):
                mail_block = (
                    f"\n📧 البريد الفعلي (قبل): <code>{old_disp}</code>\n"
                    f"✅ البريد الجديد: <code>{mail_res.get('new_email') or '—'}</code>"
                )
            else:
                mail_block = (
                    f"\n📧 البريد الفعلي: <code>{old_disp}</code>\n"
                    f"❌ فشل تغيير البريد: {mail_res.get('error') or '—'}"
                )
            msg = (
                f"📱 <b>أجهزة غير موثوقة طُردت</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"🔴 الأجهزة المطرودة: <code>{device_list}</code>"
                f"{mail_block}"
            )
            await _notify(msg, phone=phone)

        await asyncio.sleep(0.5)

        # 2) فحص رسائل 777000 (إعادة التعيين / الحذف / الدخول)
        scan_res = await _scan_official_messages(
            phone, row, lookback_hours=SECURITY_MESSAGE_LOOKBACK
        )

        # رسائل / حالة إعادة التعيين
        for info in scan_res["password_reset"]:
            alerts_count += 1
            if info.get("cancelled"):
                via = []
                if info.get("api_declined") or info.get("via_api"):
                    via.append("API")
                if info.get("msg_id"):
                    via.append("زر")
                status = "✅ تم إلغاؤه تلقائياً" + (f" ({'+'.join(via)})" if via else "")
            else:
                status = f"⚠️ فشل الإلغاء: {info.get('cancel_error') or '—'}"
            extra = ""
            if info.get("pending_reset_date"):
                extra = f"\n⏳ كان معلّقاً حتى: <code>{info['pending_reset_date']}</code>"
            msg = (
                f"🔑 <b>طلب إعادة تعيين كلمة المرور!</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"📅 التاريخ: {info.get('date_str') or 'غير معروف'}\n"
                f"📍 الموقع: {info.get('location') or 'غير معروف'}\n"
                f"📲 الجهاز: <code>{info.get('device') or 'غير معروف'}</code>\n"
                f"🔘 الإجراء: {status}"
                f"{extra}"
            )
            await _notify(msg, phone=phone)

        # رسائل طلب الحذف (+ إعادة تعيين غالباً)
        for info in scan_res["account_delete"]:
            alerts_count += 1
            if info.get("cancelled"):
                cancel_status = "✅ أُلغي جزء إعادة التعيين (زر/API)"
            else:
                cancel_status = f"⚠️ تعذّر الإلغاء التلقائي: {info.get('cancel_error') or '—'}"
            msg = (
                f"🗑️ <b>طلب حذف حساب!</b>\n"
                f"📞 الحساب: <code>{phone}</code>\n"
                f"📱 الرقم في الرسالة: {info.get('phone_in_msg') or '—'}\n"
                f"🔘 الإجراء: {cancel_status}\n"
                f"⚠️ إن بقي الطلب: افتح الرابط أو غيّر الرقم من جهاز موثوق فوراً.\n"
                f"🔗 رابط الإلغاء: {info.get('link') or 'غير موجود في الرسالة'}"
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

        # 3) فحص تغيير البريد (مقارنة القناع الفعلي مع القاعدة)
        email_res = await _check_email_change(phone, row)
        if email_res["changed"]:
            alerts_count += 1
            old_email = email_res.get("old_email") or "—"
            tg_pattern = email_res.get("tg_pattern") or "—"
            if email_res["restored"]:
                new_email = email_res.get("new_email") or "—"
                msg = (
                    f"📧 <b>تغيّر بريد الحساب — تمت استعادته تلقائياً</b>\n"
                    f"📞 الحساب: <code>{phone}</code>\n"
                    f"👁 القناع الفعلي: <code>{tg_pattern}</code>\n"
                    f"📤 البريد في القاعدة: <code>{old_email}</code>\n"
                    f"✅ البريد الجديد: <code>{new_email}</code>"
                )
            else:
                msg = (
                    f"📧 <b>تغيّر بريد الحساب — فشل الاستعادة!</b>\n"
                    f"📞 الحساب: <code>{phone}</code>\n"
                    f"👁 القناع الفعلي: <code>{tg_pattern}</code>\n"
                    f"📤 البريد في القاعدة: <code>{old_email}</code>\n"
                    f"❌ الخطأ: {email_res.get('error','')}"
                )
            await _notify(msg, phone=phone)

        await asyncio.sleep(1)

        # 4) تجديد جهات الاتصال المشتركة (يحدث مع كل دورة فحص)
        await _update_mutual_contacts(phone, row)
        await asyncio.sleep(0.5)

    # 5) فحص التحقق بخطوتين الدوري (مؤمّنة فقط)
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
