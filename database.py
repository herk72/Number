# database.py — النسخة المدمجة والمصححة
import os
import aiosqlite

if os.path.exists("/app/data"):
    DB_PATH = "/app/data/bot.db"
else:
    DB_PATH = "bot.db"


# ──────────────────────────────────────────
# تهيئة قاعدة البيانات
# ──────────────────────────────────────────
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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT UNIQUE,
                username      TEXT,
                full_name     TEXT,
                session_string TEXT,
                two_fa        TEXT,
                valid         INTEGER DEFAULT 1,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # جدول الإعدادات العامة (يُستخدم للعداد وغيره)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()


# ──────────────────────────────────────────
# دوال المستخدمين
# ──────────────────────────────────────────
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


# ──────────────────────────────────────────
# دوال الجلسات
# ──────────────────────────────────────────
async def get_all_sessions():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC"
        ) as cursor:
            return await cursor.fetchall()


async def get_session_by_phone(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sessions WHERE phone=?", (phone,)
        ) as cursor:
            return await cursor.fetchone()


async def save_session(
    phone: str,
    username: str,
    full_name: str,
    session_string: str,
    two_fa: str = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO sessions
                (phone, username, full_name, session_string, two_fa, valid)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (phone, username, full_name, session_string, two_fa))
        await db.commit()


async def mark_session_invalid(phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET valid=0 WHERE phone=?", (phone,)
        )
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


async def get_sessions_count():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM sessions WHERE valid=1"
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# ──────────────────────────────────────────
# دوال العداد (Email Counter)
# ──────────────────────────────────────────
async def get_email_counter() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key='email_counter'"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0


async def increment_email_counter() -> int:
    """
    زيادة العداد بشكل atomic داخل اتصال واحد لتفادي race condition.
    يعيد القيمة الجديدة بعد الزيادة.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # قراءة وكتابة في نفس الاتصال — لا يوجد خطر تعارض
        async with db.execute(
            "SELECT value FROM settings WHERE key='email_counter'"
        ) as cursor:
            row = await cursor.fetchone()
            new_val = (int(row[0]) if row else 0) + 1

        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('email_counter', ?)",
            (str(new_val),),
        )
        await db.commit()
    return new_val


async def reset_email_counter():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('email_counter', '0')"
        )
        await db.commit()
