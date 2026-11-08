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

CLOUDRU_API_KEY   = _env("CLOUDRU_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY")
CLOUDRU_BASE_URL  = _env("CLOUDRU_BASE_URL", default="https://foundation-models.api.cloud.ru/v1")
CLOUDRU_MODEL     = _env("CLOUDRU_MODEL", default="ai-sage/GigaChat3.5-432B-A28B-Reasoning")

GIGACHAT_AUTH_KEY = _env("GIGACHAT_AUTH_KEY", "GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE    = _env("GIGACHAT_SCOPE", default="GIGACHAT_API_PERS")
GIGACHAT_MODEL    = _env("GIGACHAT_MODEL", default="GigaChat-2-Max")
GIGACHAT_BASE_URL = _env("GIGACHAT_BASE_URL", default="https://api.giga.chat/v1")
GIGACHAT_VERIFY_SSL = _env("GIGACHAT_VERIFY_SSL", default="true").lower() == "true"

TELEGRAM_TOKEN   = _env("MUSIC_TELEGRAM_TOKEN", "MUSIC_TG_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN")
TELEGRAM_CHAT_ID = _env("MUSIC_CHAT_ID", "MUSIC_TG_CHAT_ID", "TELEGRAM_CHAT_ID", "AI_CHAT_ID")

POST_TYPE_WEIGHTS = {
    "tip": 25, "case": 20, "news": 20, "tool": 12,
    "myth": 10, "prompt": 6, "question": 4, "quiz": 3,
}
QUIZ_ANSWER_IN_COMMENT = True
QUIZ_ANSWER_DELAY_SEC = 5

RSS_ENABLED = True
RSS_SOURCES = [
    "https://pitchfork.com/rss/news/",
    "https://www.nme.com/news/music/feed",
    "https://www.rollingstone.com/music/music-news/feed/",
    "https://www.billboard.com/feed/",
    "https://pitchfork.com/rss/reviews/albums/",
]
DATA_DIR = "./data"
TOPIC_BLACKLIST = ["бесплатных уроков", "вебинар", "вебинары", "дайджест", "подборка", "топ-", "top-", "курс", "курсы", "обучение", "интенсив", "скидк", "акци", "распродаж", "промокод", "митап", "конференц", "хакатон", "вакансия", "резюме", "ищу работу", "запись трансляции"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MUSIC] - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger("MUSIC")
os.makedirs(DATA_DIR, exist_ok=True)

STATE = {"last_post_topic": None, "last_post_time": None, "last_post_ok": None, "topics_count": 0, "last_source": None, "last_gen": None, "last_type": None, "type_counters": {k: 0 for k in POST_TYPE_WEIGHTS}}
STATE_LOCK = threading.Lock()

# ================== LLM ==================
class GigaChatAuth:
    def __init__(self):
        self._token = None; self._expires_at = 0; self._lock = threading.Lock()
    def get_token(self):
        with self._lock:
            if self._token and time.time() < self._expires_at - 60: return self._token
            if not GIGACHAT_AUTH_KEY: return None
            try:
                url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
                payload = f"scope={GIGACHAT_SCOPE}"
                headers = {"Content-Type": "application/x-www-form-urlencoded", "RqUID": str(uuid.uuid4()), "Authorization": f"Bearer {GIGACHAT_AUTH_KEY}"}
                r = requests.post(url, data=payload, headers=headers, timeout=30, verify=GIGACHAT_VERIFY_SSL)
                if r.status_code == 200:
                    data = r.json(); self._token = data["access_token"]; self._expires_at = time.time() + int(data.get("expires_in", 1800))
                    logger.info("🔑 GigaChat: токен получен"); return self._token
            except Exception as e: logger.error(f"GigaChat OAuth: {e}")
            return None
_giga_auth = GigaChatAuth()

def cloudru_chat(system, user, max_tokens=1200, temperature=0.75):
    if not CLOUDRU_API_KEY: return None
    try:
        url = f"{CLOUDRU_BASE_URL.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {CLOUDRU_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": CLOUDRU_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.95}
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code == 200:
            body = r.json()["choices"][0]["message"]["content"].strip()
            if body and len(body) > 80:
                logger.info(f"✅ Cloud.ru ({CLOUDRU_MODEL}) — {len(body)} симв."); return body
        else: logger.warning(f"Cloud.ru HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e: logger.warning(f"Cloud.ru: {e}")
    return None

def gigachat_chat(system, user, max_tokens=1200, temperature=0.75):
    token = _giga_auth.get_token()
    if not token: return None
    try:
        url = f"{GIGACHAT_BASE_URL.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {"model": GIGACHAT_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": temperature}
        r = requests.post(url, headers=headers, json=payload, timeout=60, verify=GIGACHAT_VERIFY_SSL)
        if r.status_code == 200:
            body = r.json()["choices"][0]["message"]["content"].strip()
            if body and len(body) > 80:
                logger.info(f"✅ GigaChat ({GIGACHAT_MODEL}) — {len(body)} симв."); return body
    except Exception as e: logger.warning(f"GigaChat: {e}")
    return None

GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
def groq_chat(system, user, max_tokens=1200, temperature=0.75):
    if not GROQ_API_KEY: return None
    for model in GROQ_MODELS:
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": temperature, "max_tokens": max_tokens}, timeout=60)
            if r.status_code == 200:
                body = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if body and len(body) > 80:
                    logger.info(f"✅ Groq ({model}) — {len(body)} симв."); return body
        except Exception: continue
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
        r = requests.get(f"https://text.pollinations.ai/{requests.utils.quote(prompt[:1500])}", timeout=60)
        if r.status_code == 200 and r.text:
            t = r.text.strip()
            if any(b.lower() in t.lower() for b in ["budget", "api key", "raise the key"]): return None
            cyr = sum(1 for c in t if 'а' <= c.lower() <= 'я')
            if len(t) > 100 and cyr > 30: return t[:max_chars]
    except Exception: pass
    return None

# ================== TELEGRAM ==================
def tg_api(method, **params):
    if not TELEGRAM_TOKEN: return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        r = requests.post(url, json=params, timeout=15) # Уменьшили таймаут до 15 сек
        if r.status_code != 200: return None
        return r.json()
    except Exception: return None

def tg_init():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN: return None
    me = tg_api("getMe")
    if not me or not me.get("ok"):
        logger.warning("📨 Telegram: getMe не прошёл (сервер заблокирован)")
        return None
    logger.info(f"📨 Telegram: бот @{me['result'].get('username')} — OK")
    tg_api("deleteWebhook")
    if TELEGRAM_CHAT_ID: return TELEGRAM_CHAT_ID
    upd = tg_api("getUpdates", timeout=1)
    if upd and upd.get("ok") and upd.get("result"):
        for u in reversed(upd["result"]):
            cid = (u.get("message") or {}).get("chat", {}).get("id")
            if cid: TELEGRAM_CHAT_ID = str(cid); return TELEGRAM_CHAT_ID
    return None

def tg_send(text, chat_id=None):
    if not TELEGRAM_TOKEN: return
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": cid, "text": text[:4000], "parse_mode": "HTML"}, timeout=15)
    except Exception: pass

def tg_command_loop():
    global TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN: return
    
    # Инициализация Telegram ВНУТРИ потока, чтобы не блокировать запуск бота
    tg_init()
    logger.info("📨 Слушаю Telegram (в фоне)")
    
    offset = 0
    upd = tg_api("getUpdates", timeout=1)
    if upd and upd.get("ok") and upd.get("result"):
        offset = upd["result"][-1]["update_id"] + 1
    while True:
        try:
            upd = tg_api("getUpdates", timeout=25, offset=offset)
            if not upd or not upd.get("ok"): time.sleep(10); continue
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if not text: continue
                parts = text.split(); cmd = parts[0].lower().split("@")[0]
                logger.info(f"📨 Команда: {cmd}")
                if cmd == "/start":
                    tg_send(f"🤖 <b>{BOT_NAME}</b>\n\n/test — пост\n/diag — диагностика\n/llm — проверка LLM", chat_id=chat_id)
                elif cmd == "/test":
                    tg_send("🧪 Запускаю пост...", chat_id=chat_id)
                    threading.Thread(target=post_now, daemon=True).start()
        except Exception as e:
            logger.error(f"TG loop: {e}"); time.sleep(10)

# ================== ОСТАЛЬНОЙ КОД (ПЕРЕВОД, ТЕМЫ, ПРОМПТЫ) ==================
def _is_ru(text):
    return sum(1 for c in text if 'а' <= c.lower() <= 'я') > len(text) * 0.3 if text else False

def to_ru(t):
    if _is_ru(t): return t
    try:
        r = requests.get("https://api.mymemory.translated.net/get", params={"q": t[:500], "langpair": "en|ru"}, timeout=20)
        if r.status_code == 200: return r.json().get("responseData", {}).get("translatedText", t)
    except Exception: pass
    return t

def to_en(t): return t

def is_topic_ok(title, summary):
    t = (title or "").lower()
    return not any(bad in t for bad in TOPIC_BLACKLIST)

DEFAULT_TOPICS = ["Как начать писать музыку дома без студии", "Как монетизировать свои треки на стримингах", "Что такое сведение и мастеринг простыми словами", "Как выбрать первую гитару или синтезатор"]
TOPICS_TIP = ["Как записать вокал дома без шумоизоляции", "Как настроить микрофон для домашней студии", "Как свести трек, чтобы он звучал громко и чисто"]
TOPICS_CASE = ["Как парень записал хит у себя дома за выходные", "История группы, которая собрала миллион без лейбла"]
TOPICS_TOOL = ["Suno — как AI создаёт треки за минуту", "BandLab — бесплатная студия в браузере"]
TOPICS_PROMPT = ["Напиши текст песни о первой любви", "Придумай концепцию альбома в стиле lo-fi"]
TOPICS_MYTH = ["Чтобы писать музыку, нужно музыкальное образование", "Хороший трек невозможно записать дома"]
TOPICS_QUESTION = ["Какой трек вы слушаете прямо сейчас?", "Какая группа изменила вашу жизнь?"]

BASE_RULES = (
    "Ты — опытный автор постов для русскоязычного музыкального сообщества. "
    "Формат: хук, суть, 2 примера, совет, вывод, вопрос. 800-1500 символов. "
    "Только русский. Без markdown. Без клише."
)
def gen_news(t, s=""): return generate_llm(BASE_RULES + "\nТИП: НОВОСТЬ", f"Новость: {t}\nКонтекст: {s[:800]}\nНапиши пост.")
def gen_tip(t, s=""): return generate_llm(BASE_RULES + "\nТИП: ЛАЙФХАК", f"Тема: {t}\nНапиши пост.")
def gen_case(t, s=""): return generate_llm(BASE_RULES + "\nТИП: ИСТОРИЯ", f"История: {t}\nНапиши пост.")
def gen_tool(t, s=""): return generate_llm(BASE_RULES + "\nТИП: ИНСТРУМЕНТ", f"Инструмент: {t}\nНапиши пост.")
def gen_myth(t, s=""): return generate_llm(BASE_RULES + "\nТИП: МИФ vs ФАКТ", f"Миф: {t}\nНапиши пост.")
def gen_question(t, s=""): return generate_llm(BASE_RULES + "\nТИП: ВОПРОС", f"Тема: {t}\nНапиши пост.")
def gen_quiz(t, s=""):
    body, name = generate_llm("Составь викторину. Формат: Вопрос: ... A) ... B) ... C) ... D) ... ОТВЕТ: ...", f"Тема: {t}")
    if not body: return None, None
    m = re.split(r"\n*\s*ОТВЕТ\s*:\s*", body, flags=re.IGNORECASE)
    return (m[0].strip(), "✅ Ответ: " + m[1].strip()) if len(m) == 2 else (body.strip(), None)

def choose_post_type(): return random.choices(list(POST_TYPE_WEIGHTS.keys()), weights=list(POST_TYPE_WEIGHTS.values()), k=1)[0]
def get_topic_for_type(ptype):
    if ptype == "news": return get_news_topic()
    if ptype == "tip": t = random.choice(TOPICS_TIP); return t, "", "TIPS", to_en(t)
    if ptype == "case": t = random.choice(TOPICS_CASE); return t, "", "CASES", to_en(t)
    if ptype == "tool": t = random.choice(TOPICS_TOOL); return t, "", "TOOLS", to_en(t)
    if ptype == "prompt": t = random.choice(TOPICS_PROMPT); return t, "", "PROMPTS", to_en(t)
    if ptype == "myth": t = random.choice(TOPICS_MYTH); return t, "", "MYTHS", to_en(t)
    if ptype == "question": t = random.choice(TOPICS_QUESTION); return t, "", "QUESTIONS", to_en(t)
    if ptype == "quiz": t = random.choice(TOPICS_TIP); return t, "", "QUIZ", to_en(t)
    return get_news_topic()

def check_rss_sources():
    logger.info(f"📡 Проверяю RSS ({len(RSS_SOURCES)} шт.)...")
    total = 0
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url); n = len(feed.entries); total += n
            logger.info(f"  {'✅' if n else '⚠️'} {url} — тем: {n}")
        except Exception as e: logger.error(f"  ❌ {url} — {e}")
    logger.info(f"📡 Всего тем из RSS: {total}")

def get_news_topic():
    raw = []
    for url in RSS_SOURCES:
        try:
            for e in feedparser.parse(url).entries[:8]:
                title = getattr(e, "title", "").strip()
                summary = re.sub(r"<[^>]+>", " ", getattr(e, "summary", "") or "").strip()
                if title and is_topic_ok(title, summary): raw.append((title, summary, url))
        except Exception: pass
    candidates = raw if raw else [(t, "", "DEFAULT") for t in DEFAULT_TOPICS]
    with STATE_LOCK: STATE["topics_count"] = len(candidates)
    topic_orig, summary, source = random.choice(candidates)
    return to_ru(topic_orig), summary, source, to_en(topic_orig)

def smart_fallback(topic, summary="", ptype="news"):
    return f"{topic}\n\nТема, которая стоит внимания. Если коротко: музыкальная индустрия меняется быстро.\n\nЧто можно сделать: попробовать новый сервис.\n\nА вы уже пробовали? 👇"

def vk_api_call(method, **params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VER})
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
        return (None, r["error"]) if "error" in r else (r.get("response"), None)
    except Exception as e: return None, {"error_code": -1, "error_msg": str(e)}

def publish(text):
    resp, err = vk_api_call("wall.post", owner_id=GROUP_ID, from_group=1, message=text)
    if err:
        logger.error(f"VK wall.post: {err.get('error_code')} — {err.get('error_msg')}")
        return False, None
    logger.info(f"✅ Пост опубликован (post_id={resp['post_id']})")
    return True, resp["post_id"]

TYPE_EMOJI = {"news": ["🎵", "🎶", "📰"], "tip": ["💡", "🎼", "🎧"], "case": ["🎤", "🌟", "📖"], "quiz": ["🎯", "❓", "🧩"], "tool": ["🧰", "🛠", "🎚"], "prompt": ["✍️", "📝", "🎨"], "myth": ["🔍", "❌", "✅"], "question": ["❓", "💬", "🎤"]}
TYPE_HASHTAGS = {"news": "#музыка #новости", "tip": "#музыка #лайфхак", "case": "#музыка #история", "quiz": "#музыка #викторина", "tool": "#музыка #инструменты", "prompt": "#музыка #промпты", "myth": "#музыка #мифы", "question": "#музыка #обсуждение"}

def post_now(forced_type=None):
    ptype = forced_type or choose_post_type()
    logger.info(f"⏰ [{BOT_NAME}] Запуск постинга (тип: {ptype})")
    topic_ru, summary, source, topic_en = get_topic_for_type(ptype)
    logger.info(f"📝 Тема RU: {topic_ru}")

    quiz_answer = None
    if ptype == "news": res = gen_news(topic_ru, summary)
    elif ptype == "tip": res = gen_tip(topic_ru)
    elif ptype == "case": res = gen_case(topic_ru)
    elif ptype == "quiz":
        body_q, quiz_answer = gen_quiz(topic_ru); res = (body_q, "llm")
    elif ptype == "tool": res = gen_tool(topic_ru)
    elif ptype == "prompt": res = gen_prompt(topic_ru)
    elif ptype == "myth": res = gen_myth(topic_ru)
    elif ptype == "question": res = gen_question(topic_ru)
    else: res = gen_news(topic_ru, summary)

    body, name = res if isinstance(res, tuple) else (res, None)
    gen_name = name or "llm"
    if not body:
        logger.warning("⚠️ LLM упал — умный fallback")
        gen_name = "fallback"; body = smart_fallback(topic_ru, summary, ptype)

    e = random.choice(TYPE_EMOJI.get(ptype, ["🎵"]))
    tags = TYPE_HASHTAGS.get(ptype, "#музыка")
    text = f"{e} {topic_ru}\n\n{body}\n\n{tags}"
    logger.info(f"📄 Текст ({len(text)} симв.) от {gen_name}: {text[:150]}...")

    ok, post_id = publish(text)
    with STATE_LOCK:
        STATE["last_post_topic"] = topic_ru; STATE["last_post_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        STATE["last_post_ok"] = ok; STATE["last_gen"] = gen_name; STATE["last_type"] = ptype
    
    if ok:
        tg_send(f"🚀 <b>[{BOT_NAME}]</b> Пост ({ptype})\n<b>Тема:</b> {topic_ru}\n\n{text[:800]}")
    logger.info(f"🏁 [{BOT_NAME}] Цикл завершён ({ptype})\n")

def scheduler_loop():
    last_run = {"morning": None, "evening": None}
    while True:
        try:
            now = datetime.now()
            for idx, t in enumerate(POST_TIMES):
                hh, mm = map(int, t.split(":"))
                key = "morning" if idx == 0 else "evening"
                if (now.hour == hh and now.minute == mm and last_run[key] != now.date()):
                    last_run[key] = now.date()
                    logger.info(f"⏰ Запуск по расписанию: {t}")
                    threading.Thread(target=post_now, daemon=True).start()
        except Exception: pass
        time.sleep(30)

def main():
    logger.info(f"🚀 {BOT_NAME} запущен")
    logger.info(f"📡 Группа: {GROUP_ID}")
    logger.info(f"⏰ Расписание: {', '.join(POST_TIMES)}")
    
    # ЗАПУСК TELEGRAM В ФОНЕ (чтобы не блокировал VK)
    if TELEGRAM_TOKEN:
        threading.Thread(target=tg_command_loop, daemon=True).start()
    
    check_rss_sources()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    logger.info(f"  → задачи: {', '.join(POST_TIMES)}")

    while True: time.sleep(60)

if __name__ == "__main__":
    if not VK_TOKEN: sys.exit(1)
    try: main()
    except KeyboardInterrupt: pass