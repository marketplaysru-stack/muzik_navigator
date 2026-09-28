import logging
import time

import requests

import config

log = logging.getLogger("ai")

GROQ_URL = config.GROQ_BASE_URL.rstrip("/") + "/chat/completions"

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


def generate_post(item):
    """Пишет текст поста через Groq. При сбое — fallback на заголовок+ссылку."""
    if not item:
        return "Музыкальный навигатор: свежий пост уже в пути."

    if not config.GROQ_API_KEY:
        log.warning("GROQ_API_KEY не задан — публикую без ИИ.")
        return _fallback(item)

    prompt = PROMPT.format(
        title=item.get("title", ""),
        summary=item.get("summary", "")[:2000],  # обрезаем, чтобы не сжечь лимит токенов
    )
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        "max_tokens": 500,
    }

    for attempt in range(1, config.RETRY_COUNT + 1):
        try:
            resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=config.TIMEOUT)
        except requests.RequestException as e:
            log.warning("Groq сеть (попытка %d/%d): %s", attempt, config.RETRY_COUNT, e)
            time.sleep(config.BACKOFF_BASE ** attempt)
            continue

        if resp.status_code != 200:
            log.error("Groq ошибка %s: %s", resp.status_code, resp.text[:300])
            # 401 — неверный ключ, 404 — неверная модель, 429 — превышен лимит
            time.sleep(config.BACKOFF_BASE ** attempt)
            continue

        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return _add_source(text, item.get("link", ""))

    log.error("Groq не ответил после %d попыток — публикую без ИИ.", config.RETRY_COUNT)
    return _fallback(item)


def _fallback(item):
    title = item.get("title", "Без заголовка")
    link = item.get("link", "")
    return f"{title}\n\nИсточник: {link}"


def _add_source(text, link):
    if link and link not in text:
        return f"{text}\n\nИсточник: {link}"
    return text

