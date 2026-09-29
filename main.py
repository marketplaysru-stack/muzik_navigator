#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Музыкальный Навигатор. Группа 240656847. Посты в 09:00 и 15:00.
Без зависимости от Pillow (баннер-резерв отключён).
"""

import os, sys, time, json, random, hashlib, logging, requests, re, threading
import urllib.request, urllib.parse, urllib.error
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

VK_TOKEN        = _env("VK_TOKEN_MUSIC", "MUSIC_VK_TOKEN", "VK_TOKEN_AI", "VK_TOKEN")
VK_TOKEN_USER   = _env("VK_TOKEN_USER", "USER_VK_TOKEN")
HUGGINGFACE_KEY = _env("HUGGINGFACE_API_KEY", "HF_API_KEY", "HF_TOKEN")
PEXELS_KEY      = _env("PEXELS_API_KEY", "PEXELS_TOKEN")
GROQ_API_KEY    = _env("GROQ_API_KEY", "GROQ_KEY")

TELEGRAM_TOKEN   = _env("MUSIC_TELEGRAM_TOKEN", "MUSIC_TG_TOKEN", "TELEGRAM_TOKEN", "TG_TOKEN")
TELEGRAM_CHAT_ID = _env("MUSIC_CHAT_ID", "MUSIC_TG_CHAT_ID", "TELEGRAM_CHAT_ID", "AI_CHAT_ID")

# ================== ТИПЫ ПОСТОВ ==================
POST_TYPE_WEIGHTS = {
    "tip":      22,
    "case":     18,
    "news":     20,
    "tool":     12,
    "myth":     10,
    "prompt":   10,
    "question":  4,
    "quiz":      4,
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
    "last_img_source": None,
    "last_type": None,
    "type_counters": {k: 0 for k in POST_TYPE_WEIGHTS},
}
STATE_LOCK = threading.Lock()


# ================== TELEGRAM ==================
def tg_api(method, **params):
    if not TELEGRAM_TOKEN: return None
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
        return requests.post(url, json=params, timeout=20).json()
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
    logger.warning(f"📨 Telegram: напиши /start боту @{bot_username}")
    return None


def tg_send(text, chat_id=None):
    if not TELEGRAM_TOKEN: return
    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": cid, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=15)
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
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
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
                logger.error("Groq 401"); return None
            elif r.status_code == 429:
                last_err = "429"; continue
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
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": "Скажи ok"}],
                      "max_tokens": 10}, timeout=30)
            if r.status_code == 200:
                return f"✅ Groq: OK ({model})"
            elif r.status_code == 401:
                return "❌ Groq: 401 — ключ неверный"
            elif r.status_code in (429, 404):
                continue
        except Exception as e:
            return f"❌ Groq: {type(e).__name__}: {str(e)[:150]}"
    return "❌ Groq: ни одна модель недоступна"


def diag_vk_group():
    if not VK_TOKEN: return "❌ VK group: токен НЕ задан"
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


def diag_vk_user():
    if not VK_TOKEN_USER:
        return "❌ VK user: токен НЕ задан — фото не прикрепится"
    try:
        r = requests.get("https://api.vk.com/method/users.get",
                         params={"access_token": VK_TOKEN_USER, "v": VK_API_VER},
                         timeout=15).json()
        if "error" in r:
            return f"❌ VK user: {r['error'].get('error_code')} — {r['error'].get('error_msg')}"
        u = r["response"][0]
        return f"✅ VK user: id={u.get('id')} ({u.get('first_name','')} {u.get('last_name','')})"
    except Exception as e:
        return f"❌ VK user: {str(e)[:100]}"


def diag_check_pexels():
    if not PEXELS_KEY: return "⚠️ Pexels: ключ не задан"
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         headers={"Authorization": PEXELS_KEY},
                         params={"query": "music", "per_page": 1}, timeout=15)
        return "✅ Pexels: OK" if r.status_code == 200 else f"❌ Pexels: HTTP {r.status_code}"
    except Exception as e:
        return f"❌ Pexels: {str(e)[:80]}"


def diag_check_pollinations():
    try:
        r = requests.get(
            "https://image.pollinations.ai/prompt/music?width=256&height=256&nologo=true",
            timeout=30)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return "✅ Pollinations (img): OK"
        return f"⚠️ Pollinations: HTTP {r.status_code}, ct={r.headers.get('content-type')}"
    except Exception as e:
        return f"❌ Pollinations: {str(e)[:80]}"


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
        f"  VK_TOKEN_MUSIC: {'✅ есть' if VK_TOKEN else '❌ НЕТ'}",
        f"  VK_TOKEN_USER: {'✅ есть' if VK_TOKEN_USER else '❌ НЕТ'}",
        f"  PEXELS_API_KEY: {'✅ есть' if PEXELS_KEY else '⚠️ нет'}", "",
        "<b>Проверка сервисов:</b>",
        diag_check_groq(),
        diag_vk_group(),
        diag_vk_user(),
        diag_check_pexels(),
        diag_check_pollinations(), "",
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
                    tg_send("🔧 Диагностика, подожди ~15 сек...")
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
                            f"Картинка: {s['last_img_source'] or '—'}\n"
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
    promo = ["скидка", "промокод", "записаться", "регистрация",
             "бесплатно", "вебинар", "подпишись", "подписывайтесь"]
    if sum(1 for m in promo if m in s) >= 3: return False
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


# ================== КАРТИНКИ (без PIL) ==================
IMG_STYLES_EN = [
    "moody concert photograph, stage lights, crowd in blur",
    "minimalist vinyl record cover, clean design",
    "vintage music shop interior, warm nostalgic light",
    "close-up of guitar strings, warm bokeh background",
    "abstract audio waveform visualization, colorful",
    "retro cassette tape, 80s aesthetic, soft light",
    "atmospheric studio with synthesizers, soft neon",
    "musician silhouette at sunset, cinematic lighting",
    "warm flat illustration of headphones and coffee",
    "piano keys close-up, soft golden light",
]
IMG_COLORS_EN = [
    "warm amber and deep purple palette",
    "moody dark blue with gold accents",
    "vintage sepia and cream tones",
    "neon pink and cyan club lighting",
    "warm sunset orange and violet",
    "clean monochrome with one bright accent",
]
IMG_LIGHT_EN = ["soft stage lights", "golden hour glow",
                "moody club lighting", "warm lamp light"]
IMG_COMP_EN = ["close-up shot", "wide atmospheric scene",
               "top-down flat lay", "cinematic portrait"]
NEGATIVE_SUFFIX = (
    "high quality, detailed, 4k, sharp focus, atmospheric, "
    "no text, no watermark, no logo, no letters, no captions"
)


def build_image_prompt(topic_en):
    return (f"{topic_en}, {random.choice(IMG_STYLES_EN)}, "
            f"{random.choice(IMG_COLORS_EN)}, {random.choice(IMG_LIGHT_EN)}, "
            f"{random.choice(IMG_COMP_EN)}, {NEGATIVE_SUFFIX}")


PEXELS_STOPWORDS = {
    "the","a","an","and","or","of","in","to","for","on","with","is","are",
    "was","were","be","been","has","have","had","new","how","why","what",
    "when","where","who","which","that","this","these","those","your","you",
    "my","me","we","us","our","their","its","it","can","will","would","could",
    "should","may","make","makes","made","get","gets","got","use","uses","used",
    "like","likes","know","knows","think","thinks","see","looks","goes","takes",
    "still","not","no","yes","way","ways","thing","things","just","only",
    "also","even","very","really","quite","too","so","such","one","two","all",
}


def pexels_query_from_topic(topic_en):
    words = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", topic_en)
    useful = [w.lower() for w in words if w.lower() not in PEXELS_STOPWORDS]
    seen = set(); uniq = []
    for w in useful:
        if w not in seen: seen.add(w); uniq.append(w)
    if not uniq: uniq = ["music", "concert"]
    return " ".join(uniq[:2])


def gen_img_poll(prompt):
    try:
        seed = random.randint(1, 999999)
        url = (f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
               f"?width=1024&height=576&nologo=true&enhance=true&seed={seed}")
        r = requests.get(url, timeout=60)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and r.content and "image" in ct and len(r.content) > 10000:
            return r.content
        logger.info(f"Pollinations img: ct={ct}, len={len(r.content) if r.content else 0}")
    except Exception as e:
        logger.warning(f"Pollinations img: {e}")
    return None


def gen_img_hf(prompt):
    if not HUGGINGFACE_KEY: return None
    try:
        r = requests.post(
            "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-2-1",
            headers={"Authorization": f"Bearer {HUGGINGFACE_KEY}"},
            json={"inputs": prompt, "options": {"wait_for_model": True}}, timeout=60)
        if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
            return r.content
    except Exception as e:
        logger.debug(f"HF img: {e}")
    return None


def gen_img_pexels(query_en):
    if not PEXELS_KEY: return None
    try:
        logger.info(f"🖼 Pexels запрос: '{query_en}'")
        r = requests.get("https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": query_en, "per_page": 15, "orientation": "landscape"},
            timeout=20)
        if r.status_code == 200:
            ph = r.json().get("photos", [])
            if ph:
                img = requests.get(random.choice(ph[:10])["src"]["large"], timeout=30)
                if img.status_code == 200: return img.content
    except Exception as e:
        logger.warning(f"Pexels: {e}")
    return None


def generate_image(topic_ru, topic_en):
    """Без PIL — баннер не генерируем, при неудаче публикуем без фото."""
    if not topic_en: topic_en = to_en(topic_ru)
    prompt = build_image_prompt(topic_en)
    logger.info(f"🖼 Промпт: {prompt[:140]}...")
    img = gen_img_poll(prompt)
    if img: logger.info(f"✅ Картинка: Pollinations ({len(img)} б)"); return img, "pollinations"
    img = gen_img_hf(prompt)
    if img: logger.info(f"✅ Картинка: HF ({len(img)} б)"); return img, "hf"
    img = gen_img_pexels(pexels_query_from_topic(topic_en))
    if img: logger.info(f"✅ Картинка: Pexels ({len(img)} б)"); return img, "pexels"
    logger.warning("⚠️ Все API картинок упали — публикуем без фото")
    return None, "none"


# ================== ПРОМПТЫ ==================
BASE_RULES = (
    "Ты — автор постов для русскоязычного музыкального сообщества. "
    "Твои читатели — обычные люди: слушатели, начинающие музыканты, продюсеры. "
    "Пиши ТАК ПРОСТО, чтобы понял любой. Максимум 12-15 слов в предложении. "
    "В конце — простой вопрос. Только русский. Без markdown, без эмодзи в тексте."
)


def gen_news(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Новость»: 5-6 предложений. Что произошло → что это значит → что делать."
    )
    return groq_chat(system, f"Новость: {topic}\nКонтекст: {summary[:700] if summary else '(нет)'}\nНапиши пост.")


def gen_tip(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Лайфхак»: 4-5 предложений. Проблема → приём → результат → вопрос."
    )
    return groq_chat(system, f"Тема: {topic}\nНапиши пост.")


def gen_case(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Реальная история»: 5-6 предложений от третьего лица. "
        "Что было → что сделал → результат → вывод."
    )
    return groq_chat(system, f"История: {topic}\nНапиши пост.")


def gen_quiz(topic, summary=""):
    system = (
        "Ты — автор музыкальной викторины.\n\n"
        "ФОРМАТ (строго, без markdown):\n"
        "Вопрос: <вопрос>\nA) <вариант>\nB) <вариант>\nC) <вариант>\nD) <вариант>\n"
        "Пиши ответ в комментариях 👇\n\n"
        "Затем с новой строки строго:\nОТВЕТ: <буква> — <пояснение>\n\n"
        "Только русский."
    )
    body = groq_chat(system, f"Тема: {topic}\nСоставь викторину.",
                     max_tokens=500, temperature=0.8)
    if not body: return None, None
    m = re.split(r"\n*\s*ОТВЕТ\s*:\s*", body, flags=re.IGNORECASE)
    if len(m) == 2:
        return m[0].strip(), "✅ Ответ: " + m[1].strip()
    return body.strip(), None


def gen_tool(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Инструмент»: 5-6 предложений. Что это → примеры → для кого → приём → вопрос."
    )
    return groq_chat(system, f"Инструмент: {topic}\nНапиши пост.")


def gen_myth(topic, summary=""):
    system = BASE_RULES + (
        "\n\nФормат «Миф vs Факт»: «Миф: ...» → «На самом деле: ...» → пример → вопрос."
    )
    return groq_chat(system, f"Миф: {topic}\nНапиши пост.")


def gen_prompt(topic, summary=""):
    system = (
        "Ты — автор постов про AI для музыкантов.\n\n"
        "Формат «Промпт»:\n"
        "1. Зачем промпт — одно предложение.\n"
        "2. Пустая строка.\n"
        "3. Промпт в «ёлочках» — готовый к копированию.\n"
        "4. Пустая строка.\n"
        "5. Что подставить + вопрос.\n\n"
        "Только русский."
    )
    return groq_chat(system, f"Задача: {topic}\nСоставь промпт-пост.")


def gen_question(topic, summary=""):
    system = BASE_RULES + "\n\nФормат «Вопрос подписчикам»: 3-4 предложения + вопрос."
    return groq_chat(system, f"Вопрос: {topic}\nНапиши пост.")


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
        candidates = [(t, "", "DEFAULT") for t in DEFAULT_TOPICS]
    with STATE_LOCK:
        STATE["topics_count"] = len(candidates)
    topic_orig, summary, source = random.choice(candidates)
    return to_ru(topic_orig), summary, source, (topic_orig if not _is_ru(topic_orig) else to_en(topic_orig))


# ================== ФОЛБЭК ==================
FALLBACK_BY_TYPE = {
    "tip": ["Хочешь записать трек дома, но кажется, что «звук не тот»?\n\n"
            "Начни с одного: обработай вокал компрессором и добавь реверб.\n\n"
            "А вы что используете для вокала? 👇"],
    "case": ["Один парень записал трек дома за выходные — и он завирусился в TikTok.\n\n"
             "Сделал сэмпл из старой песни, добавил бит. Через неделю — 200 тысяч прослушиваний.\n\n"
             "А вы пробовали делать сэмплы? 👇"],
    "tool": ["Suno — нейросеть, которая создаёт треки за 30 секунд по описанию.\n\n"
             "Можно попросить: «лёгкий lo-fi для учёбы» или «энергичный рок».\n\n"
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
def vk_api_call(method, token, **params):
    params.update({"access_token": token, "v": VK_API_VER})
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
        if "error" in r:
            return None, r["error"]
        return r.get("response"), None
    except Exception as e:
        return None, {"error_code": -1, "error_msg": str(e)}


def upload_photo(image_data):
    if not VK_TOKEN_USER:
        logger.warning("⚠️ VK_TOKEN_USER не задан")
        return None
    if not image_data: return None

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        logger.info(f"📤 Фото, попытка {attempt}/{max_attempts} ({len(image_data)} б)")
        resp, err = vk_api_call("photos.getWallUploadServer", VK_TOKEN_USER,
                                group_id=abs(GROUP_ID))
        if err:
            code = err.get("error_code")
            if code == 9:
                wait = 30 * attempt
                logger.warning(f"🚫 Flood control. Жду {wait} сек")
                time.sleep(wait); continue
            logger.error(f"VK upload_url: {code} — {err.get('error_msg')}")
            return None
        if not resp or "upload_url" not in resp:
            return None
        upload_url = resp["upload_url"]

        try:
            boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
            body = b"".join([
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="photo"; filename="post.jpg"\r\n',
                b"Content-Type: image/jpeg\r\n\r\n",
                image_data,
                f"\r\n--{boundary}--\r\n".encode(),
            ])
            req = urllib.request.Request(
                upload_url, data=body, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                up = json.loads(r.read())
        except Exception as e:
            logger.error(f"Загрузка файла: {type(e).__name__}: {e}")
            if attempt < max_attempts:
                time.sleep(15 * attempt); continue
            return None

        if not up.get("photo") or up["photo"] == "[]":
            logger.error("VK вернул пустой photo")
            return None

        saved, err = vk_api_call("photos.saveWallPhoto", VK_TOKEN_USER,
                                 group_id=abs(GROUP_ID),
                                 photo=up["photo"], hash=up.get("hash", ""),
                                 server=up.get("server", ""))
        if err:
            code = err.get("error_code")
            if code == 9:
                wait = 30 * attempt
                logger.warning(f"🚫 Flood control saveWallPhoto. Жду {wait} сек")
                time.sleep(wait); continue
            logger.error(f"VK saveWallPhoto: {code} — {err.get('error_msg')}")
            return None
        if not saved or not isinstance(saved, list) or not saved:
            return None

        p = saved[0]
        att = f"photo{p['owner_id']}_{p['id']}"
        logger.info(f"  3/3 ✅ Фото: {att}")
        return att
    return None


def publish(text, image_data):
    att = None
    if image_data:
        att = upload_photo(image_data)
        if att: time.sleep(2)
        else: logger.warning("⚠️ Пост уйдёт без фото")

    resp, err = vk_api_call("wall.post", VK_TOKEN,
                            owner_id=GROUP_ID, from_group=1,
                            message=text, attachments=att or "")
    if err:
        logger.error(f"VK wall.post: {err.get('error_code')} — {err.get('error_msg')}")
        return False, None
    if resp and "post_id" in resp:
        pid = resp["post_id"]
        logger.info(f"✅ Пост (post_id={pid}, фото={'✅' if att else '❌'})")
        return True, pid
    return False, None


def post_comment(post_id, text):
    resp, err = vk_api_call("wall.createComment", VK_TOKEN,
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


# ================== КЭШ ==================
def img_hash(img): return hashlib.md5(img).hexdigest()
def is_cached(h):
    f = os.path.join(DATA_DIR, "image_cache.txt")
    if not os.path.exists(f): return False
    with open(f) as fp: return h in {l.strip() for l in fp}
def save_cache(h):
    with open(os.path.join(DATA_DIR, "image_cache.txt"), "a") as fp: fp.write(h + "\n")


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

    img, img_source = generate_image(topic_ru, topic_en)
    if img:
        h = img_hash(img)
        if is_cached(h):
            logger.info("🔁 Дубль — перегенерация")
            img, img_source = generate_image(topic_ru, topic_en + " alt style")
            if img: h = img_hash(img)
        if img: save_cache(h)

    ok, post_id = publish(text, img)

    if ok and ptype == "quiz" and quiz_answer and QUIZ_ANSWER_IN_COMMENT and post_id:
        time.sleep(QUIZ_ANSWER_DELAY_SEC)
        post_comment(post_id, quiz_answer)

    with STATE_LOCK:
        STATE["last_post_topic"] = topic_ru
        STATE["last_post_time"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        STATE["last_post_ok"] = ok
        STATE["last_source"] = source
        STATE["last_gen"] = gen_name
        STATE["last_img_source"] = img_source
        STATE["last_type"] = ptype
        STATE["type_counters"][ptype] = STATE["type_counters"].get(ptype, 0) + 1

    if ok:
        tg_send(f"🚀 <b>[{BOT_NAME}]</b> Пост ({ptype})\n"
                f"<b>Тема:</b> {topic_ru}\n"
                f"<b>Текст:</b> {gen_name}\n"
                f"<b>Картинка:</b> {img_source}\n"
                f"{'<b>Ответ в комментах:</b> да' if quiz_answer else ''}\n\n"
                f"{text[:800]}")
    else:
        tg_send(f"❌ <b>[{BOT_NAME}]</b> Не опубликовано ({ptype})")

    logger.info(f"🏁 [{BOT_NAME}] Цикл завершён ({ptype})\n")


def main():
    logger.info(f"🚀 {BOT_NAME} запущен")
    logger.info(f"📡 Группа: {GROUP_ID}")
    logger.info(f"⏰ Расписание: {', '.join(POST_TIMES)}")
    logger.info(f"🔑 VK group: {'есть' if VK_TOKEN else 'НЕТ'}")
    logger.info(f"🔑 VK user: {'есть' if VK_TOKEN_USER else 'НЕТ'}")
    logger.info(f"🔑 Groq: {'есть' if GROQ_API_KEY else 'НЕТ'}")
    logger.info(f"🔑 Pexels: {'есть' if PEXELS_KEY else 'НЕТ'}")
    logger.info(f"🎲 Типы постов: {POST_TYPE_WEIGHTS}")

    tg_init()
    if TELEGRAM_TOKEN:
        threading.Thread(target=tg_command_loop, daemon=True).start()
        if TELEGRAM_CHAT_ID:
            tg_send(f"🟢 <b>[{BOT_NAME}]</b> запущен\n"
                    f"Groq: {'✅' if GROQ_API_KEY else '❌'}\n"
                    f"VK user: {'✅' if VK_TOKEN_USER else '❌'}\n"
                    f"Расписание: {', '.join(POST_TIMES)}\n"
                    f"/test · /diag · /groq · /types · /status")

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
        logger.error("❌ Нет VK_TOKEN_MUSIC / VK_TOKEN_AI"); sys.exit(1)
    try: main()
    except KeyboardInterrupt: logger.info("Остановлено")