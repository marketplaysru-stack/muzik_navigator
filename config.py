import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Корень проекта — папка, где лежит config.py (не зависит от рабочей директории)
BASE_DIR = Path(__file__).resolve().parent

# Токены и ID — только из окружения
VK_TOKEN = os.getenv("VK_ACCESS_TOKEN", "")
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


VK_GROUP_ID = _int(os.getenv("VK_GROUP_ID"), 0)
ADMIN_IDS = [_int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ИИ (Groq)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

# Расписание и время
POST_TIMES = ["06:00", "18:00"]
TIMEZONE = os.getenv("TZ", "Europe/Moscow")

# Файлы данных — абсолютные пути от папки проекта
FEEDS_FILE = str(BASE_DIR / "data" / "feeds.json")
STATE_FILE = str(BASE_DIR / "data" / "state.json")

# VK API
VK_API_VERSION = "5.199"
API_URL = "https://api.vk.com/method/"
TIMEOUT = 15
RETRY_COUNT = 5
BACKOFF_BASE = 2


def validate():
    """Падает сразу и с понятным текстом, если критичное не задано."""
    problems = []
    if not VK_TOKEN:
        problems.append("VK_ACCESS_TOKEN не задан")
    if not TG_TOKEN:
        problems.append("TG_BOT_TOKEN не задан")
    if not VK_GROUP_ID:
        problems.append("VK_GROUP_ID не задан (нужен числовой ID группы БЕЗ минуса)")
    if not ADMIN_IDS:
        problems.append("ADMIN_IDS пуст — пульт не будет отвечать никому")
    if problems:
        sys.exit("Ошибка конфигурации: " + "; ".join(problems))