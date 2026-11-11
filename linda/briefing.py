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

from . import tools
from .i18n import fmt_date
from .telegram import esc

PLANS_WINDOW = 4 * 3600          # after the morning message, a plain reply is read as "today's plans" first
TIME = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b")
LANG_NAME = {"en": "English", "ru": "Russian", "uz": "Uzbek"}
FEEDS = ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://feeds.bbci.co.uk/news/business/rss.xml",
         "https://feeds.bbci.co.uk/news/technology/rss.xml", "https://feeds.npr.org/1001/rss.xml"]
TG_WORDS = re.compile(r"telegram|\bsend\b|отправ|пришли|yubor|jo'?nat", re.I)
PLAN_WORDS = re.compile(r"\bplans?\b|\btasks?\b|\bto-?do\b|планы?\b|задач|rejalar|vazifa", re.I)
COMMANDS = [("start", "Meet Linda"), ("news", "Latest headlines with links"), ("tasks", "Your open tasks"),
            ("birthdays", "All saved birthdays"), ("plans", "Add today's plans"), ("idea", "Save an idea"),
            ("goal", "Save a goal")]

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
           "can_lines": ["/news: fresh headlines with links", "/tasks: your open tasks",
                         "/birthdays: every saved birthday", "/plans: add today's plans, one per line",
                         "/idea and /goal: save them in one step"],
           "just_line": "An idea, a goal, a fact about you, a task, a diary thought, or what you worked on. "
                        "I work out where it belongs and save it.",
           "start_footer": "Every morning I send the news and ask for your plans."},
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
           "can_lines": ["/news: свежие новости со ссылками", "/tasks: открытые задачи",
                         "/birthdays: все дни рождения", "/plans: планы на сегодня, каждый с новой строки",
                         "/idea и /goal: сохранить в один шаг"],
           "just_line": "Идею, цель, факт о тебе, задачу, мысль для дневника или что ты сделал. "
                        "Я пойму, куда это отнести, и сохраню.",
           "start_footer": "Каждое утро я присылаю новости и спрашиваю про планы."},
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
           "can_lines": ["/news: havolali so'nggi yangiliklar", "/tasks: ochiq vazifalar",
                         "/birthdays: barcha tug'ilgan kunlar", "/plans: bugungi rejalar, har biri yangi qatorda",
                         "/idea va /goal: bir qadamda saqlash"],
           "just_line": "G'oya, maqsad, o'zingiz haqingizda fakt, vazifa, kundalik fikr yoki nima qilganingiz. "
                        "Qayerga tegishli ekanini aniqlab, saqlayman.",
           "start_footer": "Har tongda yangiliklar yuboraman va rejalaringizni so'rayman."},
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


def _lang(agent):
    d = agent.cfg.get("default_language", "en")
    return d if d in S else "en"


def _chunk_text(c):
    if isinstance(c, str):
        return c
    t = getattr(c, "text", None)
    return t if isinstance(t, str) else ""


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
            from .brain import reply_lang
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
- task: something to do (optionally with a day and time)
- journal: feelings, thoughts or reflections about the day (diary)
- progress: what {user} worked on, finished or improved (a project, studies, or a feature of Linda). Set "project";
  use "Linda" for anything about the assistant itself: new features, bugs, wishes for her
- note: a small one-off fact or reminder that fits nowhere else
- none: a question, greeting or chat that must not be saved
Write every "text" as ONE clear standalone sentence in the user's own language (replace 'it' and 'that' with what
they refer to). For tasks set "due" (YYYY-MM-DD) only if a day is stated or implied (today, tomorrow, Friday) and
"time" (HH:MM) only if stated; otherwise leave them empty.{hint}
Return ONLY JSON: {{"items":[{{"type":"idea","text":"...","due":"","time":"","project":""}}],"reply":"one short friendly line, only used when nothing is saved"}}"""


def classify(agent, text, plans_mode):
    """-> (items, reply) or (None, '') when no LLM answered."""
    hint = ("\nThe user was just asked for today's plans, so unless a message is clearly something else, "
            "treat its items as tasks." if plans_mode else "")
    system = CLASSIFY.format(user=agent.cfg.get("user_name") or "the user", today=datetime.date.today().isoformat(),
                             hint=hint)
    obj = _json_obj(llm_text(agent, system, text))
    if not isinstance(obj, dict):
        return None, ""
    items = []
    for e in obj.get("items") or []:
        if not isinstance(e, dict) or e.get("type") not in FILE:
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


def _save_items(agent, tg, items, original):
    t = S[_lang(agent)]
    lines = []
    with muted(agent, original) as mute:
        for it in items:
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


def _free_text(agent, tg, text):
    t = S[_lang(agent)]
    in_window = time.time() < getattr(agent, "plans_until", 0)
    tg.typing()
    if "\n" not in text and QUESTION.search(text):           # clearly a question: answer it, do not file it
        return _answer(agent, tg, text)
    items, reply = classify(agent, text, in_window)
    if items is None:                                        # no LLM answered
        if in_window:
            return _plans_reply(agent, tg, parse_plans(text)) if parse_plans(text) else \
                tg.send_styled(t["hint_title"], icon="💬", intro=t["empty"])
        return tg.send_styled(t["hint_title"], icon="💬", intro=t["no_ai"])
    if in_window:
        for it in items:
            if it["type"] == "task" and not it["due"]:
                it["due"] = str(datetime.date.today())
        agent.plans_until = time.time() + PLANS_WINDOW
    if not items:                                            # chat / question the shortcut missed
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


DIRECT = {"idea": "idea", "goal": "goal", "profile": "profile", "task": "task", "journal": "journal", "note": "note"}


def _command(agent, tg, cmd, rest):
    t = S[_lang(agent)]
    if cmd in ("start", "help"):
        return _welcome(agent, tg)
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
    if cmd in DIRECT:
        if not rest:
            return tg.send_styled(t["hint_title"], icon="💬", intro=t["usage"].format(ex=f"/{cmd} …"))
        item = {"type": DIRECT[cmd], "text": rest[:400], "due": "", "time": "", "project": ""}
        if item["type"] == "task":
            item["due"] = str(datetime.date.today()) if re.search(r"\btoday\b", rest, re.I) else ""
        return _save_items(agent, tg, [item], rest)
    return tg.send_styled(t["hint_title"], icon="💬", intro=t["unknown"])


def on_telegram_text(agent, tg, text):
    t = text.strip()
    m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", t, re.S)
    if m:
        return _command(agent, tg, m.group(1).lower(), m.group(2).strip())
    return _free_text(agent, tg, t)


def start_telegram(agent, tg):
    """Call once at startup: sets the bot's "/" menu and starts reading your messages."""
    tg.set_commands(COMMANDS)
    tg.listen(lambda text: on_telegram_text(agent, tg, text))