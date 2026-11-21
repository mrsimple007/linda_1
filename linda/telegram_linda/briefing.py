"""Telegram side of Linda: morning message, news digest with links, commands, and filing what you write.

NEWS      RSS feeds (free, no quota, with links)  ->  an LLM picks/groups/rewrites by INDEX  ->  links are attached
          by code, never by the model, so they cannot be invented or altered. Order of LLMs: Groq/Mistral
          (agent.providers), then Gemini. If every LLM is down you still get the raw headlines with links.
TELEGRAM  /start /help /news /tasks /birthdays /plans /idea /goal ... or just write: an LLM decides whether it is
          an idea, goal, profile fact, task, diary line, progress note or quick note, and saves it with Linda's
          own tools. Nothing is spoken aloud on the Mac while this happens.
"""
import concurrent.futures
import contextlib
import datetime
import difflib
import html as _html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests
import os

from .. import tools
from ..i18n import fmt_date, fmt_today
from .. import reference
from .telegram import esc
from .finance import FinanceError



PLANS_WINDOW = 4 * 3600          # after the morning message, a plain reply is read as "today's plans" first
TIME = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b")
LANG_NAME = {"en": "English", "ru": "Russian", "uz": "Uzbek"}
FEEDS = ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://feeds.bbci.co.uk/news/business/rss.xml",
         "https://feeds.bbci.co.uk/news/technology/rss.xml", "https://feeds.npr.org/1001/rss.xml"]
TG_WORDS = re.compile(r"telegram|\bsend\b|отправ|пришли|yubor|jo'?nat", re.I)
PLAN_WORDS = re.compile(r"\bplans?\b|\btasks?\b|\bto-?do\b|планы?\b|задач|rejalar|vazifa", re.I)
COMMANDS = [("start", "Meet Linda"), ("today", "News, today's tasks and birthdays"),
            ("stats", "Ask about your bots: users, activity, payments"),
            ("client", "Add or show a client"), ("clients", "All clients and their progress"),
            ("news", "Latest headlines with links"), ("tasks", "Your open tasks"),
            ("birthdays", "All saved birthdays"), ("plans", "Add today's plans"), ("idea", "Save an idea"),
            ("goal", "Save a goal"), ("done", "Mark a task done"), ("notes", "Everything I remember")]

S = {
    "en": {"morning": "Good morning, {user}", "hello": "Hello, {user}", "intro": "Linda is here. Write me today's plans, one per line, and I'll save "
           "them to today's tasks and your journal.", "news": "Latest news", "tasks": "On your list",
           "bday": "Birthdays today", "saved": "Saved", "dup": "Already on your list",
           "footer": "Reply to this message with your plans.", "hint_title": "Linda",
           "empty": "I couldn't find any plans in that message. Write one per line.",
           "plans_saved": "Saved for today", "done_footer": "You can send more: I keep listening for a few hours.",
           "no_ai": "I couldn't analyze that right now (the AI services are busy). Try /idea, /goal or /plans "
                    "followed by your text, or send it again in a minute.",
           "unknown": "I don't know that command. Try /start.", "usage": "Write it after the command, like: {ex}",
           "news_fail": "I couldn't reach the news feeds right now. Try again in a minute.",
           "nothing": "Nothing to save in that one.", "no_tasks": "No open tasks. Enjoy the day!",
           "welcome": "This is Linda, your personal assistant. I live on your Mac, and now I'm here in Telegram too.",
           "can": "What I can do here", "just": "Or just write to me",
           "can_lines": ["/today: news, today's tasks and birthdays", "/client: add or show a client, /clients: all clients", "/news: fresh headlines with links", "/tasks: your open tasks",
                         "/birthdays: every saved birthday", "/plans: add today's plans, one per line",
                         "/idea and /goal: save them in one step"],
           "just_line": "An idea, a goal, a fact about you, a task, a diary thought, or what you worked on. "
                        "I work out where it belongs and save it.",
           "start_footer": "Every morning I send the news and ask for your plans.",
           "today": "Today", "today_tasks": "Tasks for today", "no_today_tasks": "No open tasks for today.",
           "no_bday": "No birthdays today.", "more": "+{n} more: /tasks"},
    "ru": {"morning": "Доброе утро, {user}", "hello": "Привет, {user}", "intro": "Линда на связи. Напиши планы на сегодня, каждый с новой строки, и я "
           "сохраню их в задачи и дневник.", "news": "Главные новости", "tasks": "В твоём списке",
           "bday": "Дни рождения сегодня", "saved": "Сохранено", "dup": "Уже в списке",
           "footer": "Ответь на это сообщение своими планами.", "hint_title": "Линда",
           "empty": "Не нашла планов в сообщении. Пиши по одному на строку.",
           "plans_saved": "Сохранено на сегодня", "done_footer": "Можно прислать ещё: я слушаю несколько часов.",
           "no_ai": "Сейчас не получилось разобрать сообщение (ИИ-сервисы заняты). Попробуй /idea, /goal или /plans "
                    "с текстом, либо отправь ещё раз через минуту.",
           "unknown": "Не знаю такой команды. Попробуй /start.", "usage": "Напиши текст после команды, например: {ex}",
           "news_fail": "Сейчас не получается загрузить новости. Попробуй через минуту.",
           "nothing": "Тут нечего сохранять.", "no_tasks": "Открытых задач нет. Хорошего дня!",
           "welcome": "Это Линда, твой личный помощник. Я живу на твоём Mac, а теперь и здесь, в Telegram.",
           "can": "Что я умею здесь", "just": "Или просто напиши мне",
           "can_lines": ["/today: новости, задачи и дни рождения на сегодня", "/client: добавить или показать клиента, /clients: все клиенты", "/news: свежие новости со ссылками", "/tasks: открытые задачи",
                         "/birthdays: все дни рождения", "/plans: планы на сегодня, каждый с новой строки",
                         "/idea и /goal: сохранить в один шаг"],
           "just_line": "Идею, цель, факт о тебе, задачу, мысль для дневника или что ты сделал. "
                        "Я пойму, куда это отнести, и сохраню.",
           "start_footer": "Каждое утро я присылаю новости и спрашиваю про планы.",
           "today": "Сегодня", "today_tasks": "Задачи на сегодня", "no_today_tasks": "Открытых задач на сегодня нет.",
           "no_bday": "Сегодня дней рождения нет.", "more": "ещё {n}: /tasks"},
    "uz": {"morning": "Xayrli tong, {user}", "hello": "Salom, {user}", "intro": "Linda shu yerda. Bugungi rejalaringizni har birini yangi qatorga "
           "yozing, men ularni bugungi vazifalar va kundalikka saqlayman.", "news": "So'nggi yangiliklar",
           "tasks": "Ro'yxatingizda", "bday": "Bugungi tug'ilgan kunlar", "saved": "Saqlandi",
           "dup": "Ro'yxatda bor edi", "footer": "Rejalaringizni shu xabarga javob qilib yozing.",
           "hint_title": "Linda", "empty": "Xabarda reja topa olmadim. Har birini yangi qatorga yozing.",
           "plans_saved": "Bugunga saqlandi", "done_footer": "Yana yuborishingiz mumkin: bir necha soat tinglayman.",
           "no_ai": "Hozir xabarni tahlil qila olmadim (AI xizmatlari band). /idea, /goal yoki /plans dan keyin "
                    "matn yozing, yoki bir daqiqadan keyin qayta yuboring.",
           "unknown": "Bu buyruqni bilmayman. /start ni sinab ko'ring.", "usage": "Buyruqdan keyin matn yozing: {ex}",
           "news_fail": "Hozir yangiliklarni yuklay olmadim. Bir daqiqadan keyin urinib ko'ring.",
           "nothing": "Bunda saqlaydigan narsa yo'q.", "no_tasks": "Ochiq vazifalar yo'q. Kuningiz xayrli o'tsin!",
           "welcome": "Bu Linda, sizning shaxsiy yordamchingiz. Men Mac'ingizda yashayman, endi Telegramda ham.",
           "can": "Bu yerda nima qila olaman", "just": "Yoki shunchaki yozing",
           "can_lines": ["/today: yangiliklar, bugungi vazifalar va tug'ilgan kunlar", "/client: mijoz qo'shish yoki ko'rish, /clients: barcha mijozlar", "/news: havolali so'nggi yangiliklar", "/tasks: ochiq vazifalar",
                         "/birthdays: barcha tug'ilgan kunlar", "/plans: bugungi rejalar, har biri yangi qatorda",
                         "/idea va /goal: bir qadamda saqlash"],
           "just_line": "G'oya, maqsad, o'zingiz haqingizda fakt, vazifa, kundalik fikr yoki nima qilganingiz. "
                        "Qayerga tegishli ekanini aniqlab, saqlayman.",
           "start_footer": "Har tongda yangiliklar yuboraman va rejalaringizni so'rayman.",
           "today": "Bugun", "today_tasks": "Bugungi vazifalar", "no_today_tasks": "Bugunga ochiq vazifalar yo'q.",
           "no_bday": "Bugun tug'ilgan kunlar yo'q.", "more": "yana {n}: /tasks"},
}
M = {
    "en": {"news_fail": "I couldn't reach the news feeds right now.", "news_sent": "Sent {n} headlines to your Telegram.",
           "no_tg": "Telegram isn't set up.", "plans_say": "Today you have: {x}.", "plans_none": "No plans saved for today yet.",
           "bd_title": "Birthdays", "bd_say": "You have {n} birthdays saved. Next is {name}, {date}, in {d} days.",
           "bd_sent": "I sent the full list of {n} to your Telegram.", "bd_none": "I don't have any birthdays saved yet.",
           "today": "today", "tomorrow": "tomorrow", "in": "in {d} days", "turns": "turns {a}"},
    "ru": {"news_fail": "Сейчас не получается загрузить новости.", "news_sent": "Отправила {n} новостей в Telegram.",
           "no_tg": "Telegram не настроен.", "plans_say": "На сегодня у тебя: {x}.", "plans_none": "Планов на сегодня пока нет.",
           "bd_title": "Дни рождения", "bd_say": "У меня сохранено {n} дней рождения. Ближайший: {name}, {date}, через {d} дн.",
           "bd_sent": "Отправила весь список ({n}) в Telegram.", "bd_none": "Дней рождения пока нет.",
           "today": "сегодня", "tomorrow": "завтра", "in": "через {d} дн.", "turns": "исполняется {a}"},
    "uz": {"news_fail": "Hozir yangiliklarni yuklay olmadim.", "news_sent": "{n} ta yangilikni Telegramga yubordim.",
           "no_tg": "Telegram sozlanmagan.", "plans_say": "Bugun sizda: {x}.", "plans_none": "Bugunga rejalar hali yo'q.",
           "bd_title": "Tug'ilgan kunlar", "bd_say": "{n} ta tug'ilgan kun saqlangan. Eng yaqini: {name}, {date}, {d} kundan keyin.",
           "bd_sent": "To'liq ro'yxatni ({n}) Telegramga yubordim.", "bd_none": "Tug'ilgan kunlar hali yo'q.",
           "today": "bugun", "tomorrow": "ertaga", "in": "{d} kundan keyin", "turns": "{a} yoshga to'ladi"},
}

# Telegram file (briefing.py): near the other regexes
NEWS_ASK = re.compile(r"\b(?:news|headlines)\b|новост|yangilik", re.I)
LEFT_Q = re.compile(r"\bleft\b|remain\w*|остал|qold", re.I)

TASKISH = re.compile(r"\b(tasks?|to-?do|close|closed|done|finished|completed|improv\w*|implement\w*|"
                     r"fixed|add|added)\b|задач|vazifa", re.I)

def _balance_card(tg, svc, sheet):
    icon, name = SHEETS[sheet]
    b = svc.balance(sheet)
    left = b["inc"] - b["exp"]
    lines = [f"🟢 Total income: <b>{_money(b['inc'])}</b>", f"🔴 Total expenses: <b>{_money(b['exp'])}</b>",
             f"{'💰' if left >= 0 else '⚠️'} Left: <b>{_money(left, sign=True)}</b>"]
    since = f" since {b['since']}" if b["since"] else ""
    return tg.send_styled(f"{name} finance · all time", [("", lines)],
                          footer=f"UZS · {b['n']} entries{since}", icon=icon)


def _lang(agent):
    d = agent.cfg.get("default_language", "en")
    return d if d in S else "en"


def _chunk_text(c):
    if isinstance(c, str):
        return c
    t = getattr(c, "text", None)
    return t if isinstance(t, str) else ""

def _save_reference_direct(agent, tg, text):
    topic = re.sub(r"[^\w ]+", "", llm_text(agent, "Reply with ONLY a 2-4 word topic name for this note.",
                                            text[:600], temp=0)).strip()[:40]
    if not topic:
        m = re.search(r"\b([\w']+ (?:course|plan|schedule|outline))\b", text, re.I)
        topic = m.group(1) if m else " ".join(text.split()[:3])
    with muted(agent, text):
        _do(agent, "save_reference", {"topic": topic, "text": text}, text)
    return tg.send_styled(S[_lang(agent)]["saved"],
                          [("", [f"📚 <b>Reference</b>: {esc(topic)}, {len(text)} characters saved in full"])], icon="✅")


# ============================ LLM helper ========================================
def llm_text(agent, system, user, temp=0.2):
    """One plain LLM call: Groq/Mistral first (agent.providers), Gemini last. '' if everything fails."""
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for p in getattr(agent, "providers", None) or []:
        try:
            if not p.ready():
                continue
            r = p.chat(msgs, None, temp=temp, timeout=30)
            txt = (getattr(r, "content", "") or "").strip()
            if txt:
                return txt
        except Exception as e:
            try:
                p.cool(e)
            except Exception:
                pass
            print(f"[llm {getattr(p, 'name', '?')}] failed: {repr(e)[:120]}")
    g = getattr(agent, "gemini", None)
    if g is not None:
        try:
            if agent.cloud():
                return "".join(_chunk_text(c) for c in g.stream(user, system)).strip()
        except Exception as e:
            print("[llm gemini]", repr(e)[:120])
    return ""


def _json_obj(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


# ============================ news ===============================================
def _fetch_feed(url, per):
    r = requests.get(url, timeout=8, headers={"User-Agent": "Linda/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    source = (root.findtext("./channel/title") or url.split("/")[2]).split(" - ")[0].split(":")[0].strip()
    out = []
    for it in root.iter("item"):
        title = _html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(it.findtext("description") or ""))).strip()
        if title and link.startswith("http"):
            out.append({"title": title, "summary": desc[:200], "link": link, "source": source})
        if len(out) >= per:
            break
    return out


def rss_items(agent, per_feed=6):
    """Items from the feeds (config: news.feeds), fetched in parallel, duplicates removed."""
    feeds = (agent.cfg.get("news") or {}).get("feeds") or FEEDS
    items, seen = [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        futs = [pool.submit(_fetch_feed, u, per_feed) for u in feeds]
        for u, f in zip(feeds, futs):
            try:
                for it in f.result():
                    key = re.sub(r"\W+", "", it["title"].lower())[:40]
                    if key not in seen:
                        seen.add(key)
                        items.append(it)
            except Exception as e:
                print("[rss]", u, repr(e)[:100])
    return items


def organize(agent, items, n, lang):
    """The LLM chooses and groups by index and rewrites in `lang`; code attaches the links. Falls back to raw items."""
    raw = [{"topic": it["source"], "title": it["title"], "summary": it["summary"][:140], "link": it["link"]}
           for it in items[:n]]
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']}. {it['summary'][:160]}" for i, it in enumerate(items))
    system = (f"You are a news editor. From the numbered items choose the {n} most important and varied stories "
              "(never two items about the same event). Group them by a short topic name (World, Politics, Economy, "
              "Tech, Science, Health, Sport, Other). For each write a clear headline of at most 12 words and one "
              f"plain sentence of context, in {LANG_NAME[lang]}. Use only facts from the item text. Never output "
              'links. Return ONLY JSON: {"items":[{"i":0,"topic":"World","title":"...","summary":"..."}]}')
    obj = _json_obj(llm_text(agent, system, listing))
    out, used = [], set()
    for e in (obj or {}).get("items", []) if isinstance(obj, dict) else []:
        try:
            i = int(e["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(items)) or i in used or not str(e.get("title", "")).strip():
            continue
        used.add(i)
        out.append({"topic": str(e.get("topic") or "World")[:24], "title": str(e["title"]).strip(),
                    "summary": str(e.get("summary") or "").strip(), "link": items[i]["link"]})
    if len(out) < min(3, len(items)):
        print("[news] LLM unavailable or unusable, using raw headlines")
        return raw
    return out[:n]


def _gemini_news(agent, n, lang):
    """Last resort (no links): Gemini + Google Search."""
    if getattr(agent, "gemini", None) is None or not agent.cloud():
        return []
    q = (f"The {n} most important world news stories of the last 24 hours. Write in {LANG_NAME[lang]}. "
         "One story per line in exactly this form: Headline | one short sentence of context. "
         "No numbering, no markdown, no introduction.")
    try:
        text = "".join(_chunk_text(c) for c in agent.gemini.stream(q, agent._chat_system(), grounding=True))
    except Exception as e:
        print("[news gemini]", repr(e)[:120])
        return []
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).replace("**", "").strip()
        if "|" in line:
            head, ctx = (x.strip() for x in line.split("|", 1))
            if head:
                out.append({"topic": "World", "title": head, "summary": ctx, "link": ""})
    return out[:n]


def get_news(agent, n=8, lang="en"):
    items = rss_items(agent)
    return organize(agent, items, n, lang) if items else _gemini_news(agent, n, lang)


def news_sections(digest):
    groups = {}
    for d in digest:
        title = esc(d["title"])
        head = (f'<a href="{_html.escape(d["link"], quote=True)}"><b>{title}</b></a>' if d["link"] else f"<b>{title}</b>")
        groups.setdefault(d["topic"], []).append(f"• {head}" + (f"\n   {esc(d['summary'])}" if d["summary"] else ""))
    return list(groups.items())


# ============================ silent tool runner =================================
_LOCK = threading.RLock()


class _Mute:
    """Stands in for the speaker: collects what Linda would say, so nothing is spoken on the Mac."""
    def __init__(self, real):
        self.real, self.lines = real, []

    def say(self, text, lang="en", **_):
        self.lines.append(str(text))

    def __getattr__(self, name):
        return getattr(self.real, name)


@contextlib.contextmanager
def muted(agent, text=""):
    with _LOCK:
        real, lang = agent.speaker, getattr(agent, "lang", "en")
        try:
            from ..brain import reply_lang
            agent.lang = reply_lang(text, agent.cfg.get("default_language", "en")) if text else agent.cfg.get("default_language", "en")
        except Exception:
            pass
        m = _Mute(real)
        agent.speaker = m
        try:
            yield m
        finally:
            agent.speaker, agent.lang = real, lang


def _do(agent, name, args, text):
    """Run one of Linda's tools the way tools.execute does (incl. run_action's bookkeeping)."""
    def invoke():
        if name in tools.SPECIAL:
            tools.SPECIAL[name](agent, args, text)
        else:
            fields = tools.coerce(args, tools.SPEC[name][4])
            getattr(agent, "do_" + name)(tools.Intent(intent=name, **fields), text)
    agent.run_action(name, args, invoke)


# ============================ morning ============================================
def _open_today(agent):
    today = str(datetime.date.today())
    return [r for r in agent.store.open_tasks() if not r[2] or str(r[2]) <= today]


def send_morning(agent, tg, n_news=8):
    t, user = S[_lang(agent)], agent.cfg.get("user_name") or ""
    sections = news_sections(get_news(agent, n_news, _lang(agent)))
    try:
        rows = _open_today(agent)[:5]
        if rows:
            sections.append((t["tasks"], [f"• {esc(r[1])}" for r in rows]))
    except Exception as e:
        print("[morning tasks]", repr(e)[:100])
    try:
        names = [b.name for b in agent.store.upcoming(366) if b.n == 0]
        if names:
            sections.append((t["bday"], [f"🎂 {esc(x)}" for x in names]))
    except Exception as e:
        print("[morning birthdays]", repr(e)[:100])
    agent.plans_until = time.time() + PLANS_WINDOW
    return tg.send_styled(t["morning"].format(user=user).strip(", "),
                          sections, footer=t["footer"], icon="☀️", intro=t["intro"])


# ============================ voice / tool handlers ==============================
def run_news(agent, a, text):
    """Voice: speaks the top headlines. 'send ... telegram': sends the full card with links. Never uses Gemini first."""
    lang = agent.lang if agent.lang in M else "en"
    m = M[lang]
    digest = get_news(agent, 8, lang)
    if not digest:
        return agent.say(m["news_fail"])
    want_tg = bool(a.get("telegram")) or bool(TG_WORDS.search(text))
    a.pop("telegram", None)                       # we send ourselves; stop execute() from also sending the spoken text
    tasks = [r[1] for r in _open_today(agent)[:8]] if PLAN_WORDS.search(text) else None
    if want_tg:
        tg = getattr(agent, "tg", None)
        if not tg:
            return agent.say(m["no_tg"])
        sections = news_sections(digest)
        if tasks:
            sections.append((S[lang]["tasks"], [f"• {esc(x)}" for x in tasks]))
        tg.send_styled(S[lang]["news"], sections, icon="📰")
        return agent.say(m["news_sent"].format(n=len(digest)))
    agent.say(". ".join(d["title"].rstrip(".") for d in digest[:3]) + ".")
    if tasks is not None:
        agent.say(m["plans_say"].format(x=", ".join(tasks)) if tasks else m["plans_none"])


def run_birthday_list(agent, a, text):
    """The FULL list straight from the database (never typed out by the model, so it cannot be cut short)."""
    lang = agent.lang if agent.lang in M else "en"
    m = M[lang]
    rows = sorted(agent.store.upcoming(366), key=lambda b: b.n)
    want_tg = bool(a.pop("telegram", None)) or bool(TG_WORDS.search(text))
    if not rows:
        return agent.say(m["bd_none"])
    nxt = rows[0]
    if not want_tg:
        return agent.say(m["bd_say"].format(n=len(rows), name=nxt.name, date=fmt_date(nxt.date, lang), d=nxt.n))
    tg = getattr(agent, "tg", None)
    if not tg:
        return agent.say(m["no_tg"])
    groups = {}
    for b in rows:
        when = m["today"] if b.n == 0 else m["tomorrow"] if b.n == 1 else m["in"].format(d=b.n)
        rel = f" ({esc(b.relation)})" if getattr(b, "relation", "") else ""
        age = f" · {m['turns'].format(a=b.age)}" if getattr(b, "age", 0) else ""
        groups.setdefault(b.date.strftime("%B %Y"), []).append(
            f"🎂 <b>{esc(b.name)}</b>{rel}\n     {b.date:%b} {b.date.day} · {when}{age}")
    tg.send_styled(f'{m["bd_title"]} ({len(rows)})', list(groups.items()), icon="🎂")
    agent.say(m["bd_sent"].format(n=len(rows)))


# ============================ plans ==============================================
def parse_plans(text):
    t = re.sub(r"^/plans?(?:@\w+)?\s*", "", text.strip(), flags=re.I)
    parts = re.split(r"[\n;]+", t)
    if len(parts) == 1 and "," in t:
        parts = t.split(",")
    items = [re.sub(r"^\s*(?:[-*•+]|\d+[.)])\s*", "", p).strip(" .") for p in parts]
    return [i for i in items if len(i) > 2][:15]


def _write_md(agent, day, items):
    p = agent.data_dir / "journal" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
    with p.open("a", encoding="utf-8") as f:
        if not old:
            f.write(f"# {day}\n\n")
        if "## Plans" not in old:
            f.write(("\n" if old and not old.endswith("\n\n") else "") + "## Plans\n")
        for it in items:
            f.write(f"- {it}\n")
    if getattr(agent, "docs", None):
        def _idx():
            try:
                agent.docs.index_file(p, "journal")
            except Exception as e:
                print("[index]", e)
        threading.Thread(target=_idx, daemon=True).start()


def save_plans(agent, items):
    """-> (added, duplicates). Open tasks due today in the db + a '## Plans' block in journal/<today>.md."""
    today, st = str(datetime.date.today()), agent.store
    have = [tools._norm(r[1]) for r in st.open_tasks()]
    added, dup = [], []
    for raw in items:
        m = TIME.search(raw)
        at = f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""
        title = raw[:120]
        n = tools._norm(title)
        if any(n == o or difflib.SequenceMatcher(None, o, n).ratio() > 0.85 for o in have):
            dup.append(title)
            continue
        st.add_task(title, today, project="")
        have.append(n)
        if at:
            con = tools._tt_db(agent.cfg)
            try:
                con.execute("insert or replace into task_times values (?,?)", (n, at))
                con.commit()
            finally:
                con.close()
        added.append(title)
    if added:
        _write_md(agent, today, added)
    return added, dup


def _plans_reply(agent, tg, items):
    t = S[_lang(agent)]
    added, dup = save_plans(agent, items)
    agent.plans_until = time.time() + PLANS_WINDOW
    sections = []
    if added:
        sections.append(("", [f"✅ {esc(x)}" for x in added]))
    if dup:
        sections.append((t["dup"], [f"• {esc(x)}" for x in dup]))
    return tg.send_styled(t["plans_saved"], sections, footer=t["done_footer"], icon="🗓")


# ============================ filing free text ===================================
FILE = {   # type -> (tool, icon, label, args builder)
    "idea": ("save_note", "💡", "Ideas", lambda i: {"file": "ideas", "text": i["text"]}),
    "goal": ("save_note", "🎯", "Goals", lambda i: {"file": "goals", "text": i["text"]}),
    "profile": ("save_note", "👤", "Profile", lambda i: {"file": "profile", "text": i["text"]}),
    "task": ("task_add", "✅", "Tasks", lambda i: {"title": i["text"], "due": i["due"], "time": i["time"],
                                                     "project": i["project"]}),
    "journal": ("journal_add", "📓", "Journal", lambda i: {"text": i["text"]}),
    "progress": ("log_progress", "🛠", "Progress log", lambda i: {"project": i["project"] or "General",
                                                                  "text": i["text"]}),
    "note": ("remember", "📝", "Quick notes", lambda i: {"text": i["text"]}),
}
CLASSIFY = """You file messages that {user} sends to his personal assistant Linda. Today is {today}.
Split the message into separate items when it holds several things. Give each item a type:
- idea: a thought, project or product idea to keep
- goal: a target or ambition (yearly, monthly, weekly) or something to achieve
- profile: a lasting fact about {user} (languages, schedule, health, preferences, people, instruments)
- task: something NEW to do (optionally with a day and time)
- done: {user} says one or more tasks that are already in OPEN TASKS are finished ("all done", "I finished the exam
  prep", "everything except X"). Set "tasks" to the NUMBERS from the OPEN TASKS list below. Never invent numbers.
- journal: feelings, thoughts or reflections about the day (diary)
- progress: what {user} worked on, finished or improved (a project, studies, or a feature of Linda). Set "project";
  use "Linda" for anything about the assistant itself: new features, bugs, wishes for her
- note: a small one-off fact or reminder that fits nowhere else
- none: a question, greeting or chat that must not be saved
Rules: saying that tasks are finished is ONLY a "done" item, never also progress or journal. A task that is already
in OPEN TASKS is never a new "task": "X is still open" or "only X is left" means X is not done and not new, so leave it out.
Write every "text" as ONE clear standalone sentence in the user's own language (replace 'it' and 'that' with what
they refer to). For tasks set "due" (YYYY-MM-DD) only if a day is stated or implied (today, tomorrow, Friday) and
"time" (HH:MM) only if stated; otherwise leave them empty.{hint}
OPEN TASKS:
{tasks}
Return ONLY JSON: {{"items":[{{"type":"idea","text":"...","due":"","time":"","project":"","tasks":[]}}],"reply":"one short friendly line, only used when nothing is saved"}}"""


def _open_titles(agent, limit=30):
    try:
        return [r[1] for r in agent.store.open_tasks()[:limit]]
    except Exception as e:
        print("[telegram open tasks]", repr(e)[:100])
        return []


def classify(agent, text, plans_mode):
    """-> (items, reply) or (None, '') when no LLM answered."""
    hint = ("\nThe user was just asked for today's plans, so unless a message is clearly something else, "
            "treat its items as tasks." if plans_mode else "")
    titles = _open_titles(agent)
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1)) or "(none)"
    system = CLASSIFY.format(user=agent.cfg.get("user_name") or "the user", today=datetime.date.today().isoformat(),
                             hint=hint, tasks=listing)
    obj = _json_obj(llm_text(agent, system, text))
    if not isinstance(obj, dict):
        return None, ""
    items = []
    for e in obj.get("items") or []:
        if not isinstance(e, dict):
            continue
        if e.get("type") == "done":                           # the model gives NUMBERS; the titles come from the db
            picked, seen = [], set()
            for n in e.get("tasks") or []:
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(titles) and n not in seen:
                    seen.add(n)
                    picked.append(titles[n - 1])
            if picked:
                items.append({"type": "done", "text": ", ".join(picked)[:300], "titles": picked,
                              "due": "", "time": "", "project": ""})
            continue
        if e.get("type") not in FILE:
            continue
        body = str(e.get("text") or "").strip()[:400]
        if not body:
            continue
        due = str(e.get("due") or "").strip()
        try:
            datetime.date.fromisoformat(due)
        except ValueError:
            due = ""
        tm = str(e.get("time") or "").strip()
        tm = tm if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tm) else ""
        items.append({"type": e["type"], "text": body, "due": due, "time": tm,
                      "project": str(e.get("project") or "").strip()[:40]})
    return items, str(obj.get("reply") or "").strip()


def _done_tool():
    for n in ("task_done", "task_complete", "complete_task", "done_task", "finish_task"):
        if n in tools.SPEC:
            return n
    return next((n for n in tools.SPEC if "done" in n or "complete" in n), None)


def _complete_tasks(agent, titles, original):
    """Close tasks with Linda's own tool, then re-read the list. -> HTML lines for the reply card."""
    agent.done_files = {}
    tool = _done_tool()
    if not tool:
        return ["⚠️ I can't find the 'task done' tool in tools.py, so nothing was changed."]
    props = tools.SPEC[tool][2] if len(tools.SPEC[tool]) > 2 and isinstance(tools.SPEC[tool][2], dict) else {}
    key = "title" if (not props or "title" in props) else next(iter(props))
    for t in titles:
        try:
            _do(agent, tool, {key: t}, original)
        except Exception as e:
            print("[telegram done]", t, repr(e)[:120])
    still = {tools._norm(r[1]) for r in agent.store.open_tasks()}
    return [f"☑️ <b>Done</b>: {esc(t)}" + done_files(agent, t) if tools._norm(t) not in still
            else f"⚠️ <b>Could not close</b>: {esc(t)}" for t in titles]


def done_files(agent, title):
    """'\n   📁 data/tasks.md · data/projects/linda.md' for a task closed in this message ('' if unknown)."""
    files = (getattr(agent, "done_files", None) or {}).get(tools._norm(title))
    if not files:
        done = getattr(agent, "done_files", None) or {}
        key = next((k for k in done if k.startswith(tools._norm(title)[:40])), None)   # title was trimmed on save
        files = done.get(key) if key else None
    if not files:
        return ""
    return "\n   📁 " + " · ".join(f"<code>{esc(reference.rel(agent, p))}</code>" for p in files)


def _save_items(agent, tg, items, original):
    t = S[_lang(agent)]
    lines = []
    with muted(agent, original) as mute:
        for it in items:
            if it["type"] == "done":
                lines += _complete_tasks(agent, it["titles"], original)
                continue
            tool, icon, label, build = FILE[it["type"]]
            before = len(mute.lines)
            try:
                _do(agent, tool, build(it), original)
                note = " ".join(mute.lines[before:])[:90]
            except Exception as e:
                print(f"[telegram save {tool}]", repr(e)[:150])
                note = "failed"
            lines.append(f"{icon} <b>{label}</b>: {esc(it['text'])}" + (f"\n   <i>{esc(note)}</i>" if note else ""))
    return tg.send_styled(t["saved"], [("", lines)], icon="✅")


def _done_command(agent, tg, rest):
    """/done exam  ->  closes the one open task that matches (no LLM involved)."""
    rows = [r[1] for r in agent.store.open_tasks()]
    q = tools._norm(rest)
    hits = [x for x in rows if q and q in tools._norm(x)]
    if not hits:
        close = difflib.get_close_matches(q, [tools._norm(x) for x in rows], n=1, cutoff=0.6)
        hits = [x for x in rows if close and tools._norm(x) == close[0]]
    if len(hits) != 1:
        msg = ("Which one? " + "; ".join(hits[:5])) if hits else "I couldn't find that task. /tasks shows your list."
        return tg.send_styled("Linda", icon="💬", intro=msg)
    with muted(agent, rest):
        lines = _complete_tasks(agent, hits, rest)
    return tg.send_styled("Done", [("", lines)], icon="✅")


# ============================ answering questions =================================
# A question or chat is not filed: it is answered from the user's own saved data (same retrieval as voice mode).
QUESTION = re.compile(r"\?\s*$|^\s*(?:tell me|can you|can u|could you|could u|write (?:me|a|the)|скажи|напиши|покажи|"
                      r"можешь|ayt|yozib ber)\b", re.I)
BDAY_WORDS = re.compile(r"birthday|wish|greeting|congrat|поздрав|день рожд|tug'?ilgan|tabrik", re.I)
GREET_WORDS = re.compile(r"\bwish|greeting|congrat|поздрав|tabrik|\bmessage\b|\bletter\b", re.I)
ANSWER = """You are Linda, {user}'s personal assistant, chatting with him in Telegram. Today is {today}.
The DATA block is {user}'s own saved information (goals, ideas, profile, tasks, birthdays, notes, progress, journal).
Rules:
- Answer from DATA and RECENT CHAT. If the answer is not in DATA, say plainly that you do not have it saved. Never invent dates, names or facts.
- Birthdays in DATA are sorted by date: the first one is the NEXT birthday. "in_days" counts days from today.
- If asked to write a wish, greeting or message, write it IN FULL (warm, natural, personal, 3 to 5 sentences) for the right person, using the relation and "greeting_lang" from DATA (en, ru or uz). If no person is named, use the next birthday. Output only the message, ready to forward.
- Otherwise answer briefly (1 to 4 sentences) in the language of {user}'s message. No markdown, no pep-talk closers such as "good luck".
- A task due date is a deadline, not a scheduled appointment."""


def _hist(agent):
    h = getattr(agent, "tg_hist", None)
    if h is None:
        h = agent.tg_hist = []
    return h


def _context(agent, text):
    """Saved data relevant to the message, as JSON text from the same retrieval the voice mode uses."""
    parts = []

    def add(label, fn):
        try:
            v = fn()
        except Exception as e:
            print(f"[telegram context {label}]", repr(e)[:120])
            return
        if v:
            parts.append(f"{label}: {v}")
    add("matches", lambda: tools.prefetch(agent, text, budget=1500))                       # words in the message
    add("overview", lambda: tools.prefetch(agent, text, budget=2400, think=True))          # goals, ideas, tasks, profile...
    if BDAY_WORDS.search(text):
        add("birthdays", lambda: tools.recall_data(agent, {"kind": "birthdays", "query": text}, text, budget=2000))
    return "\n".join(parts)


def _answer(agent, tg, text, fallback=""):
    t = S[_lang(agent)]
    user = agent.cfg.get("user_name") or "the user"
    hist = _hist(agent)
    chat = "\n".join(f"{who}: {m}" for who, m in hist[-6:])
    prompt = (f"DATA:\n{_context(agent, text) or '(nothing relevant found)'}\n\n"
              + (f"RECENT CHAT:\n{chat}\n\n" if chat else "") + f"MESSAGE from {user}:\n{text}")
    system = ANSWER.format(user=user, today=datetime.date.today().strftime("%A %Y-%m-%d"))
    ans = llm_text(agent, system, prompt, temp=0.4)
    ans = re.sub(r"\*\*|`|^#+\s*", "", ans or "", flags=re.M).strip()
    if not ans:
        return tg.send_styled(t["hint_title"], icon="💬", intro=fallback or t["no_ai"])
    hist += [("User", text[:300]), ("Linda", ans[:300])]
    del hist[:-8]
    if GREET_WORDS.search(text):
        return tg.send(ans)                                  # plain text: easy to forward
    return tg.send_styled(t["hint_title"], icon="💬", intro=ans)




TG_RULES = (
    "Channel: a Telegram text chat, not voice. Handle EVERYTHING the message contains with tools in the SAME turn "
    "(several tool calls at once are fine). Mapping: new to-do -> task_add; finished tasks -> task_done, ONE call per "
    "finished task, with ids from the open tasks list; 'all tasks are done except X' = task_done for every open task "
    "except X; ideas -> save_note file=ideas; goals -> save_note file=goals; lasting facts about the user -> "
    "save_note file=profile; feelings or diary -> journal_add; work already finished that matches no open task -> "
    "log_progress; the user's AGENCY CLIENTS (people going to Germany) -> client_add for a new client or pasted client "
    "details (also partial ones, e.g. only an email and password: save them, never ask for more), client_update for "
    "steps/fields of an existing client, client_info / client_list for questions about them, never task_add, save_note "
    "or remember for them. Questions about the user's OWN BOTS (Simple Quizzer, Simple Slides: users, new users, "
    "active users, quizzes, presentations, files, payments, any period) -> bot_stats with the question in full; never "
    "answer bot numbers yourself. OPPORTUNITIES (scholarships, grants, exchange programmes, competitions, calls for "
    "applications): save_reference with topic 'Scholarships' (one shared file; use 'Opportunities' for things that are "
    "not scholarships) and the full text; if the user wants to apply or take part, ALSO task_add titled "
    "'Apply: <programme name> (deadline <date and time>)' with due = the application deadline as YYYY-MM-DD. "
    "Never answer 'all set' before the thing itself has been saved. Never task_add something already open, and never also log_progress or journal a task you just "
    "closed. A question or chat is answered from stored data and nothing is saved. After SAVING, reply with ONE "
    "short friendly line. A QUESTION gets a complete answer: every matching person or item, with the details asked for "
    "(dates, relations), one per line if there are several. Wishes, greetings and messages are written in full."
        " Tasks the user says they are 'still working on', 'still practicing' or 'still open' must stay open: pass them "
    "in keep_ids. If the user pastes a list of tasks and says done, close every task in that list with ids='...'. "
    "Do not reply until every task the user mentioned is handled."
        " One task_add call per separate to-do. If a message lists several things (new lines, 'and also', 'and', "
    "different subjects such as app stats vs finance stats), make a separate task_add for each, never one combined title. "
    " A follow-up starting with 'and also' is a NEW separate to-do: make its own task_add and never repeat a title "
    "that is already open. Finance to-dos (stats, revenue reports) are normal tasks. "
    "A message saying work was done (studied, improved, implemented, finished) must ALWAYS end in a tool call: "
    "task_done if it matches an open task, otherwise log_progress. Never reply with praise alone. "
    "Write the log text as what was done, e.g. 'Studied tickets 55, 56 and 57 for the driver's licence exam.' "
    "Work that belongs to no ongoing project goes to project 'General'. "
    "Never state finance numbers: finance reports are handled by code behind a PIN."
        "Any question about the user's money (revenue, income, expenses, spending, balance, purchases, transactions, "
    "per month or category) -> call finance_report with the user's question in full. Never answer it yourself, "
    "never state finance numbers, and never save such a question as a task or note. "
    
    + reference.TG_RULES_EXTRA
    
    )



DONE_WORK = re.compile(r"\b(studied|improved|finished|completed|implemented|built|fixed|worked on|practiced|"
                       r"learned|prepared|wrote|did)\b", re.I)
PLANNING = re.compile(r"\b(will|plan|planning|tomorrow|need to|going to|want to|have to)\b", re.I)
NUDGE = (" You answered without recording anything. This message reports finished work: call task_done if it matches "
         "an open task, otherwise call log_progress (no_matching_task=true, project 'General' if no project fits).")

PREP_DONE = re.compile(r"\b(?:ready|prepared|finished|done)\b", re.I)
NUDGE_PREP = (" The message also says preparation is already finished. Call log_progress for that (project it belongs to, "
              "no_matching_task=true). Do not call task_add again.")

LOG_ICONS = {"log_progress": ("🛠", "Progress"), "save_note": ("📌", "Saved"), "journal_add": ("📓", "Journal"),
             "remember": ("📝", "Note"), "practice_log": ("🎹", "Practice"), "song_add": ("🎵", "Song"),
             "save_reference": ("📚", "Reference")}


def _tool_lines(brain, agent=None):
    out = []
    for name, args, res in brain.tool_log:
        if name in LOG_ICONS and not res.lower().startswith("error"):
            icon, label = LOG_ICONS[name]
            proj = f" ({esc(args['project'])})" if name == "log_progress" and args.get("project") else ""
            if name == "save_reference":
                shown = f"{args.get('topic') or 'notes'}: {len(str(args.get('text') or ''))} characters saved in full"
            else:
                shown = str(args.get("text") or args.get("name") or res)[:300]
            line = f"{icon} <b>{label}</b>{proj}: {esc(shown)}"
            where = reference.where_saved(agent, name, args)
            if where:
                line += f"\n   📁 <code>{esc(where)}</code>"
            out.append(line)
    return out

PLAN_HINT = ("The user was just asked for today's plans: treat what they list as new tasks due today (task_add), "
             "unless a line clearly is something else.")


def _converse(agent, tg, text, in_window):
    """One LLM agent loop (same as voice mode: recall + tools + answer). Returns False if no provider answered."""
    brain = getattr(agent, "brain", None)
    if brain is None or not getattr(brain, "providers", None):
        return False
    before = [r[1] for r in agent.store.open_tasks()]
    agent.card_sent = False                                 # set by tools that send their own card (bot stats, clients)
    agent.done_files = {}                                   # filled by tools.finish_task
    brain.tool_log = []                                      # NEW
    try:
        with muted(agent, text) as mute:
            agent._produced = False
            if agent.pending_actions and agent.handle_confirmation(text):
                ok = True
            else:
                brain.cancel = threading.Event()
                brain.extra_rules = TG_RULES + (" " + PLAN_HINT if in_window else "")
                brain.read_only = bool(reference.MEM_ASK.search(text))       # a question can never trigger a save
                try:
                    ok = brain._agent_loop(text)
                    acted = any(n != "recall" for n, _, _ in brain.tool_log)
                    if ok and DONE_WORK.search(text) and not PLANNING.search(text) and not acted:   # NEW
                        mute.lines.clear()
                        brain.extra_rules += NUDGE
                        ok = brain._agent_loop(text)
                    names = {n for n, _, _ in brain.tool_log}
                    if ok and "task_add" in names and PREP_DONE.search(text) and not names & {"log_progress", "task_done"}:
                        mute.lines.clear()
                        brain.extra_rules += NUDGE_PREP
                        ok = brain._agent_loop(text)                    
                    
                    
                    elif ok and not acted and reference.looks_like_reference(text):
                        mute.lines.clear()                                       # long dictated info, nothing saved yet
                        brain.extra_rules += reference.NUDGE_REF
                        ok = brain._agent_loop(text)
                finally:
                    brain.extra_rules = ""
                    brain.read_only = False
    except Exception as e:
        print("[telegram brain]", repr(e)[:200])
        return False
    if not ok:
        return False
    reply = " ".join(mute.lines).strip() or "Done."
    after = [r[1] for r in agent.store.open_tasks()]
    b, a = {tools._norm(x) for x in before}, {tools._norm(x) for x in after}
    # what really changed, read back from the database (not what the model claims)
    changes = [f"☑️ <b>Closed</b>: {esc(x)}" + done_files(agent, x) for x in before if tools._norm(x) not in a]
    changes += [f"➕ <b>Added</b>: {esc(x)}" for x in after if tools._norm(x) not in b]
    changes += _tool_lines(brain, agent)
    brain.history = (brain.history + [("user", text), ("model", reply)])[-2 * brain.cfg["brain"]["history_turns"]:]
    if getattr(agent, "card_sent", False) and not changes:
        return True                                          # the card IS the answer; skip "I'll check that for you."
    if GREET_WORDS.search(text):
        tg.send(reply)                                       # plain text: easy to forward
    else:
        tg.send_styled(S[_lang(agent)]["hint_title"], [("", changes)] if changes else [],
                       icon="💬", intro=reply)
    return True


def _free_text(agent, tg, text):
    t = S[_lang(agent)]
    in_window = time.time() < getattr(agent, "plans_until", 0)
    tg.typing()
    if NEWS_ASK.search(text) and len(text.split()) <= 14:
        return _news(agent, tg)
    if in_window:
        agent.plans_until = time.time() + PLANS_WINDOW
    if _converse(agent, tg, text, in_window):
        return
    # ---- fallback only: every provider is down -> the old classifier ----
    if reference.looks_like_reference(text):          # providers down: never let the classifier cut it to 400 chars
        return _save_reference_direct(agent, tg, text)
    if "\n" not in text and QUESTION.search(text):
        return _answer(agent, tg, text)
    items, reply = classify(agent, text, in_window)
    if items is None:
        if in_window and parse_plans(text):
            return _plans_reply(agent, tg, parse_plans(text))
        return tg.send_styled(t["hint_title"], icon="💬", intro=t["no_ai"])
    if in_window:
        for it in items:
            if it["type"] == "task" and not it["due"]:
                it["due"] = str(datetime.date.today())
    if not items:
        return _answer(agent, tg, text, fallback=reply or t["nothing"])
    return _save_items(agent, tg, items, text)

# ============================ commands ===========================================
def _due_label(d, today):
    if not d:
        return ""
    d = str(d)
    return ("today" if d == str(today) else "tomorrow" if d == str(today + datetime.timedelta(days=1))
            else f"⚠️ {d}" if d < str(today) else d)


def _welcome(agent, tg):
    t, user = S[_lang(agent)], agent.cfg.get("user_name") or ""
    return tg.send_styled(t["hello"].format(user=user).strip(", "),
                          [(t["can"], [esc(x) for x in t["can_lines"]]), (t["just"], [esc(t["just_line"])])],
                          footer=t["start_footer"], icon="👋", intro=t["welcome"])


def _tasks(agent, tg):
    t, today = S[_lang(agent)], datetime.date.today()
    rows = agent.store.open_tasks()
    if not rows:
        return tg.send_styled("Tasks", icon="✅", intro=t["no_tasks"])
    tt = tools.task_times(agent.cfg)
    lines = []
    for r in rows[:30]:
        when = " ".join(x for x in (_due_label(r[2], today), tt.get(tools._norm(r[1]), "")) if x)
        lines.append(f"• {esc(r[1])}" + (f" <i>({esc(when)})</i>" if when else ""))
    return tg.send_styled(f"Tasks ({len(rows)})", [("", lines)], icon="✅")


def _news(agent, tg):
    t, lang = S[_lang(agent)], _lang(agent)
    tg.typing()
    digest = get_news(agent, 8, lang)
    if not digest:
        return tg.send_styled(t["news"], icon="📰", intro=t["news_fail"])
    return tg.send_styled(t["news"], news_sections(digest), icon="📰")


def _today(agent, tg, limit=15):
    """/today: news + open tasks due today (or overdue/undated, same as the morning card) + today's birthdays.
    Each part fails on its own, so a dead news feed still leaves the tasks and birthdays."""
    lang = _lang(agent)
    t, today = S[lang], datetime.date.today()
    tg.typing()
    sections = []
    try:
        digest = get_news(agent, 6, lang)
    except Exception as e:
        print("[today news]", repr(e)[:100])
        digest = []
    sections += news_sections(digest) if digest else [(t["news"], [esc(t["news_fail"])])]
    try:
        rows = _open_today(agent)
        tt = tools.task_times(agent.cfg)
        lines = []
        for r in rows[:limit]:
            when = " ".join(x for x in (_due_label(r[2], today), tt.get(tools._norm(r[1]), "")) if x)
            lines.append(f"• {esc(r[1])}" + (f" <i>({esc(when)})</i>" if when else ""))
        if len(rows) > limit:
            lines.append(f"<i>{esc(t['more'].format(n=len(rows) - limit))}</i>")
        sections.append((f"{t['today_tasks']} ({len(rows)})" if rows else t["today_tasks"],
                         lines or [esc(t["no_today_tasks"])]))
    except Exception as e:
        print("[today tasks]", repr(e)[:100])
    try:
        bdays = [b for b in agent.store.upcoming(366) if b.n == 0]
        lines = []
        for b in bdays:
            rel = f" ({esc(b.relation)})" if getattr(b, "relation", "") else ""
            age = f" · {esc(M[lang]['turns'].format(a=b.age))}" if getattr(b, "age", 0) else ""
            lines.append(f"🎂 <b>{esc(b.name)}</b>{rel}{age}")
        sections.append((t["bday"], lines or [esc(t["no_bday"])]))
    except Exception as e:
        print("[today birthdays]", repr(e)[:100])
    return tg.send_styled(f"{t['today']} · {fmt_today(today, lang)}", sections, icon="📅")


def _pieces(text, n=3500):
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > n:
            out.append(cur)
            cur = ""
        while len(line) > n:                          # one huge line
            out.append(line[:n])
            line = line[n:]
        cur += line
    return out + ([cur] if cur else [])


def _notes_command(agent, tg, rest):
    """/notes: every quick note in full + the reference files. /notes <name>: that file in full."""
    if rest:
        p = reference.find(agent, rest)
        if p is None:
            have = reference.names(agent) or "none yet"
            return tg.send_styled("Notes", icon="📚", intro=f"No saved note matches '{rest}'. Saved: {have}")
        body = p.read_text(encoding="utf-8", errors="ignore").strip()
        head = f"<b>📚 {esc(reference.rel(agent, p))}</b>\n\n"
        for i, chunk in enumerate(_pieces(body)):
            tg.send_html((head if i == 0 else "") + esc(chunk))
        return
    try:
        notes = [str(n) for n in (agent.store.notes(200) or [])]
    except Exception as e:
        print("[notes]", repr(e)[:100])
        notes = []
    sections = []
    for i in range(0, len(notes), 12):
        sections.append((f"Quick notes ({len(notes)}) · saved in linda.db" if i == 0 else "",
                         [f"• {esc(n)}" for n in notes[i:i + 12]]))
    files = reference.listing(agent)
    if files:
        sections.append(("Reference files (/notes <name> shows one in full)",
                         [f"📚 <code>{esc(r)}</code> · {n} characters" for r, n in files]))
    if not sections:
        return tg.send_styled("Notes", icon="🧠", intro="Nothing saved yet.")
    return tg.send_styled("What I remember", sections, icon="🧠")


DIRECT = {"idea": "idea", "goal": "goal", "profile": "profile", "task": "task", "journal": "journal", "note": "note"}


def _command(agent, tg, cmd, rest):
    t = S[_lang(agent)]
    if cmd in ("start", "help"):
        return _welcome(agent, tg)
    if cmd == "today":
        return _today(agent, tg)
    if cmd == "stats":
        from . import bot_stats, clients
        q = rest or "new users and active users today, yesterday and this month"
        return clients._run(agent, tg, bot_stats.bot_stats, {"question": q}, q)
    if cmd in ("client", "clients", "client_undo"):
        from . import clients
        return clients.telegram_command(agent, tg, cmd, rest)
    if cmd == "news":
        return _news(agent, tg)
    if cmd == "tasks":
        return _tasks(agent, tg)
    if cmd == "birthdays":
        with muted(agent):
            return run_birthday_list(agent, {"telegram": True}, "send")
    if cmd in ("plan", "plans"):
        items = parse_plans(rest)
        return _plans_reply(agent, tg, items) if items else \
            tg.send_styled(t["hint_title"], icon="🗓", intro=t["usage"].format(ex="/plans gym; read 20 pages"))
    if cmd in ("notes", "memory"):
        return _notes_command(agent, tg, rest)
    if cmd == "done":
        return _done_command(agent, tg, rest) if rest else \
            tg.send_styled(t["hint_title"], icon="💬", intro="Write it after the command, like: /done exam")
    if cmd in DIRECT:
        if not rest:
            return tg.send_styled(t["hint_title"], icon="💬", intro=t["usage"].format(ex=f"/{cmd} …"))
        item = {"type": DIRECT[cmd], "text": rest[:400], "due": "", "time": "", "project": ""}
        if item["type"] == "task":
            item["due"] = str(datetime.date.today()) if re.search(r"\btoday\b", rest, re.I) else ""
        return _save_items(agent, tg, [item], rest)
    return tg.send_styled(t["hint_title"], icon="💬", intro=t["unknown"])

import hmac

from .finance import fmt_amount

FIN_WORD = re.compile(
    r"\b(revenue|income|expenses?|expences?|spent|spending|earned|profit|finances?|purchases?|bought|"
    r"transactions?|payments?|balance|summary)\b|доход|расход|выручк|прибыл|покупк|трат|"
    r"daromad|xarajat|xarid", re.I)
FIN_CMDS = ("/summary", "summary", "/balance", "balance", "/finance")
LAST_MONTH = re.compile(r"last month|previous month|прошл\w+ месяц|o'?tgan oy", re.I)
RECENT = re.compile(r"\b(latest|last|recent|newest)\b|последн|oxirgi", re.I)
REPORT_Q = re.compile(r"revenue|income|expenses?|expences?|summary|balance|report|profit|finances?|"
                      r"доход|расход|daromad|xarajat", re.I)
INCOME_Q = re.compile(r"income|revenue|earn|доход|daromad", re.I)
MY_WORDS = re.compile(r"my[\s_-]?finance|personal|private|личн|shaxsiy", re.I)
CO_WORDS = re.compile(r"company|business|\bbiz\b|kompaniya|компани", re.I)
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
SHEETS = {"my_finance": ("👤", "Personal"), "company": ("🏢", "Company")}
FIN_SESSION, FIN_LOCK = 300, 900


def _fin_month(t):
    m = re.search(r"\b(\d{4}-\d{2})\b", t)
    if m:
        return m.group(1)
    today = datetime.date.today()
    if LAST_MONTH.search(t):
        return (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    low = t.lower()
    for i, name in enumerate(MONTHS_EN, 1):
        if re.search(rf"\b{name.lower()}\b", low):
            return f"{today.year - (i > today.month)}-{i:02d}"
    return None                                     # None = this month


def _month_label(m):
    y, mo = map(int, (m or datetime.date.today().strftime("%Y-%m")).split("-"))
    return f"{MONTHS_EN[mo - 1]} {y}"


def _fin_sheets(q):
    my, co = bool(MY_WORDS.search(q)), bool(CO_WORDS.search(q))
    return ["my_finance"] if my and not co else ["company"] if co and not my else ["my_finance", "company"]


def _money(n, sign=False):
    s = fmt_amount(abs(int(n)))
    return ("−" if n < 0 else "+" if sign and n > 0 else "") + s


def _bar(share, n=10):
    k = max(0, min(n, round(share * n)))
    return "▰" * k + "▱" * (n - k)


def _summary_card(tg, svc, sheet, s, month):
    icon, name = SHEETS[sheet]
    title = f"{name} finance · {_month_label(month)}"
    d = s["sheets"].get(sheet)
    if not d:
        return tg.send_styled(title, icon=icon, intro="No entries for this month yet.")
    inc, exp, bal = d["income"], d["expense"], d["balance"]
    sections = [("", [f"🟢 Income: <b>{_money(inc)}</b>", f"🔴 Expenses: <b>{_money(exp)}</b>",
                      f"{'💰' if bal >= 0 else '⚠️'} Balance: <b>{_money(bal, sign=True)}</b>"])]
    if d["top"]:
        lines = []
        for i, (n, t) in enumerate(d["top"], 1):
            share = t / exp if exp else 0
            lines.append(f"{i}. {esc(n)}\n    <b>{_money(t)}</b> · {_bar(share)} {share:.0%}")
        sections.append(("Biggest expenses", lines))
    return tg.send_styled(title, sections, footer="UZS · only entries made through Linda", icon=icon)


def _recent_card(tg, svc, sheet, tx_type, n=5):
    icon, name = SHEETS[sheet]
    rows = svc.recent(sheet, tx_type, n)
    kind = "income" if tx_type == "income" else "expenses"
    title = f"{name} · latest {len(rows) if rows else n} {kind}"
    if not rows:
        return tg.send_styled(title, icon=icon, intro="Nothing recorded yet.")
    sign = "🟢 +" if tx_type == "income" else "🔴 −"
    lines = []
    for r in rows:
        d = datetime.date.fromisoformat(r["tx_date"])
        lines.append(f"{sign}{fmt_amount(r['amount'])}  <b>{esc(r['description'])}</b>\n    {d:%d.%m.%Y}")
    return tg.send_styled(title, [("", lines)], footer="UZS", icon=icon)

FIN_PLAN = """Classify one message sent to a personal assistant. Today is {today}.
Return ONLY JSON with these keys: finance, sheet, view, type, month, limit. Fill each from THE MESSAGE, not from habit.
- finance: true ONLY if the user asks to READ money data (totals, revenue, expenses, balance, latest purchases).
  Reporting finished work, tasks, or to-dos is false even if the word "finance" appears.
- sheet: "my_finance" ONLY if the message says personal/private/my own; "company" ONLY if it says company/business;
  otherwise "both".
- view: "recent" = a list of individual entries (latest/last N purchases, expenses, payments, income);
  "summary" = totals of a month; "balance" = how much is left overall; "custom" = any other money question
  (biggest purchase, spending on one thing, a date range, a comparison).
- type: "income" or "expense" (purchases, spending, payments = expense).
- month: "YYYY-MM" only for view=summary when a month is named; otherwise null.
- limit: how many entries for view=recent (the number the user asked for, default 5, at most 50).
Examples:
"latest 10 purchases" -> {{"finance": true, "sheet": "both", "view": "recent", "type": "expense", "month": null, "limit": 10}}
"company revenue in October" -> {{"finance": true, "sheet": "company", "view": "summary", "type": "income", "month": "YYYY-10", "limit": 5}}
"how much did I spend on taxi this month" -> {{"finance": true, "sheet": "both", "view": "custom", "type": "expense", "month": null, "limit": 5}}"""

COUNT = re.compile(r"\b(\d{1,2})\b")
TOTAL_Q = re.compile(r"summary|total|overall|итог|всего|jami", re.I)


def _fin_limit(text, value=None):
    """The number of entries asked for ('latest 10 purchases' -> 10), 5 if none, never above 50."""
    for v in (value, *(m.group(1) for m in COUNT.finditer(text))):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 50:
            return n
    return 5


def _fin_plan(agent, text):
    """LLM understands the question; if every LLM is down, the old regexes decide."""
    raw = llm_text(agent, FIN_PLAN.format(today=datetime.date.today()), text, temp=0)
    obj = _json_obj(raw)
    if isinstance(obj, dict) and "finance" in obj:
        sheet = obj.get("sheet")
        view = obj.get("view")
        month = obj.get("month")
        if not (isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month)):
            month = None
        view = view if view in ("summary", "recent", "balance", "custom") else "summary"
        if (view == "summary" and RECENT.search(LAST_MONTH.sub("", text)) and not LEFT_Q.search(text)
                and not TOTAL_Q.search(text)):
            view = "recent"                   # "latest/last N ..." is a list of entries, whatever the model said
        return {"finance": bool(obj["finance"]),
                "sheets": [sheet] if sheet in SHEETS else ["my_finance", "company"],
                "view": view,
                "type": "income" if obj.get("type") == "income" else "expense",
                "month": month, "limit": _fin_limit(text, obj.get("limit"))}
    # fallback: the regex logic
    view = ("balance" if LEFT_Q.search(text) else "recent" if RECENT.search(LAST_MONTH.sub("", text))
            else "summary" if REPORT_Q.search(text) else "custom")
    return {"finance": not TASKISH.search(text), "sheets": _fin_sheets(text), "view": view,
            "type": "income" if INCOME_Q.search(text) else "expense", "month": _fin_month(text),
            "limit": _fin_limit(text)}

def _fin_answer(agent, tg, query):
    svc = getattr(agent, "finance", None)
    if svc is None:
        return tg.send_styled("Finance", icon="💰", intro="Finance is not set up.")
    p = _fin_plan(agent, query)
    print(f"[finance] plan={p}")
    try:
        if p["view"] == "balance":
            for sh in p["sheets"]:
                _balance_card(tg, svc, sh)
        elif p["view"] == "recent":
            for sh in p["sheets"]:
                _recent_card(tg, svc, sh, p["type"], p.get("limit", 5))
        elif p["view"] == "summary":
            s = svc.summary(p["month"])
            for sh in p["sheets"]:
                _summary_card(tg, svc, sh, s, p["month"])
        else:
            da = getattr(agent, "data_ask", None)
            if da is None:
                return tg.send_styled("Finance", icon="💰", intro="I can show reports, balances and latest entries.")
            for sh in p["sheets"]:
                icon, name = SHEETS[sh]
                ans = da.ask(f"{query} (use only rows where sheet = '{sh}')", "finance")
                tg.send_styled(f"{name} finance", icon=icon, intro=ans)
    except FinanceError as e:
        tg.send_styled("Finance", icon="💰", intro=str(e))
    except Exception as e:
        print("[finance ask]", repr(e)[:150])
        tg.send_styled("Finance", icon="💰", intro=f"I couldn't read the finance data: {e}")

def _fin_pin(agent):
    return str(agent.cfg.get("finance_pin") or os.getenv("LINDA_FINANCE_PIN") or "")


def finance_request(agent, tg, question):
    """The ONLY door to finance reads. Pure code: PIN, lockout, session."""
    now, pin = time.time(), _fin_pin(agent)
    if not pin:
        tg.send_styled("Finance", icon="🔒", intro="Set finance_pin in config.yaml (or LINDA_FINANCE_PIN) first.")
    elif now < getattr(agent, "_fin_lock", 0):
        tg.send_styled("Finance", icon="🔒", intro="Locked for a few minutes after wrong PINs.")
    elif now < getattr(agent, "_fin_until", 0):
        _fin_answer(agent, tg, question)
    else:
        agent._fin_pending = (question, now + 120)
        tg.send_styled("Finance", icon="🔒", intro="Send your finance PIN to see this.")

def finance_gate(agent, tg, t):
    """True when the message was handled here. Guards every finance READ (questions and /summary)."""
    now, pin = time.time(), _fin_pin(agent)
    fc = getattr(agent, "finance_chat", None)
    low = t.lower()
    pend = getattr(agent, "_fin_pending", None)
    if pend and now < pend[1] and re.fullmatch(r"\S{3,20}", t) and not (fc and fc.matches(t)):
        agent._fin_pending = None
        tg.delete_later(getattr(tg, "last_message_id", None))      # the PIN must not stay in the chat
        if now < getattr(agent, "_fin_lock", 0):
            tg.send_styled("Finance", icon="🔒", intro="Locked for a few minutes after wrong PINs.")
        elif pin and hmac.compare_digest(t, pin):
            agent._fin_until, agent._fin_fails = now + FIN_SESSION, 0
            _fin_answer(agent, tg, pend[0])
        else:
            agent._fin_fails = getattr(agent, "_fin_fails", 0) + 1
            if agent._fin_fails >= 3:
                agent._fin_lock, agent._fin_fails = now + FIN_LOCK, 0
            tg.send_styled("Finance", icon="🔒", intro="Wrong PIN.")
        return True
    agent._fin_pending = None
    cmd = low.split(" ")[0] in FIN_CMDS
    is_read = cmd or (bool(FIN_WORD.search(t)) and _fin_plan(agent, t)["finance"])
    if not is_read or TX_ENTRY.match(t):
        return False                      # everything else reaches the brain, which can call finance_report
    finance_request(agent, tg, t)
    return True


TX_ENTRY = re.compile(r"^\s*(?:[a-zа-я]{1,10}\s*[:,]\s*)?[+\-\u2212\u2013\u2014]\s*\d", re.I)

PAIR_WINDOW = 300                 # seconds: "I want to apply for this" waits this long for the thing it means
POINTER = re.compile(r"\b(this|that|it|these|bu|shu|buni|shuni|bunga|shunga|это|этот|эту|этого|на это)\b", re.I)


def _is_pointer(t):
    """A short message that points at something not sent yet: 'I wanna apply for this', 'save this', 'buni saqla'."""
    return (len(t) <= 80 and "\n" not in t and not t.startswith("/") and bool(POINTER.search(t))
            and bool(ACT.search(t)) and not DONE_PAST.search(t) and not t.rstrip().endswith("?")
            and not EMAILISH.search(t))


EMAILISH = re.compile(r"@|https?://|\d{3,}")
ACT = re.compile(r"\b(apply|register|sign up|enrol+|participate|save|keep|note|look at|check|read|"
                 r"topshir\w*|ariza|ro'?yxatdan|qatnash\w*|saqla\w*|ko'?rib|o'qib|"
                 r"подать|подам|запиш\w*|сохрани\w*|участв\w*|посмотри)\b", re.I)
DONE_PAST = re.compile(r"\b(did|done|finished|completed|applied|saved|qildim|topshirdim|сделал)\b", re.I)


COPIED_SENDER = re.compile(r"^\s*>?\s*(?:(?:[A-ZА-ЯЁ]\.?\s?){1,3}|[\w' ]{1,30}assistant)\s*:\s*$", re.I)


def _strip_copied_sender(t):
    """Text copied out of Telegram starts with the sender line ('M. A:', 'MrSimple's Assistant:'): drop those lines."""
    lines = t.splitlines()
    while lines and (COPIED_SENDER.match(lines[0]) or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip() or t


@contextlib.contextmanager
def _typing(tg):
    """Telegram shows 'typing…' for 5 s only: repeat it every 4 s until the answer is out."""
    stop = threading.Event()
    def keep():
        while not stop.is_set():
            try:
                tg.typing()
            except Exception:
                pass
            stop.wait(4)
    threading.Thread(target=keep, daemon=True).start()
    try:
        yield
    finally:
        stop.set()


def on_telegram_text(agent, tg, text):
    with _typing(tg):
        return _on_telegram_text(agent, tg, text)


def _on_telegram_text(agent, tg, text):
    t = _strip_copied_sender(text.strip())
    pend = getattr(agent, "_pointer", None)
    last, agent._last_msg = getattr(agent, "_last_msg", 0), time.time()
    if pend and time.time() - pend[1] < PAIR_WINDOW and not t.startswith("/"):
        agent._pointer = None
        t = f"{pend[0]}\n\n{t}"                              # what the user meant + the thing itself, as ONE request
        print(f"[route] paired with the previous message: {pend[0][:40]!r}")
    elif (_is_pointer(t) and not getattr(agent, "_fin_pending", None)
          and time.time() - last > 120):    # "this" right after a message means that one
        agent._pointer = (t, time.time())
        print(f"[route] pointer message, waiting for what it refers to: {t[:40]!r}")
        return tg.send_styled(S[_lang(agent)]["hint_title"], icon="📎",
                              intro="Okay, send it and I'll take care of it.")
    if not getattr(agent, "_fin_pending", None):          # a PIN reply must reach the finance gate first
        try:
            from . import clients
            if clients.telegram_text(agent, tg, t):          # before finance: "+998 ..." is not an amount
                return
        except Exception as e:
            print("[clients]", repr(e)[:200])
    if finance_gate(agent, tg, t):
        print(f"[route] finance gate: {t[:60]!r}")
        return
    fc = getattr(agent, "finance_chat", None)
    if fc:
        reply = fc.handle(t)
        if reply is not None:
            print(f"[route] finance_chat (entry/undo/summary): {t[:60]!r}")
            return tg.send_styled("Finance", icon="💰", intro=reply)
    m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", t, re.S)
    if m:
        print(f"[route] command /{m.group(1)}")
        cmd, rest = m.group(1).lower(), m.group(2).strip()
        return _command(agent, tg, cmd, rest)
    print(f"[route] free text -> brain: {t[:60]!r}")
    return _free_text(agent, tg, t)


def start_telegram(agent, tg):
    if os.getenv("LINDA_TELEGRAM_STANDALONE") == "1" and not getattr(agent, "is_bot", False):
        print("[telegram] слушает отдельный бот, сервер не слушает")
        return

    # 1) finance: entries, undo, /summary (must not depend on the SQL helper)
    try:
        from linda.telegram_linda.finance import FinanceService
        from .finance_chat import FinanceChat
        agent.finance = FinanceService.from_config(
            chooser=lambda system, user: llm_text(agent, system, user, temp=0))
        agent.finance_chat = FinanceChat(agent.finance)
    except Exception as e:
        print("[finance] disabled:", repr(e)[:150])

    # 2) general read-only SQL questions (finance, tasks, practice, ...)
    try:
        from .finance_ask import DataAsk, DOMAINS
        fin = getattr(agent, "finance", None)
        db_path = fin.db_path if fin else str(tools.ROOT / agent.cfg.get("db_path", "linda.db"))
        agent.data_ask = DataAsk(
            db_path,
            llm=lambda system, user: llm_text(agent, system, user, temp=0),
            domains=DOMAINS,
            llm_answer=bool(fin.cfg.get("llm_answer", True)) if fin else True)
    except Exception as e:
        print("[data ask] disabled:", repr(e)[:150])

    tg.set_commands(COMMANDS)
    tg.listen(lambda text: on_telegram_text(agent, tg, text))