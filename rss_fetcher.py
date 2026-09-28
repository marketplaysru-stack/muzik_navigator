"""Забирает свежие записи из RSS-лент. Возвращает список последних новостей."""
import logging
import time

import feedparser
import requests

import config

log = logging.getLogger("rss")

USER_AGENT = "Mozilla/5.0 (MusicBot RSS)"


def _fetch_feed(url):
    """Скачивает одну RSS-ленту. При сбое возвращает None."""
    for attempt in range(1, config.RETRY_COUNT + 1):
        try:
            resp = requests.get(
                url,
                timeout=config.TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            log.warning("RSS сеть (%s, попытка %d/%d): %s", url, attempt, config.RETRY_COUNT, e)
            time.sleep(config.BACKOFF_BASE ** attempt)
            continue

        if resp.status_code != 200:
            log.warning("RSS %s вернул %s", url, resp.status_code)
            time.sleep(config.BACKOFF_BASE ** attempt)
            continue

        parsed = feedparser.parse(resp.content)
        if parsed.bozo:
            log.warning("RSS %s: некорректный XML (%s)", url, parsed.bozo_exception)
        return parsed

    return None


def _entry_timestamp(entry):
    """Время публикации в секундах для сортировки. 0, если не удалось."""
    pp = getattr(entry, "published_parsed", None)
    if not pp:
        return 0
    try:
        return time.mktime(pp)
    except (OverflowError, ValueError):
        return 0


def _entry_to_item(entry):
    """Превращает запись RSS в словарь с ключами title/summary/link + служебный _ts."""
    return {
        "title": getattr(entry, "title", "").strip(),
        "summary": getattr(entry, "summary", "").strip(),
        "link": getattr(entry, "link", "").strip(),
        "_ts": _entry_timestamp(entry),
    }


def fetch_latest(feeds, limit=2):
    """Возвращает список из `limit` самых свежих записей по всем лентам.

    feeds — список URL RSS-лент.
    Отдаёт словари {title, summary, link} без служебных полей, новые сверху.
    """
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

    # Новые сверху.
    candidates.sort(key=lambda x: x["_ts"], reverse=True)

    # Убираем дубли по ссылке (одна новость в нескольких лентах).
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

    # Наружу — только то, что ждёт ai_writer.generate_post.
    return [
        {"title": i["title"], "summary": i["summary"], "link": i["link"]}
        for i in result
    ]