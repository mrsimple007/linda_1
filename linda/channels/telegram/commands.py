"""Tool handlers used by voice (news, birthday list) and the /commands."""
import datetime
import re
from linda import tools
from linda.i18n import fmt_date, fmt_today
from linda.memory import reference
from linda.channels.telegram.telegram import esc

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
        from linda.business import bot_stats, clients
        q = rest or "new users and active users today, yesterday and this month"
        return clients._run(agent, tg, bot_stats.bot_stats, {"question": q}, q)
    if cmd in ("client", "clients", "client_undo"):
        from linda.business import clients
        return clients.telegram_command(agent, tg, cmd, rest)
    if cmd in ("reminders", "followups", "review"):
        from linda.skills import proactive
        if cmd == "reminders":
            return proactive.send_reminders(agent, tg, quiet=False)
        if cmd == "followups":
            return proactive.send_followups(agent, tg, quiet=False)
        return proactive.send_weekly_review(agent, tg)
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


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import M, PLAN_WORDS, S, TG_WORDS, _lang  # noqa: E402
from linda.channels.telegram.news import _open_today, get_news, muted, news_sections  # noqa: E402
from linda.channels.telegram.filing import _done_command, _plans_reply, _save_items, parse_plans  # noqa: E402
