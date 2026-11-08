#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Музыкальный Навигатор. Группа 240656847. Посты в 06:00 и 15:00.
LLM: Cloud.ru (GigaChat 3.5) → Groq → Pollinations text → умный fallback.
Без schedule — планировщик на threading.
"""

import os, sys, time, json, random, logging, requests, re, threading
import uuid
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

# --- Cloud.ru Foundation Models ---
CLOUDRU_API_KEY   = _env("CLOUDRU_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY")
CLOUDRU_BASE_URL  = _env("CLOUDRU_BASE_URL", default="https://foundation-models.api.cloud.ru/v1")
CLOUDRU_MODEL     = _env("CLOUDRU_MODEL", default="ai-sage/GigaChat3.5-432B-A28B-Reasoning")

# --- Классический GigaChat (Сбер) ---
GIGACHAT_AUTH_KEY = _env("GIGACHAT_AUTH_KEY", "GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE    = _env("GIGACHAT_SCOPE", default="GIGACHAT_API_PERS")
GIGACHAT_MODEL    = _env("GIGACHAT_MODEL", default="GigaChat-2-Max")
GIGACHAT_BASE_URL = _env("GIGACHAT_BASE_URL", default="https://api.giga.chat/v1")
GIGACHAT_VERIFY_SSL = _env("GIGACHAT_VERIFY_SSL", default="true").lower() == "true"

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


# ================== LLM ==================
class GigaChatAuth:
    def __init__(self):
        self._token = None
        self._expires_at = 0
        self._lock = threading.Lock()

    def get_token(self):
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            if not GIGACHAT_AUTH_KEY:
                return None
            try:
                url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
                payload = f"scope={GIGACHAT_SCOPE}"
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": f"Bearer {GIGACHAT_AUTH_KEY}",
                }
                r = requests.post(url, data=payload, headers=headers, timeout=30,
                                  verify=GIGACHAT_VERIFY_SSL)
                if r.status_code == 200:
                    data = r.json()
                    self._token = data["access_token"]
                    self._expires_at = time.time() + int(data.get("expires_in", 1800))
                    logger.info("🔑 GigaChat: токен доступа получен")
                    return self._token
            except Exception as e:
                logger.error(f"GigaChat OAuth: {type(e).__name__}: {e}")
            return None

_giga_auth = GigaChatAuth()


def cloudru_chat(system, user, max_tokens=1200, temperature=0.75):
    if not CLOUDRU_API_KEY: return None
    try:
        url = f"{CLOUDRU_BASE_URL.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {CLOUDRU_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": CLOUDRU_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
        }
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code == 200:
            body = r.json()["choices"][0]["message"]["content"].strip()
            if body and len(body) > 80:
                logger.info(f"✅ Cloud.ru ({CLOUDRU_MODEL}) — {len(body)} симв.")
                return body
        elif r.status_code in (401, 403, 404, 429):
            logger.warning(f"Cloud.ru {r.status_code}: {r.text[:200]}")
        else:
            logger.warning(f"Cloud.ru HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"Cloud.ru: {type(e).__name__}: {e}")
    return None


def gigachat_chat(system, user, max_tokens=1200, temperature=0.75):
    token = _giga_auth.get_token()
    if not token: return None
    try:
        url = f"{GIGACHAT_BASE_URL.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "model": GIGACHAT_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        r = requests.post(url, headers=headers, json=payload, timeout=60, verify=GIGACHAT_VERIFY_SSL)
        if r.status_code == 200:
            body = r.json()["choices"][0]["message"]["content"].strip()
            if body and len(body) > 80:
                logger.info(f"✅ GigaChat ({GIGACHAT_MODEL}) — {len(body)} симв.")
                return body
        elif r.status_code == 401:
            _giga_auth._token = None
            logger.warning("GigaChat 401 — токен истёк")
        else:
            logger.warning(f"GigaChat HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.warning(f"GigaChat: {type(e).__name__}: {e}")
    return None


GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

def groq_chat(system, user, max_tokens=1200, temperature=0.75):
    if not GROQ_API_KEY: return None
    for model in GROQ_MODELS:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "max_tokens": max_tokens}, timeout=60)
            if r.status_code == 200:
                body = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if body and len(body) > 80:
                    logger.info(f"✅ Groq ({model}) — {len(body)} симв.")
                    return body
            elif r.status_code in (403, 429, 404):
                continue
        except Exception:
            continue
    logger.warning("❌ Groq недоступен")
    return None


def generate_llm(system, user, max_tokens=1200, temperature=0.75):
    if CLOUDRU_API_KEY:
        body = cloudru_chat(system, user, max_tokens, temperature)
        if body: return body, "cloud.ru"
    if GIGACHAT_AUTH_KEY:
        body = gigachat_chat(system, user, max_tokens, temperature)
        if body: return body, "gigachat"
    if GROQ_API_KEY:
        body = groq_chat(system, user, max_tokens, temperature)
        if body: return body, "groq"
    return None, None


def pollinations_text(prompt, max_chars=2000):
    try:
        url = f"https://text.pollinations.ai/{requests.utils.quote(prompt[:1500])}"
        r = requests.get(url, timeout=60)
        if r.status_code == 200 and r.text:
            t = r.text.strip()
            bad = ["budget", "API key", "raise the key", "Pollinations account"]
            if any(b.lower() in t.lower() for b in bad): return None
            cyr = sum(1 for c in t if 'а' <= c.lower() <= 'я')
            if len(t) > 100 and cyr > 30:
                logger.info(f"✅ Pollinations text ({len(t)} симв.)")
                return t[:max_chars]
    except Exception:
        pass
    return None


# ================== ДИАГНОСТИКА ==================
def diag_check_llm():
    if not CLOUDRU_API_KEY and not GIGACHAT_AUTH_KEY and not GROQ_API_KEY:
        return "❌ LLM: нет ключей"
    if CLOUDRU_API_KEY:
        try:
            r = requests.post(f"{CLOUDRU_BASE_URL.rstrip('/')}/chat/completions",
                              headers={"Authorization": f"Bearer {CLOUDRU_API_KEY}", "Content-Type": "application/json"},
                              json={"model": CLOUDRU_MODEL, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 20},
                              timeout=20)
            if r.status_code == 200: return f"✅ Cloud.ru ({CLOUDRU_MODEL}): OK"
            return f"❌ Cloud.ru: HTTP {r.status_code}\n<code>{r.text[:200]}</code>"
        except Exception as e:
            return f"❌ Cloud.ru: {type(e).__name__}: {str(e)[:150]}"
    if GIGACHAT_AUTH_KEY:
        return "ℹ️ GigaChat (Сбер) настроен (проверка недоступна без токена)"
    return f"ℹ️ Groq ({GROQ_MODELS[0]})"


def diag_vk_group():
    if not VK_TOKEN: return "❌ VK group: токен НЕ задан"
    try:
        r = requests.get("https://api.vk.com/method/groups.getById", params={"access_token": VK_TOKEN, "v": VK_API_VER, "group_id": abs(GROUP_ID)}, timeout=15).json()
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
            if n > 0: ok += 1; lines.append(f"  ✅ {url.split('/')[2]} — {n}")
            else: lines.append(f"  ⚠️ {url.split('/')[2]} — 0")
        except Exception as e:
            lines.append(f"  ❌ {url.split('/')[2]} — {str(e)[:60]}")
    return f"📡 RSS: {ok}/{total}\n" + "\n".join(lines)


def diag_full():
    return "\n".join([
        f"🔧 <b>Диагностика [{BOT_NAME}]</b>", "",
        "<b>Переменные:</b>",
        f"  CLOUDRU_API_KEY: {'✅' if CLOUDRU_API_KEY else '❌'}",
        f"  GROQ_API_KEY: {'✅' if GROQ_API_KEY else '❌'}",
        f"  VK_TOKEN_MUSIC: {'✅' if VK_TOKEN else '❌'}", "",
        "<b>Сервисы:</b>",
        diag_check_llm(),
        diag_vk_group(), "",
        diag_check_rss(),
    ])


# ================== TELEGRAM ==================
def tg_api(method, **params):
    if not TELEGRAM_TOKEN: return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        r = requests.post(url, json=params, timeout=20)
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None


def tg_init():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN: return None
    me = tg_api("getMe")
    if not me or not me.get("ok"): return None
    tg_api("deleteWebhook")
    if TELEGRAM_CHAT_ID: return TELEGRAM_CHAT_ID
    upd = tg_api("getUpdates", timeout=1)
    if upd and upd.get("ok") and upd.get("result"):
        for u in reversed(upd["result"]):
            msg = u.get("message") or u.get("edited_message") or {}
            cid = msg.get("chat", {}).get("id")
            if cid:
                TELEGRAM_CHAT_ID = str(cid)
                return TELEGRAM_CHAT_ID
    return None


def tg_send(text, chat_id=None):
    if not TELEGRAM_TOKEN: return
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": cid, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=15)
    except Exception:
        pass


def tg_command_loop():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN: return
    logger.info("📨 Слушаю Telegram: /test, /status, /diag, /llm, /types, /help")
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
            if not upd or not upd.get("ok"): time.sleep(5); continue
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text: continue
                if not TELEGRAM_CHAT_ID and chat_id:
                    TELEGRAM_CHAT_ID = chat_id; tg_send("✅ chat_id сохранён.", chat_id=chat_id); continue
                if chat_id != str(TELEGRAM_CHAT_ID):
                    tg_send("⛔ Нет доступа.", chat_id=chat_id); continue
                parts = text.split(); cmd = parts[0].lower().split("@")[0]
                logger.info(f"📨 Команда: {cmd}")
                if cmd in ("/start", "/help"):
                    tg_send(f"🤖 <b>{BOT_NAME}</b>\n\n/test — пост\n/test news|tip|case|tool|myth|prompt|question|quiz\n/diag — диагностика\n/llm — проверка LLM\n/status — состояние\n/types — счётчики")
                elif cmd == "/diag":
                    tg_send("🔧 Диагностика..."); threading.Thread(target=lambda: tg_send(diag_full()), daemon=True).start()
                elif cmd == "/llm":
                    tg_send("🔎 Проверяю..."); threading.Thread(target=lambda: tg_send(f"<b>LLM:</b>\n{diag_check_llm()}"), daemon=True).start()
                elif cmd == "/status":
                    with STATE_LOCK: s = dict(STATE)
                    counters = ", ".join(f"{k}:{v}" for k, v in s["type_counters"].items())
                    tg_send(f"📊 <b>[{BOT_NAME}]</b>\n\nТем: {s['topics_count']}\nПоследний: {s['last_post_time'] or '—'}\nТип: {s['last_type'] or '—'}\nТема: {s['last_post_topic'] or '—'}\nТекст: {s['last_gen'] or '—'}\nРезультат: {'✅' if s['last_post_ok'] else '❌' if s['last_post_ok'] is False else '—'}\n\nСчётчики: {counters}")
                elif cmd == "/types":
                    with STATE_LOCK: c = dict(STATE["type_counters"])
                    tg_send("📈 <b>Счётчики</b>\n\n" + "\n".join(f"  {k}: {v}" for k, v in c.items()))
                elif cmd == "/test":
                    forced = parts[1].lower() if len(parts) > 1 else None
                    if forced and forced not in POST_TYPE_WEIGHTS:
                        tg_send(f"❓ Неизвестный тип."); continue
                    tg_send(f"🧪 Запускаю пост...")
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

def translate(text, source_lang, target_lang):
    if not text: return text
    try:
        r = requests.get("https://api.mymemory.translated.net/get", params={"q": text[:500], "langpair": f"{source_lang}|{target_lang}"}, timeout=20)
        if r.status_code == 200:
            t = r.json().get("responseData", {}).get("translatedText", "")
            if t and len(t) > 3: return t
    except Exception: pass
    return text

def to_ru(t): return t if _is_ru(t) else translate(t, "en", "ru")
def to_en(t): return t if not _is_ru(t) else translate(t, "ru", "en")

def is_topic_ok(title, summary):
    t = (title or "").lower(); s = (summary or "").lower()[:300]
    for bad in TOPIC_BLACKLIST:
        if bad in t: return False
    if sum(1 for m in ["скидка", "промокод", "записаться", "регистрация", "бесплатно", "вебинар"] if m in s) >= 3: return False
    if t.endswith("?") and len(t) < 30: return False
    return True


# ================== ТЕМЫ ==================
DEFAULT_TOPICS = [
    "Как начать писать музыку дома без студии", "Как монетизировать свои треки на стримингах",
    "Что такое сведение и мастеринг простыми словами", "Как выбрать первую гитару или синтезатор",
    "Как продвигать свои треки в соцсетях", "Как сделать качественную обложку для трека",
    "Как попасть в плейлисты на Spotify и VK Музыке", "Сколько стоит записать трек в студии",
    "Как побороть страх сцены начинающему музыканту", "Как использовать AI для создания музыки",
    "Как работать с авторскими правами на музыку", "Топ-5 бесплатных программ для создания музыки",
]
TOPICS_TIP = [
    "Как записать вокал дома без шумоизоляции", "Как настроить микрофон для домашней студии",
    "Как уложиться в бюджет при записи первого трека", "Как свести трек, чтобы он звучал громко и чисто",
    "Как придумать цепляющий припев", "Как выбрать DAW для начинающего", "Как продвигать треки без бюджета",
    "Как правильно оформить релиз на стримингах", "Как сочинять музыку, когда нет вдохновения",
    "Как записать песню с помощью одного микрофона",
]
TOPICS_CASE = [
    "Как парень записал хит у себя дома за выходные", "История группы, которая собрала миллион без лейбла",
    "Как блогер выпустил трек с AI-вокалом и попал в чарты", "Как школьник заработал на битмейкинге больше родителей",
    "Как девушка начала петь в 35 и получила контракт", "История трека, который стал вирусным в TikTok",
    "Как музыкант из деревни попал на большую сцену", "Как диджей собрал первый миллион на стримингах",
]
TOPICS_TOOL = [
    "Suno — как AI создаёт треки за минуту", "Udio — конкурент Suno, как пользоваться",
    "BandLab — бесплатная студия в браузере", "Audacity — бесплатный редактор звука",
    "Splice — библиотека сэмплов для треков", "LANDR — автоматический мастеринг",
    "DistroKid — как выложить трек на все площадки", "Spotify for Artists — аналитика для музыканта",
    "Moises — как убрать вокал из трека", "Kits.AI — как сделать AI-вокал",
]
TOPICS_PROMPT = [
    "Напиши текст песни о первой любви", "Придумай концепцию альбома в стиле lo-fi",
    "Сгенерируй идею для клипа на трек", "Помоги написать припев для рок-баллады",
    "Опиши свой идеальный музыкальный вечер", "Предложи название для инди-группы",
]
TOPICS_MYTH = [
    "Чтобы писать музыку, нужно музыкальное образование", "Хороший трек невозможно записать дома",
    "Без лейбла пробиться невозможно", "В музыке зарабатывают только звёзды",
    "AI-музыка — это не настоящая музыка", "Нужна дорогая техника, чтобы звучать хорошо",
    "Для стримингов нужно пройти сложный отбор",
]
TOPICS_QUESTION = [
    "Какой трек вы слушаете прямо сейчас?", "Какая группа изменила вашу жизнь?",
    "Какой жанр музыки вам ближе всего?", "Какую песню вы бы хотели перепеть?",
    "Что для вас важнее — текст или мелодия?", "Какой концерт запомнился вам больше всего?",
]


# ================== ПРОМПТЫ ==================
BASE_RULES = (
    "Ты — опытный автор постов для русскоязычного музыкального сообщества. "
    "Твои читатели — обычные люди: слушатели, начинающие музыканты, продюсеры.\n\n"
    "ФОРМАТ ПОСТА:\n"
    "1. ХУК (1-2 предложения).\n"
    "2. СУТЬ (2-3 предложения).\n"
    "3. ПРИМЕРЫ (2-3 предложения).\n"
    "4. СОВЕТ (1-2 предложения).\n"
    "5. ВЫВОД (1 предложение).\n"
    "6. ВОПРОС (1 предложение).\n\n"
    "ОБЪЁМ: 8-12 предложений. Длина поста — от 800 до 1500 символов.\n\n"
    "ЯЗЫК: Простой. Максимум 15-18 слов в предложении. Без сложных терминов. "
    "Только русский. Без markdown. Без эмодзи в тексте. Без хештегов.\n\n"
    "ЗАПРЕЩЕНО: Начинать с 'В этом материале'. Использовать клише. "
    "Давать общие советы без конкретики. Писать короче 700 символов."
)

def gen_news(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: НОВОСТЬ. Перескажи новость так, будто объясняешь её другу. Не копируй текст источника."
    return generate_llm(system, f"Новость: {topic}\nКонтекст: {summary[:800] if summary else '(нет)'}\n\nНапиши пост по формату.")

def gen_tip(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: ЛАЙФХАК. Начни с проблемы ('Знакомо, когда…'). Дай ОДИН конкретный приём с пошаговым объяснением."
    return generate_llm(system, f"Тема лайфхака: {topic}\n\nНапиши пост по формату.")

def gen_case(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: РЕАЛЬНАЯ ИСТОРИЯ. Расскажи от третьего лица. Что было нужно → что сделал → какой результат."
    return generate_llm(system, f"История: {topic}\n\nНапиши пост по формату.")

def gen_quiz(topic, summary=""):
    system = (
        "Ты — автор музыкальной викторины.\n\n"
        "ФОРМАТ:\nВопрос: <вопрос>\nA) <вариант>\nB) <вариант>\nC) <вариант>\nD) <вариант>\n"
        "Пиши ответ в комментариях 👇\n\nЗатем с новой строки строго:\nОТВЕТ: <буква> — <пояснение>\n\nТолько русский."
    )
    body, name = generate_llm(system, f"Тема: {topic}\nСоставь викторину.", max_tokens=600, temperature=0.8)
    if not body: return None, None
    m = re.split(r"\n*\s*ОТВЕТ\s*:\s*", body, flags=re.IGNORECASE)
    if len(m) == 2: return m[0].strip(), "✅ Ответ: " + m[1].strip()
    return body.strip(), None

def gen_tool(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: ИНСТРУМЕНТ. Что это, что умеет, для кого полезен. 2-3 конкретных примера. Один неочевидный приём."
    return generate_llm(system, f"Инструмент: {topic}\n\nНапиши пост по формату.")

def gen_prompt(topic, summary=""):
    system = (
        "Ты — автор постов про промпты для AI-музыки.\n\n"
        "ФОРМАТ:\n1. Зачем промпт.\n2. Пустая строка.\n3. Промпт в «ёлочках» (3-5 предложений, с [скобками]).\n"
        "4. Пустая строка.\n5. Что подставить и какой результат ждать.\n6. Вопрос читателю.\n\n"
        "ОБЪЁМ: 500-900 символов. Только русский."
    )
    return generate_llm(system, f"Задача: {topic}\n\nСоставь пост-промпт по формату.")

def gen_myth(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: МИФ vs ФАКТ. Начни 'Миф: ...'. Далее 'На самом деле: ...' — с конкретным примером."
    return generate_llm(system, f"Миф для разбора: {topic}\n\nНапиши пост по формату.")

def gen_question(topic, summary=""):
    system = BASE_RULES + "\n\nТИП: ВОПРОС ПОДПИСЧИКАМ. Короткий контекст + открытый вопрос."
    return generate_llm(system, f"Тема вопроса: {topic}\n\nНапиши пост по формату.")


# ================== РОТАЦИЯ ==================
def choose_post_type():
    return random.choices(list(POST_TYPE_WEIGHTS.keys()), weights=list(POST_TYPE_WEIGHTS.values()), k=1)[0]

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
    if not RSS_ENABLED or not RSS_SOURCES: return 0
    logger.info(f"📡 Проверяю RSS ({len(RSS_SOURCES)} шт.)...")
    total = 0
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url); n = len(feed.entries); total += n
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
                    if title and 15 < len(title) < 220: raw.append((title, summary, url))
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
    with STATE_LOCK: STATE["topics_count"] = len(candidates)
    topic_orig, summary, source = random.choice(candidates)
    return to_ru(topic_orig), summary, source, (topic_orig if not _is_ru(topic_orig) else to_en(topic_orig))


# ================== УМНЫЙ FALLBACK ==================
def smart_fallback(topic, summary="", ptype="news"):
    summary_ru = to_ru(summary[:500]) if summary else ""
    facts = ""
    if summary_ru:
        sents = re.split(r"(?<=[.!?])\s+", summary_ru)
        useful = [s.strip() for s in sents if 40 < len(s.strip()) < 300][:3]
        if useful: facts = " ".join(useful)[:600]

    if ptype == "news":
        if facts: return f"{topic}\n\nЧто случилось: {facts}\n\nПочему это важно: музыкальная индустрия меняется быстро. Следить за трендами полезно, чтобы не упустить новые возможности.\n\nЧто можно сделать: попробовать новый сервис на практике.\n\nКак вы относитесь к таким изменениям? 👇"
        return f"{topic}\n\nТема, которая сейчас обсуждается. Если коротко: изменения касаются того, как создаётся музыка.\n\nЧто можно сделать: попробовать один из популярных сервисов.\n\nА вы уже пробовали? 👇"
    if ptype == "tip": return f"{topic}\n\nЗнакомо, когда не знаешь, с чего начать?\n\nОткрой BandLab и запиши 30-секундный набросок идеи. Не думай о качестве.\n\nА что бы вы хотели попробовать? 👇"
    if ptype == "case": return f"{topic}\n\nТакие истории не редкость. Люди пишут треки дома и собирают тысячи прослушиваний.\n\nПервый шаг — попробовать записать одну идею на телефон.\n\nА у вас был такой опыт? 👇"
    if ptype == "tool": return f"{topic}\n\nЭтот инструмент стоит попробовать. Используйте его для генерации идей, а не для финала.\n\nПробовали? Поделитесь 👇"
    if ptype == "prompt": return f"Промпт для «{topic.lower()}».\n\n«Представь, что ты продюсер. Помоги с темой: {topic}. Дай 3 шага и объясни ошибки».\n\nА какую тему вы хотели разобрать? 👇"
    if ptype == "myth": return f"{topic}\n\nМиф: {topic.lower()}.\n\nНа самом деле: музыка — это навык, который можно развить.\n\nА как думали вы раньше? 👇"
    if ptype == "question": return f"{topic}\n\nЭтот вопрос обсуждают многие.\n\nЧто вы думаете? Поделитесь 👇"
    if ptype == "quiz": return "Викторина.\n\nВопрос: сколько струн у гитары?\nA) 4\nB) 6\nC) 7\nD) 12\n\nПиши ответ 👇"
    return f"{topic}\n\nТема, которая стоит внимания.\n\nЧто можно сделать: попробовать новый инструмент.\n\nА что вы хотели бы узнать? 👇"

def smart_fallback_answer(ptype): return "✅ Ответ: B) 6 струн."


# ================== VK ==================
def vk_api_call(method, **params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VER})
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
        if "error" in r: return None, r["error"]
        return r.get("response"), None
    except Exception as e:
        return None, {"error_code": -1, "error_msg": str(e)}

def publish(text):
    resp, err = vk_api_call("wall.post", owner_id=GROUP_ID, from_group=1, message=text)
    if err:
        logger.error(f"VK wall.post: {err.get('error_code')} — {err.get('error_msg')}")
        return False, None
    if resp and "post_id" in resp:
        logger.info(f"✅ Пост опубликован (post_id={resp['post_id']})")
        return True, resp["post_id"]
    return False, None

def post_comment(post_id, text):
    resp, err = vk_api_call("wall.createComment", owner_id=GROUP_ID, post_id=post_id, from_group=1, message=text)
    if not err: logger.info(f"💬 Комментарий к посту {post_id}")

# ================== ФОРМАТ ==================
TYPE_EMOJI = {"news": ["🎵", "🎶", "📰"], "tip": ["💡", "🎼", "🎧"], "case": ["🎤", "🌟", "📖"], "quiz": ["🎯", "❓", "🧩"], "tool": ["🧰", "🛠", "🎚"], "prompt": ["✍️", "📝", "🎨"], "myth": ["🔍", "❌", "✅"], "question": ["❓", "💬", "🎤"]}
TYPE_HASHTAGS = {"news": "#музыка #новости", "tip": "#музыка #лайфхак", "case": "#музыка #история", "quiz": "#музыка #викторина", "tool": "#музыка #инструменты", "prompt": "#музыка #промпты", "myth": "#музыка #мифы", "question": "#музыка #обсуждение"}

def format_post(topic, body, ptype):
    e = random.choice(TYPE_EMOJI.get(ptype, ["🎵"]))
    tags = TYPE_HASHTAGS.get(ptype, "#музыка")
    return f"{e} {topic}\n\n{body}\n\n{tags}"


# ================== ЦИКЛ ==================
def post_now(forced_type=None):
    ptype = forced_type or choose_post_type()
    logger.info(f"⏰ [{BOT_NAME}] Запуск постинга (тип: {ptype})")

    topic_ru, summary, source, topic_en = get_topic_for_type(ptype)
    logger.info(f"📝 Тема RU: {topic_ru}")
    logger.info(f"📎 Источник: {source}")

    quiz_answer = None
    gen_name = None

    if ptype == "news":
        res = gen_news(topic_ru, summary)
        if not res[0]:
            logger.info("↪️ News упал, пробую tip")
            ptype = "tip"; res = gen_tip(topic_ru)
    elif ptype == "tip": res = gen_tip(topic_ru)
    elif ptype == "case": res = gen_case(topic_ru)
    elif ptype == "quiz":
        body_q, quiz_answer = gen_quiz(topic_ru)
        res = (body_q, "llm" if body_q else None)
    elif ptype == "tool": res = gen_tool(topic_ru)
    elif ptype == "prompt": res = gen_prompt(topic_ru)
    elif ptype == "myth": res = gen_myth(topic_ru)
    elif ptype == "question": res = gen_question(topic_ru)
    else: res = gen_news(topic_ru, summary)

    body, name = res if isinstance(res, tuple) else (res, None)
    if body: gen_name = name or "llm"

    if not body:
        logger.info("↪️ LLM упал, пробую Pollinations text")
        body = pollinations_text(f"Напиши пост на русском на тему: {topic_ru}. Структура: хук, суть, 2 примера, совет, вывод, вопрос. 800-1200 символов.")
        if body:
            gen_name = "pollinations"
            if ptype == "quiz": quiz_answer = smart_fallback_answer(ptype)

    if not body:
        logger.warning(f"⚠️ LLM недоступен — умный fallback ({ptype})")
        gen_name = "fallback"
        body = smart_fallback(topic_ru, summary, ptype)
        if ptype == "quiz": quiz_answer = smart_fallback_answer(ptype)

    text = format_post(topic_ru, body, ptype)
    logger.info(f"📄 Текст ({len(text)} симв.) от {gen_name}: {text[:200]}...")

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
        tg_send(f"🚀 <b>[{BOT_NAME}]</b> Пост ({ptype})\n<b>Тема:</b> {topic_ru}\n<b>Текст:</b> {gen_name} ({len(text)} симв.)\n\n{text[:800]}")
    else:
        tg_send(f"❌ <b>[{BOT_NAME}]</b> Не опубликовано ({ptype})")

    logger.info(f"🏁 [{BOT_NAME}] Цикл завершён ({ptype})\n")


# ================== ПЛАНИРОВЩИК (без schedule) ==================
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
                if (now.hour == hh and now.minute == mm and last_run[key] != now.date()):
                    last_run[key] = now.date()
                    logger.info(f"⏰ Запуск по расписанию: {t}")
                    threading.Thread(target=post_now, daemon=True).start()
        except Exception as e:
            logger.error(f"scheduler: {e}")
        time.sleep(30)


def main():
    logger.info(f"🚀 {BOT_NAME} запущен")
    logger.info(f"📡 Группа: {GROUP_ID}")
    logger.info(f"⏰ Расписание: {', '.join(POST_TIMES)}")
    logger.info(f"🔑 VK group: {'есть' if VK_TOKEN else 'НЕТ'}")
    if CLOUDRU_API_KEY: logger.info(f"🤖 LLM: Cloud.ru ({CLOUDRU_MODEL})")
    elif GIGACHAT_AUTH_KEY: logger.info(f"🤖 LLM: GigaChat")
    elif GROQ_API_KEY: logger.info(f"🤖 LLM: Groq")
    else: logger.info("🤖 LLM: НЕТ")

    tg_init()
    if TELEGRAM_TOKEN:
        threading.Thread(target=tg_command_loop, daemon=True).start()
        if TELEGRAM_CHAT_ID:
            tg_send(f"🟢 <b>[{BOT_NAME}]</b> запущен\nLLM: Cloud.ru ({CLOUDRU_MODEL})\n/test · /diag · /llm · /status")

    check_rss_sources()

    # Планировщик на threading
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logger.info(f"  → задачи: {', '.join(POST_TIMES)}")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    if not VK_TOKEN:
        logger.error("❌ Нет VK_TOKEN_MUSIC"); sys.exit(1)
    if not CLOUDRU_API_KEY and not GIGACHAT_AUTH_KEY and not GROQ_API_KEY:
        logger.error("❌ Нет ключей ни для одного LLM"); sys.exit(1)
    try: main()
    except KeyboardInterrupt: logger.info("Остановлено")