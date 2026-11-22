"""Shared Telegram pieces: texts (S, M), COMMANDS, the LLM helper, the silent tool runner (muted)."""
import json
import re
from linda.channels.telegram.telegram import esc

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
            ("goal", "Save a goal"), ("done", "Mark a task done"), ("notes", "Everything I remember"),
            ("reminders", "Deadlines coming up and overdue"), ("followups", "Clients waiting for action"),
            ("review", "Weekly review"), ("songs", "Your songs: learning, learned, want to learn")]

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


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.news import _do, muted  # noqa: E402
from linda.channels.telegram.finance_gate import SHEETS, _money  # noqa: E402
