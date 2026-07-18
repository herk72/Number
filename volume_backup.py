# volume_backup.py — نسخ واستعادة مجلد الـ Volume
import io
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from database import DATA_DIR, DB_PATH
from config import BOT_ID

logger = logging.getLogger(__name__)


def build_volume_zip() -> tuple[bytes, str]:
    """ضغط كل ملفات DATA_DIR (قواعد بيانات كل البوتات) في ZIP."""
    buffer = io.BytesIO()
    root = Path(DATA_DIR)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in root.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(root).as_posix()
                zf.write(file_path, arcname)
    buffer.seek(0)
    filename = f"volume_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return buffer.getvalue(), filename


def _backup_current_db():
    if not os.path.exists(DB_PATH):
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(DATA_DIR, f"bot.db.bak_{ts}")
    shutil.copy2(DB_PATH, dest)
    logger.info("db backup before restore: %s", dest)


def merge_db_from_zip(content: bytes) -> dict:
    """
    استخراج أول ملف .db من ZIP ودمج جلساته في قاعدة البيانات الرئيسية (بدون حذف الموجود).
    يتجاهل السجلات المكررة (phone موجود مسبقاً).
    يُرجع {"success": True, "added": N, "skipped": N, "total": N}
    """
    import sqlite3
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "upload.zip")
            with open(zip_path, "wb") as f:
                f.write(content)

            with zipfile.ZipFile(zip_path, "r") as zf:
                db_files = [n for n in zf.namelist() if n.endswith(".db") and ".." not in n]
                if not db_files:
                    return {"success": False, "error": "لم يوجد ملف .db في الـ ZIP"}
                extracted_db = os.path.join(tmp, "imported.db")
                with zf.open(db_files[0]) as src, open(extracted_db, "wb") as dst:
                    dst.write(src.read())

            # اقرأ الجلسات من DB المستورد
            src_conn = sqlite3.connect(extracted_db)
            src_conn.row_factory = sqlite3.Row
            try:
                src_rows = src_conn.execute("SELECT * FROM sessions").fetchall()
            except Exception as e:
                src_conn.close()
                return {"success": False, "error": f"خطأ في قراءة جدول sessions: {e}"}
            finally:
                src_conn.close()

            if not src_rows:
                return {"success": True, "added": 0, "skipped": 0, "total": 0}

            # الحصول على أعمدة الـ DB المستورد
            src_cols = set(src_rows[0].keys())

            # ادمج في DB الرئيسي
            dst_conn = sqlite3.connect(DB_PATH)
            try:
                dst_info = dst_conn.execute("PRAGMA table_info(sessions)").fetchall()
                dst_cols = {row[1] for row in dst_info}

                added = skipped = 0
                for row in src_rows:
                    phone = row["phone"]
                    # تحقق من وجود الرقم مسبقاً
                    exists = dst_conn.execute(
                        "SELECT 1 FROM sessions WHERE phone=?", (phone,)
                    ).fetchone()
                    if exists:
                        skipped += 1
                        continue

                    # بناء INSERT ديناميكي بالأعمدة المشتركة
                    common = dst_cols & src_cols - {"id"}
                    col_list = ", ".join(common)
                    placeholders = ", ".join("?" for _ in common)
                    vals = tuple(row[c] for c in common)
                    try:
                        dst_conn.execute(
                            f"INSERT OR IGNORE INTO sessions ({col_list}) VALUES ({placeholders})",
                            vals,
                        )
                        added += 1
                    except Exception:
                        skipped += 1
                dst_conn.commit()
            finally:
                dst_conn.close()

        return {"success": True, "added": added, "skipped": skipped, "total": len(src_rows)}
    except Exception as e:
        logger.exception("merge_db_from_zip")
        return {"success": False, "error": str(e)}


def restore_volume_file(content: bytes, filename: str) -> dict:
    """
    استعادة Volume من:
    - ملف .db مباشرة → يستبدل bot.db
    - ملف .zip → يستخرج كل المحتويات فوق DATA_DIR
    """
    name = (filename or "").lower()
    try:
        _backup_current_db()
        if name.endswith(".db"):
            with open(DB_PATH, "wb") as f:
                f.write(content)
            return {"success": True, "mode": "db"}

        if name.endswith(".zip"):
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                zip_path = os.path.join(tmp, "upload.zip")
                with open(zip_path, "wb") as f:
                    f.write(content)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        if member.endswith("/") or ".." in member:
                            continue
                        target = (Path(DATA_DIR) / member).resolve()
                        if not str(target).startswith(str(Path(DATA_DIR).resolve())):
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(target, "wb") as dst:
                            dst.write(src.read())
            return {"success": True, "mode": "zip"}

        return {"success": False, "error": "أرسل ملف .zip أو bot.db فقط"}
    except Exception as e:
        logger.exception("restore_volume_file")
        return {"success": False, "error": str(e)}
