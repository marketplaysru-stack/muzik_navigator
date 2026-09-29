#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Музыкальный Навигатор. Группа 240656847. Посты в 09:00 и 15:00.
Один групповой токен. Без картинок (VK API не даёт фото с групповым токеном).
"""

import os, sys, time, json, random, hashlib, logging, requests, re, threading
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
POST_TIMES = ["09:00", "15:00"]
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
]
# ================================================================


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [MUSIC] - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
os.makedirs(DATA_DIR, exist_ok=True)
logger = logging.getLogger("MUSIC")

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


# ================== VK: ДИАГНОСТИКА ТОКЕНА ==================
def vk_token_info():
    """Проверяет, что за токен и к какой группе привязан."""
    if not VK_TOKEN:
        logger.error("❌ VK_TOKEN не задан")
        return False

    logger.info(f"🔑 VK токен: {VK_TOKEN[:15]}... (длина {len(VK_TOKEN)})")

    # Проверка 1: это групповой токен?
    try:
        r = requests.get("https://api.vk.com/method/groups.getById",
                         params={"access_token": VK_TOKEN,
                                 "v": VK_API_VER,
                                 "group_id": abs(GROUP_ID)},
                         timeout=15).json()
        if "error" in r:
            err = r["error"]
            logger.error(f"❌ VK groups.getById: {err.get('error_code')} — {err.get('error_msg')}")
            return False
        groups = r.get("response", {}).get("groups", [])
        if groups:
            g = groups[0]
            logger.info(f"✅ VK токен знает группу: '{g.get('name')}' (id={g.get('id')})")
            if g.get("id") != abs(GROUP_ID):
                logger.error(f"⚠️ ВНИМАНИЕ! Токен от группы {g.get('id')}, "
                             f"а публикуем в {abs(GROUP_ID)}. Несовпадение!")
                return False
        else:
            logger.error("❌ groups.getById вернул пустой список")
            return False
    except Exception as e:
        logger.error(f"❌ VK groups.getById: {type(e).__name__}: {e}")
        return False

    # Проверка 2: получаем имя токена (какой пользователь/группа)
    try:
        r = requests.get("https://api.vk.com/method/users.get",
                         params={"access_token": VK_TOKEN, "v": VK_API_VER},
                         timeout=15).json()
        if "response" in r and r["response"]:
            u = r["response"][0]
            logger.info(f"   токен принадлежит: {u.get('first_name','')} {u.get('last_name','')} (id={u.get('id')})")
        elif "error" in r:
            err = r["error"]
            # Это нормально для группового токена — users.get не работает
            if err.get("error_code") == 5 or "group" in str(err.get("error_msg", "")).lower():
                logger.info("   (групповой токен — users.get недоступен, это норма)")
            else:
                logger.warning(f"   users.get: {err.get('error_code')} — {err.get('error_msg')}")
    except Exception as e:
        logger.debug(f"users.get: {e}")

    return True


def vk_api_call(method, **params):
    """Вызов VK API с одним токеном."""
    params.update({"access_token": VK_TOKEN, "v": VK_API_VER})
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
        if "error" in r:
            return None, r["error"]
        return r.get("response"), None
    except Exception as e:
        return None, {"error_code": -1, "error_msg": str(e)}


def publish(text):
    """Публикует текст в группу. Возвращает (ok, post_id)."""
    # Пробуем с owner_id=-GROUP_ID и from_group=1
    resp, err = vk_api_call("wall.post",
                            owner_id=-abs(GROUP_ID),
                            from_group=1,
                            message=text)

    if err:
        code = err.get("error_code")
        msg = err.get("error_msg")
        logger.error(f"VK wall.post (from_group=1): {code} — {msg}")

        # Если 214 — пробуем без from_group
        if code == 214:
            logger.info("↪️ Пробую без from_group=1...")
            resp, err = vk_api_call("wall.post",
                                    owner_id=-abs(GROUP_ID),
                                    message=text)
            if err:
                code2 = err.get("error_code")
                msg2 = err.get("error_msg")
                logger.error(f"VK wall.post (без from_group): {code2} — {msg2}")
                return False, None

    if resp and "post_id" in resp:
        pid = resp["post_id"]
        logger.info(f"✅ Пост опубликован (post_id={pid})")
        return True, pid

    logger.error(f"❌ VK wall.post без ошибки, но и без post_id: {resp}")
    return False, None


# ================== TELEGRAM ==================
def tg_api(method, **params):
    if not TELEGRAM_TOKEN: return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        r = requests.post(url, json=params, timeout=30)
        if r.status_code != 200:
            logger.warning(f"TG {method}: HTTP {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        logger.warning(f"TG {method}: {type(e).__name__}: {e}")
        return None


def tg_init():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN:
        logger.info("📨 Telegram: токен не задан")
        return None

    me = tg_api("getMe")
    if me and me.get("ok"):
        logger.info(f"📨 Telegram: бот @{me['result'].get('username','?')} — OK")
        tg_api("deleteWebhook")
    else:
        logger.warning("📨 Telegram: getMe не прошёл (сеть/таймаут), продолжаю")

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
    return None


def tg_send(text, chat_id=None):
    if not TELEGRAM_TOKEN:
        return
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": cid, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=20)
        if r.status_code != 200:
            logger.warning(f"tg_send: HTTP {r.status_code} — {r.text[:150]}")
    except Exception as e:
        logger.warning(f"tg_send: {type(e).__name__}: {e}")


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
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [
                          {"role": "system", "content": system},
                          {"role": "user", "content": user},
                      ],
                      "temperature": temperature,
                      "max_tokens": max_tokens}, timeout=60)
            if r.status_code == 200:
                body = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if body and len(body) > 40:
                    logger.info(f"✅ Groq ответил ({model})")
                    return body
            elif r.status_code == 404:
                last_err = f"404 {model}"; continue
            elif r.status_code == 401:
                logger.error("Groq 401 — ключ неверный"); return None
            elif r.status_code == 403:
                last_err = f"403 {model}"; continue
            elif r.status_code == 429:
                last_err = f"429 {model}"; continue
            else:
                last_err = f"{r.status_code} {model}"; continue
        except Exception as e:
            last_err = f"{model}: {e}"; continue
    logger.warning(f"❌ Все Groq-модели отказали. Последняя: {last_err}")
    return None


# ================== ДИАГНОСТИКА ==================
def diag_vk():
    if not VK_TOKEN:
        return "❌ VK: токен НЕ задан"
    try:
        r = requests.get("https://api.vk.com/method/groups.getById",
                         params={"access_token": VK_TOKEN, "v": VK_API_VER,
                                 "group_id": abs(GROUP_ID)}, timeout=15).json()
        if "response" in r and r["response"].get("groups"):
            g = r["response"]["groups"][0]
            return f"✅ VK: {g.get('name')} (id={g.get('id')})"
        return f"❌ VK: {r}"
    except Exception as e:
        return f"❌ VK: {str(e)[:100]}"


def diag_groq():
    if not GROQ_API_KEY:
        return "❌ Groq: ключ НЕ задан"
    for model in GROQ_MODELS:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "ok"}],
                      "max_tokens": 5}, timeout=20)
            if r.status_code == 200:
                return f"✅ Groq: OK ({model})"
            elif r.status_code == 401:
                return "❌ Groq: 401 — ключ неверный"
            elif r.status_code == 403:
                return f"❌ Groq: 403 — доступ запрещён"
            elif r.status_code in (429, 404):
                continue
        except Exception as e:
            return f"❌ Groq: {type(e).__name__}"
    return "❌ Groq: недоступен"


def diag_telegram():
    if not TELEGRAM_TOKEN:
        return "❌ Telegram: токен НЕ задан"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=15)
        if r.status_code != 200:
            return f"❌ Telegram: HTTP {r.status_code}"
        d = r.json()
        if d.get("ok"):
            return f"✅ Telegram: @{d['result'].get('username','?')}"
        return f"❌ Telegram: {d.get('error_code')} — {d.get('description')}"
    except Exception as e:
        return f"❌ Telegram: {type(e).__name__}"


def diag_rss():
    ok = 0; total = len(RSS_SOURCES)
    for url in RSS_SOURCES:
        try:
            f = feedparser.parse(url)
            if len(f.entries) > 0: ok += 1
        except Exception:
            pass
    return f"📡 RSS: {ok}/{total}"


def diag_full():
    return "\n".join([
        f"🔧 <b>Диагностика [{BOT_NAME}]</b>", "",
        diag_vk(),
        diag_groq(),
        diag_telegram(),
        diag_rss(),
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
                            f"/diag — диагностика\n"
                            f"/status — состояние\n"
                            f"/types — счётчики")
                elif cmd == "/diag":
                    tg_send("🔧 Диагностика...")
                    threading.Thread(target=lambda: tg_send(diag_full()), daemon=True).start()
                elif cmd == "/status":
                    with STATE_LOCK: s = dict(STATE)
                    tg_send(f"📊 <b>[{BOT_NAME}]</b>\n\n"
                            f"Тем: {s['topics_count']}\n"
                            f"Последний: {s['last_post_time'] or '—'}\n"
                            f"Тип: {s['last_type'] or '—'}\n"
                            f"Тема: {s['last_post_topic'] or '—'}\n"
                            f"Текст: {s['last_gen'] or '—'}\n"
                            f"Результат: {'✅' if s['last_post_ok'] else '❌' if s['last_post_ok'] is False else '—'}")
                elif cmd == "/types":
                    with STATE_LOCK: c = dict(STATE["type_counters"])
                    tg_send("📈 <b>Счётчики</b>\n\n" + "\n".join(f"  {k}: {v}" for k, v in c.items()))
                elif cmd == "/test":
                    forced = parts[1].lower() if len(parts) > 1 else None
                    if forced and forced not in POST_TYPE_WEIGHTS:
                        tg_send(f"❓ Тип '{forced}' не найден. Доступные: {', '.join(POST_TYPE_WEIGHTS.keys())}"); continue
                    tg_send(f"🧪 Запускаю пост{' (' + forced + ')' if forced else ''}...")
                    threading.Thread(target=post_now, args=(forced,), daemon=True).start()
                else:
                    tg_send(f"❓ Не знаю <code>{cmd}</code>")
        except Exception as e:
            logger.error(f"TG loop: {e}"); time.sleep(5)


# ================== ПЕРЕВОД ==================
def _is_ru(text):
    if not text: return False
    cyr = sum(1 for c in text if 'а' <= c.lower() <= 'я')
    return cyr > len(text) * 0.3


def translate(text, src, dst):
    if not text: return text
    try:
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": text[:500], "langpair": f"{src}|{dst}"},
                         timeout=20)
        if r.status_code == 200:
            t = r.json().get("responseData", {}).get("translatedText", "")
            if t and len(t) > 3: return t
    except Exception:
        pass
    return text


def to_ru(t): return t if _is_ru(t) else translate(t, "en", "ru")


def is_topic_ok(title, summary):
    t = (title or "").lower()
    for bad in TOPIC_BLACKLIST:
        if bad in t: return False
    if t.endswith("?") and len(t) < 30: return False
    return True


# ================== ТЕМЫ ==================
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
TOPICS_MYTH = [
    "Чтобы писать музыку, нужно музыкальное образование",
    "Хороший трек невозможно записать дома",
    "Без лейбла пробиться невозможно",
    "В музыке зарабатывают только звёзды",
    "AI-музыка — это не настоящая музыка",
    "Нужна дорогая техника, чтобы звучать хорошо",
    "Для стримингов нужно пройти сложный отбор",
]
TOPICS_PROMPT = [
    "Напиши текст песни о первой любви",
    "Придумай концепцию альбома в стиле lo-fi",
    "Сгенерируй идею для клипа на трек",
    "Помоги написать припев для рок-баллады",
    "Опиши свой идеальный музыкальный вечер",
    "Предложи название для инди-группы",
]
TOPICS_QUESTION = [
    "Какой трек вы слушаете прямо сейчас?",
    "Какая группа изменила вашу жизнь?",
    "Какой жанр музыки вам ближе всего?",
    "Какую песню вы бы хотели перепеть?",
    "Что для вас важнее — текст или мелодия?",
    "Какой концерт запомнился вам больше всего?",
]
DEFAULT_TOPICS = TOPICS_TIP + TOPICS_CASE + TOPICS_MYTH


# ================== ПРОМПТЫ ==================
BASE_RULES = (
    "Ты — автор постов для русскоязычного музыкального сообщества. "
    "Твои читатели — обычные люди: слушатели, начинающие музыканты. "
    "Пиши ТАК ПРОСТО, чтобы понял любой. Максимум 12-15 слов в предложении. "
    "В конце — простой вопрос. Только русский. Без markdown, без эмодзи в тексте."
)


def gen_news(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Новость»: 5-6 предложений. Что → что это значит → что делать.",
                     f"Новость: {topic}\nКонтекст: {summary[:700] if summary else '(нет)'}\nНапиши пост.")


def gen_tip(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Лайфхак»: проблема → приём → результат → вопрос.",
                     f"Тема: {topic}\nНапиши пост.")


def gen_case(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Реальная история»: от третьего лица, что было → что сделал → результат → вывод.",
                     f"История: {topic}\nНапиши пост.")


def gen_quiz(topic, summary=""):
    system = ("Ты — автор музыкальной викторины.\n\n"
              "ФОРМАТ:\nВопрос: <вопрос>\nA) <вариант>\nB) <вариант>\nC) <вариант>\nD) <вариант>\n"
              "Пиши ответ в комментариях 👇\n\nОТВЕТ: <буква> — <пояснение>\n\nТолько русский.")
    body = groq_chat(system, f"Тема: {topic}\nСоставь викторину.", max_tokens=500, temperature=0.8)
    if not body: return None, None
    m = re.split(r"\n*\s*ОТВЕТ\s*:\s*", body, flags=re.IGNORECASE)
    if len(m) == 2:
        return m[0].strip(), "✅ Ответ: " + m[1].strip()
    return body.strip(), None


def gen_tool(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Инструмент»: что это → примеры → для кого → приём → вопрос.",
                     f"Инструмент: {topic}\nНапиши пост.")


def gen_myth(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Миф vs Факт»: «Миф: ...» → «На самом деле: ...» → пример → вопрос.",
                     f"Миф: {topic}\nНапиши пост.")


def gen_prompt(topic, summary=""):
    system = ("Ты — автор постов про AI для музыкантов.\n\n"
              "Формат «Промпт»:\n1. Зачем промпт.\n2. Пустая строка.\n"
              "3. Промпт в «ёлочках».\n4. Пустая строка.\n5. Что подставить + вопрос.\n\nТолько русский.")
    return groq_chat(system, f"Задача: {topic}\nСоставь промпт-пост.")


def gen_question(topic, summary=""):
    return groq_chat(BASE_RULES + "\n\nФормат «Вопрос подписчикам»: 3-4 предложения + вопрос.",
                     f"Вопрос: {topic}\nНапиши пост.")


def choose_post_type():
    return random.choices(list(POST_TYPE_WEIGHTS.keys()),
                          weights=list(POST_TYPE_WEIGHTS.values()), k=1)[0]


def get_topic_for_type(ptype):
    if ptype == "tip":
        t = random.choice(TOPICS_TIP); return t, "", "TIPS"
    if ptype == "case":
        t = random.choice(TOPICS_CASE); return t, "", "CASES"
    if ptype == "tool":
        t = random.choice(TOPICS_TOOL); return t, "", "TOOLS"
    if ptype == "prompt":
        t = random.choice(TOPICS_PROMPT); return t, "", "PROMPTS"
    if ptype == "myth":
        t = random.choice(TOPICS_MYTH); return t, "", "MYTHS"
    if ptype == "question":
        t = random.choice(TOPICS_QUESTION); return t, "", "QUESTIONS"
    if ptype == "quiz":
        t = random.choice(random.choice([TOPICS_MYTH, TOPICS_TIP]))
        return t, "", "QUIZ"
    return get_news_topic()


def check_rss_sources():
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
    if not candidates:
        candidates = [(t, "", "DEFAULT") for t in DEFAULT_TOPICS]
    with STATE_LOCK:
        STATE["topics_count"] = len(candidates)
    topic_orig, summary, source = random.choice(candidates)
    return to_ru(topic_orig), summary, source


FALLBACK_BY_TYPE = {
    "tip": ["Хочешь записать трек дома, но «звук не тот»?\n\nНачни с одного: обработай вокал компрессором и добавь реверб.\n\nА вы что используете для вокала? 👇"],
    "case": ["Один парень записал трек дома за выходные — и он завирусился в TikTok.\n\nСделал сэмпл из старой песни, добавил бит.\n\nА вы пробовали делать сэмплы? 👇"],
    "tool": ["Suno — нейросеть, которая создаёт треки за 30 секунд по описанию.\n\nПопроси: «лёгкий lo-fi» или «энергичный рок».\n\nА вы уже пробовали? 👇"],
    "prompt": ["Промпт, чтобы сочинить текст песни:\n\n«Напиши текст песни о первой любви в стиле Цоя. Куплет, припев, повтор».\n\nКакую песню хотели бы написать? 👇"],
    "myth": ["Миф: чтобы писать музыку, нужно музыкальное образование.\n\nНа самом деле: миллионы треков записаны людьми без диплома.\n\nА вы как думали? 👇"],
    "question": ["Интересно ваше мнение: какой трек вы слушаете последние дни?\n\nПишите в комментариях 👇"],
    "quiz": ["Вопрос: сколько струн у классической гитары?\nA) 4\nB) 6\nC) 7\nD) 12\nПиши ответ в комментариях 👇"],
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


TYPE_EMOJI = {
    "news": ["🎵", "🎶", "📰", "🎤"],
    "tip": ["💡", "🎼", "🎧", "🎹"],
    "case": ["🎤", "🌟", "📖", "🎬"],
    "quiz": ["🎯", "❓", "🧩", "🏆"],
    "tool": ["🧰", "🛠", "🎚", "🎛"],
    "prompt": ["✍️", "🎨", "📝", "🎼"],
    "myth": ["🔍", "❌", "✅", "🎵"],
    "question": ["❓", "💬", "🎤", "👇"],
}
TYPE_HASHTAGS = {
    "news": "#музыка #новости #навигатор",
    "tip": "#музыка #лайфхак #советы",
    "case": "#музыка #история #успех",
    "quiz": "#музыка #викторина",
    "tool": "#музыка #инструменты #сервисы",
    "prompt": "#музыка #промпты #AI",
    "myth": "#музыка #мифы #факты",
    "question": "#музыка #обсуждение",
}


def format_post(topic, body, ptype):
    e = random.choice(TYPE_EMOJI.get(ptype, ["🎵"]))
    tags = TYPE_HASHTAGS.get(ptype, "#музыка #навигатор")
    return f"{e} {topic}\n\n{body}\n\n{tags}"


def post_now(forced_type=None):
    ptype = forced_type or choose_post_type()
    logger.info(f"⏰ [{BOT_NAME}] Запуск постинга (тип: {ptype})")

    topic_ru, summary, source = get_topic_for_type(ptype)
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

    ok, post_id = publish(text)

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
    logger.info(f"🔑 Groq: {'есть' if GROQ_API_KEY else 'НЕТ'}")
    logger.info(f"🎲 Типы постов: {POST_TYPE_WEIGHTS}")

    # Проверка VK-токена
    vk_token_info()

    tg_init()
    if TELEGRAM_TOKEN:
        threading.Thread(target=tg_command_loop, daemon=True).start()
        if TELEGRAM_CHAT_ID:
            tg_send(f"🟢 <b>[{BOT_NAME}]</b> запущен\n"
                    f"Groq: {'✅' if GROQ_API_KEY else '❌'}\n"
                    f"Расписание: {', '.join(POST_TIMES)}\n"
                    f"/test · /diag · /status · /types")

    check_rss_sources()

    def scheduler_loop():
        last_run = {"morning": None, "evening": None}
        while True:
            try:
                now = datetime.now()
                for idx, t in enumerate(POST_TIMES):
                    try:
                        hh, mm = map(int, t.split(":"))
                    except Exception:
                        continue
                    key = "morning" if idx == 0 else "evening"
                    if (now.hour == hh and now.minute == mm
                            and last_run[key] != now.date()):
                        last_run[key] = now.date()
                        logger.info(f"⏰ Запуск по расписанию: {t}")
                        threading.Thread(target=post_now, daemon=True).start()
            except Exception as e:
                logger.error(f"scheduler: {e}")
            time.sleep(30)

    threading.Thread(target=scheduler_loop, daemon=True).start()
    logger.info(f"  → задачи: {', '.join(POST_TIMES)}")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error("❌ Нет VK_TOKEN_MUSIC / VK_TOKEN_AI")
        sys.exit(1)
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Остановлено")