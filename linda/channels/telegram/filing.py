"""Plans, filing free text without the AI agent (fallback classifier), finishing tasks, answering questions."""
import datetime
import difflib
import re
import threading
import time
from linda import tools
from linda.memory import reference
from linda.channels.telegram.telegram import esc

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


def done_files(agent, title, attr="done_files"):
    """'\n   📁 data/tasks.md · data/projects/linda.md' for a task closed (or added) in this message ('' if unknown)."""
    files = (getattr(agent, attr, None) or {}).get(tools._norm(title))
    if not files:
        done = getattr(agent, attr, None) or {}
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


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import PLANS_WINDOW, S, TIME, _json_obj, _lang, llm_text  # noqa: E402
from linda.channels.telegram.news import _do, muted  # noqa: E402
