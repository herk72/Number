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
    # اسم الملف يحتوي على BOT_ID لتمييز النسخة
    filename = f"volume_{BOT_ID}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return buffer.getvalue(), filename


def _backup_current_db():
    if not os.path.exists(DB_PATH):
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(DATA_DIR, f"bot.db.bak_{ts}")
    shutil.copy2(DB_PATH, dest)
    logger.info("db backup before restore: %s", dest)


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
