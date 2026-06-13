import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN в .env файле")

DEFAULT_ACTION_MODE = os.getenv("DEFAULT_ACTION_MODE", "notify_admin")
VERIFICATION_TIMEOUT = int(os.getenv("VERIFICATION_TIMEOUT", "180"))
AUTO_DELETE_UNVERIFIED = os.getenv("AUTO_DELETE_UNVERIFIED", "True").lower() in ("true", "1", "yes")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_ACCESS_ID = os.getenv("LLM_ACCESS_ID", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-3.5-turbo")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "8"))