import aiosqlite
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = "bot.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                action_mode TEXT DEFAULT 'notify_admin',
                admin_id INTEGER DEFAULT NULL,
                verify_topic_id INTEGER DEFAULT NULL,
                use_llm INTEGER DEFAULT 0
            )
        """)
        # Добавляем столбцы, если их нет
        for col in ["verify_topic_id", "use_llm"]:
            try:
                await db.execute(f"ALTER TABLE chat_settings ADD COLUMN {col} INTEGER DEFAULT NULL")
            except aiosqlite.OperationalError:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_verification (
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                message_thread_id INTEGER,
                expires_at TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER,
                chat_id INTEGER,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.commit()

async def get_chat_settings(chat_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT action_mode, admin_id, verify_topic_id, use_llm FROM chat_settings WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "action_mode": row[0],
                    "admin_id": row[1],
                    "verify_topic_id": row[2],
                    "use_llm": bool(row[3])
                }
            return {"action_mode": None, "admin_id": None, "verify_topic_id": None, "use_llm": False}

async def set_chat_settings(chat_id: int, action_mode: str = None, admin_id: Optional[int] = None,
                            verify_topic_id: Optional[int] = None, use_llm: bool = None):
    current = await get_chat_settings(chat_id)
    new_action = action_mode if action_mode is not None else current["action_mode"]
    new_admin = admin_id if admin_id is not None else current["admin_id"]
    new_topic = verify_topic_id if verify_topic_id is not None else current["verify_topic_id"]
    new_llm = 1 if use_llm is True else (0 if use_llm is False else (1 if current["use_llm"] else 0))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO chat_settings (chat_id, action_mode, admin_id, verify_topic_id, use_llm) VALUES (?, ?, ?, ?, ?)",
            (chat_id, new_action, new_admin, new_topic, new_llm)
        )
        await db.commit()

# Остальные функции (верификация, баны) без изменений
async def add_pending_verification(user_id: int, chat_id: int, message_id: int,
                                   message_thread_id: Optional[int],
                                   expires_at: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pending_verification (user_id, chat_id, message_id, message_thread_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, message_id, message_thread_id, expires_at)
        )
        await db.commit()

async def remove_pending_verification(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM pending_verification WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        await db.commit()

async def get_pending_verification(user_id: int, chat_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT message_id, message_thread_id, expires_at FROM pending_verification WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "message_id": row[0],
                    "message_thread_id": row[1],
                    "expires_at": datetime.fromisoformat(row[2])
                }
            return None

async def get_all_pending_verifications():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id, chat_id, message_id, message_thread_id, expires_at FROM pending_verification"
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"user_id": row[0], "chat_id": row[1], "message_id": row[2],
                     "message_thread_id": row[3], "expires_at": datetime.fromisoformat(row[4])} for row in rows]

async def add_banned_user(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO banned_users (user_id, chat_id, banned_at) VALUES (?, ?, ?)",
            (user_id, chat_id, datetime.now())
        )
        await db.commit()

async def remove_banned_user(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM banned_users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        await db.commit()

async def is_user_banned(user_id: int, chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM banned_users WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)) as cursor:
            return await cursor.fetchone() is not None

async def get_banned_list(chat_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, banned_at FROM banned_users WHERE chat_id = ? ORDER BY banned_at DESC", (chat_id,)) as cursor:
            return await cursor.fetchall()