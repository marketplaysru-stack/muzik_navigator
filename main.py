import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import ai_writer
import config
from rss_fetcher import fetch_latest
from vk_poster import post_to_vk

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

bot = Bot(token=config.TG_TOKEN)
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)


def is_admin(user_id):
    return user_id in config.ADMIN_IDS


def load_feeds():
    if not os.path.exists(config.FEEDS_FILE):
        return []
    with open(config.FEEDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f).get("feeds", [])


def save_feeds(feeds):
    os.makedirs(os.path.dirname(config.FEEDS_FILE), exist_ok=True)
    with open(config.FEEDS_FILE, "w", encoding="utf-8") as f:
        json.dump({"feeds": feeds}, f, ensure_ascii=False, indent=2)


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {"seen_links": []}
    with open(config.STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def publish_once():
    """Тянет свежие новости, переписывает через ИИ и публикует. Возвращает текст или None."""
    feeds = load_feeds()
    if not feeds:
        log.warning("Нет RSS-фидов. Добавь через /addfeed.")
        return None

    items = fetch_latest(feeds, limit=2)
    if not items:
        log.warning("Из фидов ничего не пришло.")
        return None

    # Пропускаем новости, которые уже публиковали
    state = load_state()
    seen = set(state.get("seen_links", []))
    fresh = [i for i in items if i.get("link") and i["link"] not in seen]
    if not fresh:
        log.info("Все свежие новости уже публиковались.")
        return None

    item = fresh[0]
    text = ai_writer.generate_post(item)
    post_id = post_to_vk(config.VK_TOKEN, config.VK_GROUP_ID, text)
    if post_id is None:
        log.error("Публикация не удалась.")
        return None

    seen.add(item["link"])
    state["seen_links"] = list(seen)[-200:]
    save_state(state)
    return text


@router.message(Command("start"))
async def cmd_start(msg: Message):
    if not is_admin(msg.from_user.id):
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
    for t in config.POST_TIMES:
        hour, minute = t.split(":")
        scheduler.add_job(scheduled_publish, "cron", hour=int(hour), minute=int(minute))
    scheduler.start()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Telegram-пульт запущен. Публикации: %s (%s).", ", ".join(config.POST_TIMES), config.TIMEZONE)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")