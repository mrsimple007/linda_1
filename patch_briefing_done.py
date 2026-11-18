"""Run from the project root:   python patch_briefing_done.py [--dry]

Teaches linda/telegram_linda/briefing.py to CLOSE tasks when you say they are done:
  "all of the tasks are done, just take exam is open"  ->  closes the other tasks, leaves "Take exam"
  /done exam                                           ->  closes the one task that matches
The model only picks task NUMBERS from your open list; Linda's own 'task done' tool does the closing, and the
open list is re-read afterwards, so a failure is never reported as success. A backup (.bak) is written first.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
P = ROOT / "linda" / "telegram_linda" / "briefing.py"
DRY = "--dry" in sys.argv

NEW_BLOCK = '''CLASSIFY = """You file messages that {user} sends to his personal assistant Linda. Today is {today}.
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
    hint = ("\\nThe user was just asked for today's plans, so unless a message is clearly something else, "
            "treat its items as tasks." if plans_mode else "")
    titles = _open_titles(agent)
    listing = "\\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1)) or "(none)"
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
        tm = tm if re.fullmatch(r"([01]\\d|2[0-3]):[0-5]\\d", tm) else ""
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
    return [f"☑️ <b>Done</b>: {esc(t)}" if tools._norm(t) not in still else f"⚠️ <b>Could not close</b>: {esc(t)}"
            for t in titles]


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
            lines.append(f"{icon} <b>{label}</b>: {esc(it['text'])}" + (f"\\n   <i>{esc(note)}</i>" if note else ""))
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


'''


def main():
    if not P.exists():
        sys.exit(f"[error] {P} not found. Run from the project root.")
    src = P.read_text(encoding="utf-8")
    if "_complete_tasks" in src:
        print("[have] briefing.py is already patched")
        return
    m1 = re.search(r'^CLASSIFY = """', src, re.M)
    m2 = re.search(r'^# =+ answering questions', src, re.M)
    if not (m1 and m2 and m1.start() < m2.start()):
        sys.exit("[error] could not find the CLASSIFY ... answering questions region. Nothing changed.")
    out = src[:m1.start()] + NEW_BLOCK + src[m2.start():]

    a = '("goal", "Save a goal")]'
    b = '    if cmd in DIRECT:\n        if not rest:'
    if a not in out or b not in out:
        sys.exit("[error] could not find COMMANDS or the DIRECT branch. Nothing changed.")
    out = out.replace(a, '("goal", "Save a goal"), ("done", "Mark a task done")]', 1)
    out = out.replace(b, '    if cmd == "done":\n'
                         '        return _done_command(agent, tg, rest) if rest else \\\n'
                         '            tg.send_styled(t["hint_title"], icon="💬", intro="Write it after the command, like: /done exam")\n'
                         + b, 1)
    compile(out, str(P), "exec")                                   # syntax check before touching the file
    if DRY:
        print("[dry] would patch", P)
        return
    P.with_suffix(".py.bak").write_text(src, encoding="utf-8")
    P.write_text(out, encoding="utf-8")
    print(f"[ok] patched {P.relative_to(ROOT)} (backup: briefing.py.bak). Restart the bot.")


main()