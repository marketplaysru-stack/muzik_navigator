#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Музыкальный Навигатор. Группа 240656847. Посты в 06:00 и 15:00.
Типы: новость, лайфхак, кейс, викторина, инструмент, промпт, миф/факт, вопрос.
Без картинок — публикуется только текст.
"""

import os, sys, time, json, random, hashlib, logging, requests, schedule, re, threading
from datetime import datetime
import feedparser

def _env(*names, default=""):
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return default

# ================== НАСТРОЙКИ ==================
BOT_NAME   = "Музыкальный Навигатор"
GROUP_ID   = int(os.getenv("MUSIC_GROUP_ID", "240656847"))
POST_TIMES = ["06:00", "15:00"]
VK_API_VER = "5.199"

VK_TOKEN     = _env("VK_TOKEN_MUSIC", "MUSIC_VK_TOKEN", "VK_TOKEN_AI", "VK_TOKEN")
GROQ_API_KEY = _env("GROQ_API_KEY", "GROQ_KEY")

TELEGRAM_TOKEN   = _env("MUSIC_TELEGRAM_TOKEN", "MUSIC_TG_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN")
TELEGRAM_CHAT_ID = _env("MUSIC_CHAT_ID", "MUSIC_TG_CHAT_ID", "TELEGRAM_CHAT_ID", "AI_CHAT_ID")

# ================== ТИПЫ ПОСТОВ ==================
POST_TYPE_WEIGHTS = {
    "tip":      25,
    "case":     20,
    "news":     20,
    "tool":     12,
    "myth":     10,
    "prompt":    6,
    "question":  4,
    "quiz":      3,
}
QUIZ_ANSWER_IN_COMMENT = True
QUIZ_ANSWER_DELAY_SEC = 5

# ================== RSS ==================
RSS_ENABLED = True
RSS_SOURCES = [
    "https://pitchfork.com/rss/news/",
    "https://www.nme.com/news/music/feed",
    "https://www.rollingstone.com/music/music-news/feed/",
    "https://www.billboard.com/feed/",
    "https://pitchfork.com/rss/reviews/albums/",
]
DATA_DIR = "./data"

TOPIC_BLACKLIST = [
    "бесплатных уроков", "вебинар", "вебинары",
    "дайджест", "подборка", "топ-", "top-",
    "курс", "курсы", "обучение", "интенсив",
    "скидк", "акци", "распродаж", "промокод",
    "митап", "конференц", "хакатон",
    "вакансия", "резюме", "ищу работу",
    "запись трансляции",
]
# ================================================================


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [MUSIC] - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger("MUSIC")
os.makedirs(DATA_DIR, exist_ok=True)

STATE = {
    "last_post_topic": None,
    "last_post_time": None,
    "last_post_ok": None,
    "topics_count": 0,
    "last_source": None,
    "last_gen": None,
    "last_type": None,
    "type_counters": {k: 0 for k in POST_TYPE_WEIGHTS},
}
STATE_LOCK = threading.Lock()


# ================== TELEGRAM ==================
def tg_api(method, **params):
    if not TELEGRAM_TOKEN: return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        r = requests.post(url, json=params, timeout=20)
        if r.status_code != 200:
            logger.warning(f"TG {method}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.debug(f"TG {method}: {e}"); return None


def tg_init():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN:
        logger.info("📨 Telegram: токен не задан"); return None
    me = tg_api("getMe")
    if not me or not me.get("ok"):
        logger.warning("📨 Telegram: токен невалиден"); return None
    bot_username = me["result"].get("username", "?")
    logger.info(f"📨 Telegram: бот @{bot_username}")
    tg_api("deleteWebhook")
    if TELEGRAM_CHAT_ID:
        logger.info(f"📨 Telegram chat_id из env: {TELEGRAM_CHAT_ID}")
        return TELEGRAM_CHAT_ID
    upd = tg_api("getUpdates", timeout=1)
    if upd and upd.get("ok") and upd.get("result"):
        for u in reversed(upd["result"]):
            msg = u.get("message") or u.get("edited_message") or {}
            cid = msg.get("chat", {}).get("id")
            if cid:
                TELEGRAM_CHAT_ID = str(cid)
                logger.info(f"📨 Telegram chat_id найден: {TELEGRAM_CHAT_ID}")
                return TELEGRAM_CHAT_ID
    logger.warning(f"📨 Telegram: chat_id неизвестен — напиши /start боту @{bot_username}")
    return None


def tg_send(text, chat_id=None):
    if not TELEGRAM_TOKEN: return
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": cid, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code != 200:
            logger.warning(f"tg_send: HTTP {r.status_code}")
    except Exception as e:
        logger.debug(f"TG send: {e}")


# ================== GROQ ==================
GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
]


def groq_chat(system, user, max_tokens=700, temperature=0.7):
    if not GROQ_API_KEY: return None
    last_err = None
    for model in GROQ_MODELS:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }, timeout=60)
            if r.status_code == 200:
                body = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if body and len(body) > 40:
                    logger.info(f"✅ Groq ответил ({model})")
                    return body
            elif r.status_code == 404:
                logger.warning(f"Groq {model}: 404")
                last_err = f"404 {model}"; continue
            elif r.status_code == 401:
                logger.error("Groq 401 — ключ неверный"); return None
            elif r.status_code == 403:
                last_err = f"403 {model}"; continue
            elif r.status_code == 429:
                last_err = f"429"; continue
            else:
                last_err = f"{r.status_code} {model}"; continue
        except Exception as e:
            last_err = f"{model}: {e}"; continue
    logger.warning(f"❌ Все Groq-модели отказали. Последняя: {last_err}")
    return None


# ================== ДИАГНОСТИКА ==================
def diag_check_groq():
    if not GROQ_API_KEY:
        return "❌ Groq: ключ НЕ задан"
    for model in GROQ_MODELS:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "Скажи ok"}],
                      "max_tokens": 10}, timeout=30)
            if r.status_code == 200:
                return f"✅ Groq: OK ({model})"
            elif r.status_code == 401:
                return "❌ Groq: 401 — ключ неверный"
            elif r.status_code == 403:
                return "❌ Groq: 403 — доступ запрещён"
            elif r.status_code in (429, 404):
                continue
        except Exception as e:
            return f"❌ Groq: {type(e).__name__}: {str(e)[:150]}"
    return "❌ Groq: ни одна модель недоступна"


def diag_vk_group():
    if not VK_TOKEN:
        return "❌ VK group: токен НЕ задан"
    try:
        r = requests.get("https://api.vk.com/method/groups.getById",
                         params={"access_token": VK_TOKEN, "v": VK_API_VER,
                                 "group_id": abs(GROUP_ID)}, timeout=15).json()
        if "response" in r and r["response"].get("groups"):
            g = r["response"]["groups"][0]
            return f"✅ VK group: {g.get('name')} (id={g.get('id')})"
        return f"❌ VK group: {r}"
    except Exception as e:
        return f"❌ VK group: {str(e)[:100]}"


def diag_check_rss():
    ok = 0; total = len(RSS_SOURCES); lines = []
    for url in RSS_SOURCES:
        try:
            f = feedparser.parse(url); n = len(f.entries)
            if n > 0:
                ok += 1; lines.append(f"  ✅ {url.split('/')[2]} — {n}")
            else:
                lines.append(f"  ⚠️ {url.split('/')[2]} — 0")
        except Exception as e:
            lines.append(f"  ❌ {url.split('/')[2]} — {str(e)[:60]}")
    return f"📡 RSS: {ok}/{total}\n" + "\n".join(lines)


def diag_full():
    return "\n".join([
        f"🔧 <b>Диагностика [{BOT_NAME}]</b>", "",
        "<b>Переменные окружения:</b>",
        f"  GROQ_API_KEY: {'✅ есть' if GROQ_API_KEY else '❌ НЕТ'}",
        f"  VK_TOKEN_MUSIC: {'✅ есть' if VK_TOKEN else '❌ НЕТ'}", "",
        "<b>Сервисы:</b>",
        diag_check_groq(),
        diag_vk_group(), "",
        diag_check_rss(),
    ])


# ================== TELEGRAM LOOP ==================
def tg_command_loop():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN: return
    logger.info("📨 Слушаю Telegram: /test, /status, /diag, /groq, /types, /help")
    offset = 0
    upd = tg_api("getUpdates", timeout=1)
    if upd and upd.get("ok") and upd.get("result"):
        offset = upd["result"][-1]["update_id"] + 1
        if not TELEGRAM_CHAT_ID:
            for u in reversed(upd["result"]):
                msg = u.get("message") or u.get("edited_message") or {}
                cid = msg.get("chat", {}).get("id")
                if cid: TELEGRAM_CHAT_ID = str(cid); break
    while True:
        try:
            upd = tg_api("getUpdates", timeout=25, offset=offset)
            if not upd or not upd.get("ok"):
                time.sleep(5); continue
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text: continue
                if not TELEGRAM_CHAT_ID and chat_id:
                    TELEGRAM_CHAT_ID = chat_id
                    tg_send("✅ chat_id сохранён.", chat_id=chat_id); continue
                if chat_id != str(TELEGRAM_CHAT_ID):
                    tg_send("⛔ Нет доступа.", chat_id=chat_id); continue
                parts = text.split(); cmd = parts[0].lower().split("@")[0]
                logger.info(f"📨 Команда: {cmd}")
                if cmd in ("/start", "/help"):
                    tg_send(f"🤖 <b>{BOT_NAME}</b>\n\n"
                            f"/test — пост (случайный тип)\n"
                            f"/test news|tip|case|tool|myth|prompt|question|quiz\n"
                            f"/diag — диагностика\n/groq — проверка Groq\n"
                            f"/status — состояние\n/types — счётчики")
                elif cmd == "/diag":
                    tg_send("🔧 Диагностика...")
                    threading.Thread(target=lambda: tg_send(diag_full()), daemon=True).start()
                elif cmd == "/groq":
                    tg_send("🔎 Проверяю Groq...")
                    def _gr():
                        st = diag_check_groq()
                        ex = ""
                        if st.startswith("✅"):
                            b = groq_chat("Только русский, кратко.",
                                          "Одно предложение про музыку.", max_tokens=80)
                            if b: ex = f"\n\n<b>Текст:</b>\n{b[:300]}"
                        tg_send(f"<b>Groq:</b> {st}{ex}")
                    threading.Thread(target=_gr, daemon=True).start()
                elif cmd == "/status":
                    with STATE_LOCK: s = dict(STATE)
                    counters = ", ".join(f"{k}:{v}" for k, v in s["type_counters"].items())
                    tg_send(f"📊 <b>[{BOT_NAME}]</b>\n\n"
                            f"Тем: {s['topics_count']}\n"
                            f"Последний: {s['last_post_time'] or '—'}\n"
                            f"Тип: {s['last_type'] or '—'}\n"
                            f"Тема: {s['last_post_topic'] or '—'}\n"
                            f"Текст: {s['last_gen'] or '—'}\n"
                            f"Результат: {'✅' if s['last_post_ok'] else '❌' if s['last_post_ok'] is False else '—'}\n\n"
                            f"Счётчики: {counters}")
                elif cmd == "/types":
                    with STATE_LOCK: c = dict(STATE["type_counters"])
                    tg_send("📈 <b>Счётчики</b>\n\n" + "\n".join(f"  {k}: {v}" for k, v in c.items()))
                elif cmd == "/test":
                    forced = parts[1].lower() if len(parts) > 1 else None
                    if forced and forced not in POST_TYPE_WEIGHTS:
                        tg_send(f"❓ Неизвестный тип. Доступные: {', '.join(POST_TYPE_WEIGHTS.keys())}"); continue
                    tg_send(f"🧪 Запускаю пост{' (' + forced + ')' if forced else ''}...")
                    threading.Thread(target=post_now, args=(forced,), daemon=True).start()
                else:
                    tg_send(f"❓ Не знаю <code>{cmd}</code>. Напиши /help")
        except Exception as e:
            logger.error(f"TG loop: {e}"); time.sleep(5)


# ================== ПЕРЕВОД ==================
def _is_ru(text):
    if not text: return False
    cyr = sum(1 for c in text if 'а' <= c.lower() <= 'я')
    return cyr > len(text) * 0.3


def translate(text, source_lang, target_lang):
    if not text: return text
    try:
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": text[:500], "langpair": f"{source_lang}|{target_lang}"},
                         timeout=20)
        if r.status_code == 200:
            t = r.json().get("responseData", {}).get("translatedText", "")
            if t and len(t) > 3: return t
    except Exception as e:
        logger.warning(f"Перевод: {e}")
    return text


def to_ru(t): return t if _is_ru(t) else translate(t, "en", "ru")
def to_en(t): return t if not _is_ru(t) else translate(t, "ru", "en")


def is_topic_ok(title, summary):
    t = (title or "").lower(); s = (summary or "").lower()[:300]
    for bad in TOPIC_BLACKLIST:
        if bad in t: return False
    if t.endswith("?") and len(t) < 30: return False
    return True


# ================== ТЕМЫ ==================
DEFAULT_TOPICS = [
    "Как начать писать музыку дома без студии",
    "Как монетизировать свои треки на стримингах",
    "Что такое сведение и мастеринг простыми словами",
    "Как выбрать первую гитару или синтезатор",
    "Как продвигать свои треки в соцсетях",
    "Как сделать качественную обложку для трека",
    "Как попасть в плейлисты на Spotify и VK Музыке",
    "Сколько стоит записать трек в студии",
    "Как побороть страх сцены начинающему музыканту",
    "Как использовать AI для создания музыки",
    "Как работать с авторскими правами на музыку",
    "Топ-5 бесплатных программ для создания музыки",
]
TOPICS_TIP = [
    "Как записать вокал дома без шумоизоляции",
    "Как настроить микрофон для домашней студии",
    "Как уложиться в бюджет при записи первого трека",
    "Как свести трек, чтобы он звучал громко и чисто",
    "Как придумать цепляющий припев",
    "Как выбрать DAW для начинающего",
    "Как продвигать треки без рекламного бюджета",
    "Как правильно оформить релиз на стримингах",
    "Как сочинять музыку, когда нет вдохновения",
    "Как записать песню с помощью одного микрофона",
]
TOPICS_CASE = [
    "Как парень записал хит у себя дома за выходные",
    "История группы, которая собрала миллион просмотров без лейбла",
    "Как блогер выпустил трек с AI-вокалом и попал в чарты",
    "Как школьник заработал на битмейкинге больше родителей",
    "Как девушка начала петь в 35 и получила контракт",
    "История трека, который стал вирусным в TikTok",
    "Как музыкант из деревни попал на большую сцену",
    "Как диджей собрал первый миллион на стримингах",
]
TOPICS_TOOL = [
    "Suno — как AI создаёт треки за минуту",
    "Udio — конкурент Suno, как пользоваться",
    "BandLab — бесплатная студия в браузере",
    "Audacity — бесплатный редактор звука",
    "Splice — библиотека сэмплов для треков",
    "LANDR — автоматический мастеринг",
    "DistroKid — как выложить трек на все площадки",
    "Spotify for Artists — аналитика для музыканта",
    "Moises — как убрать вокал из трека",
    "Kits.AI — как сделать AI-вокал",
]
TOPICS_PROMPT = [
    "Напиши текст песни о первой любви",
    "Придумай концепцию альбома в стиле lo-fi",
    "Сгенерируй идею для клипа на трек",
    "Помоги написать припев для рок-баллады",
    "Опиши свой идеальный музыкальный вечер",
    "Предложи название для инди-группы",
]
TOPICS_MYTH = [
    "Чтобы писать музыку, нужно музыкальное образование",
    "Хороший трек невозможно записать дома",
    "Без лейбла пробиться невозможно",
    "В музыке зарабатывают только звёзды",
    "AI-музыка — это не настоящая музыка",
    "Нужна дорогая техника, чтобы звучать хорошо",
    "Для стримингов нужно пройти сложный отбор",
]
TOPICS_QUESTION = [
    "Какой трек вы слушаете прямо сейчас?",
    "Какая группа изменила вашу жизнь?",
    "Какой жанр музыки вам ближе всего?",
    "Какую песню вы бы хотели перепеть?",
    "Что для вас важнее — текст или мелодия?",
    "Какой концерт запомнился вам больше всего?",
]


# ================== ПРОМПТЫ ==================
BASE_RULES = (
    "Ты — автор постов для русскоязычного музыкального сообщества. "
    "Твои читатели — обычные люди: слушатели, начинающие музыканты, продюсеры. "
    "Пиши ТАК ПРОСТО, чтобы понял любой.\n\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1. Максимум 12-15 слов в предложении.\n"
    "2. Обязательно конкретный пример или факт.\n"
    "3. В конце — простой вопрос читателю.\n"
    "4. Только русский. Без markdown, без хештегов, без эмодзи в тексте.\n"
    "5. Не начинай с 'В этом материале', 'Сегодня', 'Разбираем'."
)


def gen_news(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Новость»: 5-6 предложений. "
        "Перескажи так, будто объясняешь другу."
    )
    return groq_chat(system, f"Новость: {topic}\nКонтекст: {summary[:700] if summary else '(нет)'}\nНапиши простой пост.")


def gen_tip(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Лайфхак»: 4-5 предложений. Начни с проблемы: «Знакомо, когда…». "
        "Дай ОДИН конкретный приём по шагам. В конце — вопрос."
    )
    return groq_chat(system, f"Тема лайфхака: {topic}\nНапиши простой пост.")


def gen_case(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Реальный кейс»: 5-6 предложений от третьего лица. "
        "Что было нужно → что человек сделал → какой результат."
    )
    return groq_chat(system, f"Кейс: {topic}\nНапиши пост по формату.")


def gen_quiz(topic, summary=""):
    system = (
        "Ты — автор музыкальной викторины для обычных людей.\n\n"
        "ФОРМАТ (строго, без markdown):\n"
        "Вопрос: <простой вопрос>\n"
        "A) <вариант>\nB) <вариант>\nC) <вариант>\nD) <вариант>\n"
        "Пиши ответ в комментариях 👇\n\n"
        "Затем с новой строки строго:\nОТВЕТ: <буква> — <пояснение>\n\nТолько русский."
    )
    body = groq_chat(system, f"Тема: {topic}\nСоставь простую викторину.", max_tokens=500, temperature=0.8)
    if not body: return None, None
    m = re.split(r"\n*\s*ОТВЕТ\s*:\s*", body, flags=re.IGNORECASE)
    if len(m) == 2:
        return m[0].strip(), "✅ Ответ: " + m[1].strip()
    return body.strip(), None


def gen_tool(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Инструмент простыми словами»: 5-6 предложений. "
        "Что это → 2-3 примера → для кого → приём → вопрос."
    )
    return groq_chat(system, f"Инструмент: {topic}\nНапиши простой пост.")


def gen_prompt(topic, summary=""):
    system = (
        "Ты — автор простых постов про AI для музыкантов.\n\n"
        "Формат «Промпт дня» (без markdown):\n"
        "1. Зачем промпт — одно предложение.\n"
        "2. Пустая строка.\n"
        "3. Сам промпт в «ёлочках».\n"
        "4. Пустая строка.\n"
        "5. Что подставить + вопрос.\n\nТолько русский."
    )
    return groq_chat(system, f"Задача: {topic}\nСоставь простой промпт-пост.")


def gen_myth(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Миф vs Факт»: 5-6 предложений. Начни «Миф: ...». "
        "Далее «На самом деле: ...» — с бытовым примером. В конце — вопрос."
    )
    return groq_chat(system, f"Миф для разбора: {topic}\nНапиши пост.")


def gen_question(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Вопрос подписчикам»: 3-4 предложения. Короткий контекст + вопрос."
    )
    return groq_chat(system, f"Тема вопроса: {topic}\nНапиши пост.")


# ================== РОТАЦИЯ ==================
def choose_post_type():
    return random.choices(list(POST_TYPE_WEIGHTS.keys()),
                          weights=list(POST_TYPE_WEIGHTS.values()), k=1)[0]


def get_topic_for_type(ptype):
    if ptype == "news": return get_news_topic()
    if ptype == "tip":
        t = random.choice(TOPICS_TIP); return t, "", "TIPS", to_en(t)
    if ptype == "case":
        t = random.choice(TOPICS_CASE); return t, "", "CASES", to_en(t)
    if ptype == "tool":
        t = random.choice(TOPICS_TOOL); return t, "", "TOOLS", to_en(t)
    if ptype == "prompt":
        t = random.choice(TOPICS_PROMPT); return t, "", "PROMPTS", to_en(t)
    if ptype == "myth":
        t = random.choice(TOPICS_MYTH); return t, "", "MYTHS", to_en(t)
    if ptype == "question":
        t = random.choice(TOPICS_QUESTION); return t, "", "QUESTIONS", to_en(t)
    if ptype == "quiz":
        t = random.choice(random.choice([DEFAULT_TOPICS, TOPICS_MYTH, TOPICS_TIP]))
        return t, "", "QUIZ", to_en(t)
    return get_news_topic()


def check_rss_sources():
    if not RSS_ENABLED or not RSS_SOURCES:
        logger.info("📡 RSS отключён"); return 0
    logger.info(f"📡 Проверяю RSS ({len(RSS_SOURCES)} шт.)...")
    total = 0
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            n = len(feed.entries); total += n
            logger.info(f"  {'✅' if n else '⚠️'} {url} — тем: {n}")
        except Exception as e:
            logger.error(f"  ❌ {url} — {e}")
    logger.info(f"📡 Всего тем из RSS: {total}")
    return total


def get_news_topic():
    raw = []
    if RSS_ENABLED and RSS_SOURCES:
        for url in RSS_SOURCES:
            try:
                feed = feedparser.parse(url)
                for e in feed.entries[:8]:
                    title = getattr(e, "title", "").strip()
                    summary = getattr(e, "summary", "") or getattr(e, "description", "")
                    summary = re.sub(r"<[^>]+>", " ", summary or "").strip()
                    if title and 15 < len(title) < 220:
                        raw.append((title, summary, url))
            except Exception as e:
                logger.warning(f"RSS {url}: {e}")
    candidates = [c for c in raw if is_topic_ok(c[0], c[1])]
    logger.info(f"📚 Из RSS: {len(raw)}, отфильтровано: {len(raw)-len(candidates)}, осталось: {len(candidates)}")
    if not candidates and os.path.exists("topics.txt"):
        with open("topics.txt", "r", encoding="utf-8") as f:
            candidates = [(l.strip(), "", "topics.txt") for l in f if l.strip() and not l.startswith("#")]
    if not candidates:
        logger.warning("Нет тем — запасной список")
        candidates = [(t, "", "DEFAULT") for t in DEFAULT_TOPICS]
    with STATE_LOCK:
        STATE["topics_count"] = len(candidates)
    topic_orig, summary, source = random.choice(candidates)
    return to_ru(topic_orig), summary, source, (topic_orig if not _is_ru(topic_orig) else to_en(topic_orig))


# ================== ФОЛБЭК ==================
FALLBACK_BY_TYPE = {
    "tip": ["Хочешь записать трек дома, но «звук не тот»?\n\n"
            "Начни с одного: обработай вокал компрессором и добавь реверб.\n\n"
            "А вы что используете для вокала? 👇"],
    "case": ["Один парень записал трек дома за выходные — и он завирусился в TikTok.\n\n"
             "Сделал сэмпл из старой песни, добавил бит. Через неделю — 200 тысяч прослушиваний.\n\n"
             "А вы пробовали делать сэмплы? 👇"],
    "tool": ["Suno — нейросеть, которая создаёт треки за 30 секунд по описанию.\n\n"
             "Попроси: «лёгкий lo-fi для учёбы» или «энергичный рок».\n\n"
             "А вы уже пробовали? 👇"],
    "prompt": ["Промпт, чтобы сочинить текст песни:\n\n"
               "«Напиши текст песни о первой любви в стиле Цоя. Куплет, припев, повтор».\n\n"
               "Какую песню хотели бы написать? 👇"],
    "myth": ["Миф: чтобы писать музыку, нужно музыкальное образование.\n\n"
             "На самом деле: миллионы треков записаны людьми без диплома.\n\n"
             "А вы как думали? 👇"],
    "question": ["Интересно ваше мнение: какой трек вы слушаете последние дни?\n\n"
                 "Пишите в комментариях 👇"],
    "quiz": ["Вопрос: сколько струн у классической гитары?\n"
             "A) 4\nB) 6\nC) 7\nD) 12\nПиши ответ в комментариях 👇"],
    "news": ["Свежая новость из мира музыки. Расскажу простыми словами.\n\nЧто думаете? 👇"],
}
QUIZ_FALLBACK_ANSWER = "✅ Ответ: B) 6 — у классической гитары шесть струн."


def smart_fallback(topic, summary="", ptype="news"):
    if summary:
        summary_ru = to_ru(summary[:500])
        sents = re.split(r"(?<=[.!?])\s+", summary_ru)
        useful = [s.strip() for s in sents if 40 < len(s.strip()) < 300][:3]
        facts = " ".join(useful)[:500]
        if facts:
            return f"{facts}\n\nЧто думаете по теме? 👇"
    return random.choice(FALLBACK_BY_TYPE.get(ptype, FALLBACK_BY_TYPE["news"]))


# ================== VK ==================
def vk_api_call(method, **params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VER})
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
        if "error" in r:
            return None, r["error"]
        return r.get("response"), None
    except Exception as e:
        return None, {"error_code": -1, "error_msg": str(e)}


def publish(text):
    """Публикует только текст — без фото, без Flood control."""
    resp, err = vk_api_call("wall.post",
                            owner_id=GROUP_ID,
                            from_group=1,
                            message=text)
    if err:
        logger.error(f"VK wall.post: {err.get('error_code')} — {err.get('error_msg')}")
        return False, None
    if resp and "post_id" in resp:
        pid = resp["post_id"]
        logger.info(f"✅ Пост опубликован (post_id={pid})")
        return True, pid
    return False, None


def post_comment(post_id, text):
    resp, err = vk_api_call("wall.createComment",
                            owner_id=GROUP_ID, post_id=post_id,
                            from_group=1, message=text)
    if err:
        logger.error(f"VK comment: {err.get('error_code')}")
        return False
    logger.info(f"💬 Комментарий к посту {post_id}")
    return True


# ================== ФОРМАТ ==================
TYPE_EMOJI = {
    "news":     ["🎵", "🎶", "📰", "🎤"],
    "tip":      ["💡", "🎼", "🎧", "🎹"],
    "case":     ["🎤", "🌟", "📖", "🎬"],
    "quiz":     ["🎯", "❓", "🧩", "🏆"],
    "tool":     ["🧰", "🛠", "🎚", "🎛"],
    "prompt":   ["✍️", "🎨", "📝", "🎼"],
    "myth":     ["🔍", "❌", "✅", "🎵"],
    "question": ["❓", "💬", "🎤", "👇"],
}
TYPE_HASHTAGS = {
    "news":     "#музыка #новости #навигатор",
    "tip":      "#музыка #лайфхак #советы",
    "case":     "#музыка #история #успех",
    "quiz":     "#музыка #викторина",
    "tool":     "#музыка #инструменты #сервисы",
    "prompt":   "#музыка #промпты #AI",
    "myth":     "#музыка #мифы #факты",
    "question": "#музыка #обсуждение",
}


def format_post(topic, body, ptype):
    e = random.choice(TYPE_EMOJI.get(ptype, ["🎵"]))
    tags = TYPE_HASHTAGS.get(ptype, "#музыка #навигатор")
    return f"{e} {topic}\n\n{body}\n\n{tags}"


# ================== ЦИКЛ ==================
def post_now(forced_type=None):
    ptype = forced_type or choose_post_type()
    logger.info(f"⏰ [{BOT_NAME}] Запуск постинга (тип: {ptype})")

    topic_ru, summary, source, topic_en = get_topic_for_type(ptype)
    logger.info(f"📝 Тема RU: {topic_ru}")
    logger.info(f"📎 Источник: {source}")

    quiz_answer = None
    gen_name = "groq"

    if ptype == "news":
        body = gen_news(topic_ru, summary)
        if not body:
            logger.info("↪️ News упал, пробую tip")
            ptype = "tip"; body = gen_tip(topic_ru)
    elif ptype == "tip": body = gen_tip(topic_ru)
    elif ptype == "case": body = gen_case(topic_ru)
    elif ptype == "quiz": body, quiz_answer = gen_quiz(topic_ru)
    elif ptype == "tool": body = gen_tool(topic_ru)
    elif ptype == "prompt": body = gen_prompt(topic_ru)
    elif ptype == "myth": body = gen_myth(topic_ru)
    elif ptype == "question": body = gen_question(topic_ru)
    else: body = gen_news(topic_ru, summary)

    if not body:
        logger.warning(f"⚠️ Groq не ответил — фолбэк для {ptype}")
        gen_name = "fallback"
        body = smart_fallback(topic_ru, summary, ptype)
        if ptype == "quiz" and not quiz_answer:
            quiz_answer = QUIZ_FALLBACK_ANSWER

    text = format_post(topic_ru, body, ptype)
    logger.info(f"📄 Текст ({len(text)} симв.) от {gen_name}: {text[:150]}...")

    # Картинки отключены — публикуем сразу
    ok, post_id = publish(text)

    if ok and ptype == "quiz" and quiz_answer and QUIZ_ANSWER_IN_COMMENT and post_id:
        time.sleep(QUIZ_ANSWER_DELAY_SEC)
        post_comment(post_id, quiz_answer)

    with STATE_LOCK:
        STATE["last_post_topic"] = topic_ru
        STATE["last_post_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        STATE["last_post_ok"] = ok
        STATE["last_source"] = source
        STATE["last_gen"] = gen_name
        STATE["last_type"] = ptype
        STATE["type_counters"][ptype] = STATE["type_counters"].get(ptype, 0) + 1

    if ok:
        tg_send(f"🚀 <b>[{BOT_NAME}]</b> Пост ({ptype})\n"
                f"<b>Тема:</b> {topic_ru}\n"
                f"<b>Текст:</b> {gen_name}\n\n"
                f"{text[:800]}")
    else:
        tg_send(f"❌ <b>[{BOT_NAME}]</b> Не опубликовано ({ptype})")

    logger.info(f"🏁 [{BOT_NAME}] Цикл завершён ({ptype})\n")


def main():
    logger.info(f"🚀 {BOT_NAME} запущен")
    logger.info(f"📡 Группа: {GROUP_ID}")
    logger.info(f"⏰ Расписание: {', '.join(POST_TIMES)}")
    logger.info(f"🔑 VK group: {'есть' if VK_TOKEN else 'НЕТ'}")
    logger.info(f"🔑 Groq: {'есть' if GROQ_API_KEY else 'НЕТ'}")
    logger.info(f"🎲 Типы постов: {POST_TYPE_WEIGHTS}")

    tg_init()
    if TELEGRAM_TOKEN:
        threading.Thread(target=tg_command_loop, daemon=True).start()
        if TELEGRAM_CHAT_ID:
            tg_send(f"🟢 <b>[{BOT_NAME}]</b> запущен\n"
                    f"Groq: {'✅' if GROQ_API_KEY else '❌'}\n"
                    f"Картинки отключены\n"
                    f"/test · /diag · /groq · /types · /status")

    check_rss_sources()
    for t in POST_TIMES:
        try:
            schedule.every().day.at(t).do(post_now)
            logger.info(f"  → задача на {t}")
        except Exception as e:
            logger.error(f"Время {t}: {e}")
    while True:
        schedule.run_pending(); time.sleep(30)


if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error("❌ Нет VK_TOKEN_MUSIC / VK_TOKEN_AI"); sys.exit(1)
    try: main()
    except KeyboardInterrupt: logger.info("Остановлено")