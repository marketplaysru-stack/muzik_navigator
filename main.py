"""
Музыкальный постер VK — всё в одном файле.
Берёт новости из RSS-лент, переписывает через Groq (ИИ), публикует в VK.
Управление — Telegram-пульт (aiogram).
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import feedparser
import requests
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════
#  КОНФИГУРАЦИЯ (бывший config.py)
# ═══════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


VK_TOKEN       = os.getenv("VK_ACCESS_TOKEN", "")
TG_TOKEN       = os.getenv("TG_BOT_TOKEN", "")
VK_GROUP_ID    = _int(os.getenv("VK_GROUP_ID"), 0)
ADMIN_IDS      = [_int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL     = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL  = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

POST_TIMES     = ["06:00", "18:00"]
TIMEZONE       = os.getenv("TZ", "Europe/Moscow")

FEEDS_FILE     = str(BASE_DIR / "data" / "feeds.json")
STATE_FILE     = str(BASE_DIR / "data" / "state.json")

VK_API_VERSION = "5.199"
API_URL        = "https://api.vk.com/method/"

TIMEOUT        = 15
RETRY_COUNT    = 5
BACKOFF_BASE   = 2

PROXY_URL      = os.getenv("PROXY_URL", "")        # http://user:pass@host:port
USER_AGENT     = "Mozilla/5.0 (MusicBot RSS)"

GROQ_URL       = GROQ_BASE_URL.rstrip("/") + "/chat/completions"

PROMPT = (
    "Ты — редактор музыкального сообщества ВКонтакте. "
    "Ниже — новость на английском из зарубежного издания. "
    "Переведи её на русский и перескажи своими словами: не копируй и не переводи дословно, "
    "изложи суть так, как рассказал бы подписчику. Добавь короткий свой взгляд или контекст. "
    "Текст до 600 символов, живой лёгкий тон, без воды и канцелярита. "
    "В конце добавь 2-4 хэштега по теме. "
    "Верни только готовый текст поста, без пояснений и без слова «Источник».\n\n"
    "Заголовок: {title}\n"
    "Содержание: {summary}"
)

# ═══════════════════════════════════════════
#  ПРОВЕРКА КРИТИЧНЫХ ПЕРЕМЕННЫХ
# ═══════════════════════════════════════════

def validate():
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

validate()

# ═══════════════════════════════════════════
#  ЛОГГЕР
# ═══════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

# ═══════════════════════════════════════════
#  RSS-ФЕТЧЕР (бывший rss_fetcher.py)
# ═══════════════════════════════════════════

def _fetch_feed(url):
    """Скачивает одну RSS-ленту. При сбое возвращает None."""
    rss_log = logging.getLogger("rss")
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            rss_log.warning("RSS сеть (%s, попытка %d/%d): %s", url, attempt, RETRY_COUNT, e)
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        if resp.status_code != 200:
            rss_log.warning("RSS %s вернул %s", url, resp.status_code)
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo:
            rss_log.warning("RSS %s: некорректный XML (%s)", url, parsed.bozo_exception)
        return parsed

    return None


def _entry_timestamp(entry):
    pp = getattr(entry, "published_parsed", None)
    if not pp:
        return 0
    try:
        return time.mktime(pp)
    except (OverflowError, ValueError):
        return 0


def _entry_to_item(entry):
    return {
        "title": getattr(entry, "title", "").strip(),
        "summary": getattr(entry, "summary", "").strip(),
        "link": getattr(entry, "link", "").strip(),
        "_ts": _entry_timestamp(entry),
    }


def fetch_latest(feeds, limit=2):
    """Возвращает список из `limit` самых свежих записей по всем лентам."""
    candidates = []
    for url in feeds:
        parsed = _fetch_feed(url)
        if not parsed or not parsed.entries:
            continue
        for entry in parsed.entries:
            candidates.append(_entry_to_item(entry))

    if not candidates:
        log.error("Не удалось получить ни одной записи из лент.")
        return []

    candidates.sort(key=lambda x: x["_ts"], reverse=True)

    result = []
    seen_links = set()
    for c in candidates:
        if c["link"] and c["link"] in seen_links:
            continue
        if c["link"]:
            seen_links.add(c["link"])
        result.append(c)
        if len(result) >= limit:
            break

    return [
        {"title": i["title"], "summary": i["summary"], "link": i["link"]}
        for i in result
    ]


# ═══════════════════════════════════════════
#  VK-ПОСТЕР (бывший vk_poster.py)
# ═══════════════════════════════════════════

def vk_call(token, method, params):
    """Один вызов VK API с ретраями и backoff."""
    vk_log = logging.getLogger("vk")
    url = API_URL + method
    data = {**params, "access_token": token, "v": VK_API_VERSION}
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.post(url, data=data, timeout=TIMEOUT)
            result = resp.json()
        except requests.RequestException as e:
            vk_log.warning("Сетевая ошибка (попытка %d/%d): %s", attempt, RETRY_COUNT, e)
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        if "error" in result:
            err = result["error"]
            vk_log.error("VK ошибка %s: %s", err.get("error_code"), err.get("error_msg"))
            if err.get("error_code") == 9:
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            return None
        return result.get("response")
    return None


def post_to_vk(token, group_id, text):
    """Публикует пост на стене группы. Возвращает post_id или None."""
    resp = vk_call(token, "wall.post", {
        "owner_id": -group_id,
        "from_group": 1,
        "message": text,
    })
    if resp:
        post_id = resp.get("post_id")
        log.info("Опубликован пост id=%s", post_id)
        return post_id
    return None


# ═══════════════════════════════════════════
#  AI-РЕДАКТОР (бывший ai_writer.py)
# ═══════════════════════════════════════════

def _fallback_post(item):
    title = item.get("title", "Без заголовка")
    link = item.get("link", "")
    return f"{title}\n\nИсточник: {link}"


def _add_source(text, link):
    if link and link not in text:
        return f"{text}\n\nИсточник: {link}"
    return text


def generate_post(item):
    """Пишет текст поста через Groq. При сбое — fallback."""
    if not item:
        return "Музыкальный навигатор: свежий пост уже в пути."

    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY не задан — публикую без ИИ.")
        return _fallback_post(item)

    prompt = PROMPT.format(
        title=item.get("title", ""),
        summary=item.get("summary", "")[:2000],
    )
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 500,
    }

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("Groq сеть (попытка %d/%d): %s", attempt, RETRY_COUNT, e)
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        if resp.status_code != 200:
            log.error("Groq ошибка %s: %s", resp.status_code, resp.text[:300])
            time.sleep(BACKOFF_BASE ** attempt)
            continue

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return _add_source(text, item.get("link", ""))

    log.error("Groq не ответил после %d попыток — публикую без ИИ.", RETRY_COUNT)
    return _fallback_post(item)


# ═══════════════════════════════════════════
#  TELEGRAM-ПУЛЬТ (бывший main.py)
# ═══════════════════════════════════════════

session = AiohttpSession(proxy=PROXY_URL or None)
bot = Bot(token=TG_TOKEN, session=session)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def load_feeds():
    if not os.path.exists(FEEDS_FILE):
        return []
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("feeds", [])


def save_feeds(feeds):
    os.makedirs(os.path.dirname(FEEDS_FILE), exist_ok=True)
    with open(FEEDS_FILE, "w", encoding="utf-8") as f:
        json.dump({"feeds": feeds}, f, ensure_ascii=False, indent=2)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_links": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def publish_once():
    feeds = load_feeds()
    if not feeds:
        log.warning("Нет RSS-фидов. Добавь через /addfeed.")
        return None

    items = fetch_latest(feeds, limit=2)
    if not items:
        log.warning("Из фидов ничего не пришло.")
        return None

    state = load_state()
    seen = set(state.get("seen_links", []))
    fresh = [i for i in items if i.get("link") and i["link"] not in seen]
    if not fresh:
        log.info("Все свежие новости уже публиковались.")
        return None

    item = fresh[0]
    text = generate_post(item)
    post_id = post_to_vk(VK_TOKEN, VK_GROUP_ID, text)
    if post_id is None:
        log.error("Публикация не удалась.")
        return None

    seen.add(item["link"])
    state["seen_links"] = list(seen)[-200:]
    save_state(state)
    return text


# --- Команды ---

@router.message(Command("start"))
async def cmd_start(msg: Message):
    log.info("Пришла команда /start от user_id=%s", msg.from_user.id)
    if not is_admin(msg.from_user.id):
        await msg.answer("Доступ закрыт. Твой ID: %s" % msg.from_user.id)
        return
    await msg.answer(
        "Пульт музыкального постера VK.\n"
        "/post — опубликовать сейчас\n"
        "/status — состояние\n"
        "/feeds — список фидов\n"
        "/addfeed URL — добавить фид\n"
        "/rmfeed URL — удалить фид"
    )


@router.message(Command("post"))
async def cmd_post(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    text = await asyncio.to_thread(publish_once)
    await msg.answer(text if text else "Нечего публиковать — проверь фиды или повторы.")


@router.message(Command("status"))
async def cmd_status(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    feeds = load_feeds()
    state = load_state()
    next_runs = ", ".join(str(j.next_run_time) for j in scheduler.get_jobs())
    await msg.answer(
        f"Фидов: {len(feeds)}\n"
        f"Опубликовано уникальных: {len(state.get('seen_links', []))}\n"
        f"Следующие запуски: {next_runs}"
    )


@router.message(Command("feeds"))
async def cmd_feeds(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    feeds = load_feeds()
    await msg.answer("\n".join(feeds) if feeds else "Фидов пока нет.")


@router.message(Command("addfeed"))
async def cmd_addfeed(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Формат: /addfeed URL")
        return
    feeds = load_feeds()
    if parts[1] not in feeds:
        feeds.append(parts[1])
        save_feeds(feeds)
    await msg.answer("Фид добавлен.")


@router.message(Command("rmfeed"))
async def cmd_rmfeed(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Формат: /rmfeed URL")
        return
    feeds = load_feeds()
    if parts[1] in feeds:
        feeds.remove(parts[1])
        save_feeds(feeds)
        await msg.answer("Фид удалён.")
    else:
        await msg.answer("Такого фида нет.")


async def scheduled_publish():
    await asyncio.to_thread(publish_once)


async def main():
    for t in POST_TIMES:
        hour, minute = t.split(":")
        scheduler.add_job(scheduled_publish, "cron", hour=int(hour), minute=int(minute))
    scheduler.start()
    dp.include_router(router)

    try:
        await bot.delete_webhook(drop_pending_updates=True, request_timeout=30)
    except Exception as e:
        log.warning("Не удалось сбросить вебхук (%s) — продолжаю запуск.", e)

    log.info("Telegram-пульт запущен. Публикации: %s (%s).", ", ".join(POST_TIMES), TIMEZONE)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")