"""Every skill as a Gemini function declaration. The LLM calls them directly: this replaces the intent router.
Handlers are the existing Agent.do_<intent> methods (they speak for themselves).

Sections: TABLE (declarations) -> write handlers -> execute() -> memory read (recall / prefetch / memory_index)."""
import datetime
import difflib
import json
import pathlib
import re
import sqlite3
import threading
import time

from google.genai import types

from .config import ROOT
from .i18n import day_label, fmt_date
from .router import Intent

S, I, B = "string", "integer", "boolean"
TG = {"telegram": (B, "true if the user also wants the result sent to their Telegram")}
# (name, description, {param: (type, description)}, required, alias {tool param -> Intent field})


TABLE = [
    ("process_download", "Do something with the NEWEST file in the user's Downloads folder (convert, rewrite, summarize, extract, translate).",
     {"instruction": (S, "what to do, in the user's words"),
      "preset": (S, "'quiz_txt' to turn a quiz/test document into plain text: numbered question, +right answer, -wrong answers; otherwise empty"), **TG}, [], {}),
    ("news", "Spoken world-news briefing (uses Google Search).",
     {"language": (S, "en, ru or uz, only if the user names a language"), "topic": (S, "optional topic"), **TG}, [], {}),
    ("morning", "Good-morning briefing: greeting, birthdays, today's calendar, weather, news.", {}, [], {}),
    ("weather", "Weather report.", {"topic": (S, "city, only if the user names one"), **TG}, [], {}),
    ("add_birthday", "Save someone's birthday.",
     {"name": (S, "person"), "relation": (S, "mother, friend..."), "day": (I, "1-31"), "month": (I, "1-12"),
      "year": (I, "birth year if said"), "language": (S, "greeting language en/ru/uz if said")}, ["name", "day", "month"], {}),
    ("list_birthdays", "List the saved birthdays. For 'send me the birthday list on Telegram' set telegram=true: the full list is built from the database, never write it yourself.", {**TG}, [], {}),
    ("delete_birthday", "Delete a saved birthday.", {"name": (S, "person")}, ["name"], {}),
    ("birthday_greeting", "Write a birthday greeting and send it to the user's Telegram so they can forward it.",
     {"name": (S, "person"), "relation": (S, "relation"), "language": (S, "en, ru or uz")}, ["name"], {}),
    ("calendar_add", "Add an event to the calendar.",
     {"title": (S, "short title in the user's language, no date words"),
      "start": (S, "local ISO datetime, e.g. 2026-10-28T15:00 (09:00 if no time said)"),
      "duration_minutes": (I, "default 60")}, ["title", "start"], {}),
    ("calendar_agenda", "Read what is on the calendar.", {"when": (S, "today, tomorrow or week")}, [], {}),
    ("calendar_undo", "Remove the calendar event that was just added.", {}, [], {}),
    ("telegram_send", "Send something to the user's own Telegram. Never use it for lists of stored data (birthdays, tasks, news): call that tool with telegram=true instead.",
     {"text": (S, "the full message to send. Always write the actual content. For a greeting, wish or personal message write it in full: warm, natural, personal to the person, several sentences, never a one-liner. Leave empty only if the user says 'send that' about your previous answer.")}, ["text"], {}),
    ("clipboard", "Work with the text the user copied (summarize, translate, fix, explain).",
     {"instruction": (S, "what to do"), **TG}, [], {}),
    ("mac", "Mac control.", {"action": (S, "lock, mute, unmute or screenshot")}, ["action"], {}),
    ("chess", "Start a chess game with the user.", {"color": (S, "white or black")}, [], {}),
    ("timer", "Countdown timer or reminder after a delay.",
     {"seconds": (I, "total seconds"), "text": (S, "what to be reminded about")}, ["seconds"], {}),
    ("open_app", "Open a macOS application.", {"app": (S, "application name")}, ["app"], {}),
    ("volume", "Set the volume.", {"level": (I, "0-100")}, ["level"], {}),
    ("time", "Tell the current local time.", {}, [], {}),
    ("battery", "Tell the battery level.", {}, [], {}),
    ("remember", "Remember a small one-off fact or reminder. Lasting facts about the user go to save_note instead.",
     {"text": (S, "the fact, one full sentence")}, ["text"], {}),
    ("plan", "Plan how to use the time the user has (practice, driving theory...). Explains why.",
     {"minutes": (I, "time available"), "topic": (S, "'practice' if they ask only about practice")}, [], {}),
    ("analyze_playing", "Listen to the user play an instrument and give feedback from pitch/timing analysis.",
     {"name": (S, "song, if said"), "instrument": (S, "piano or guitar")}, [], {"instrument": "action"}),
    ("tune", "Tune an instrument: listens to one note.", {}, [], {}),
    ("quiz", "Start a voice quiz (driving theory and other question banks).",
     {"topic": (S, "question set name if said"), "total": (I, "number of questions if said")}, [], {}),
    ("log_driving", "Save a driving test result.",
     {"score": (I, "points or correct answers"), "total": (I, "maximum"), "kind": (S, "theory, practical or mock_test"),
      "notes": (S, "notes")}, ["score", "total"], {"kind": "topic", "notes": "text"}),
    ("task_add", "Add a to-do item. If the user says today or tomorrow, put that real date in due. ONE call per separate to-do: never merge different things into one title. "
     "Also use it when the user says they NEED or WANT to do something later (e.g. practice in the evening): put a time if said. "
     "Not for work already finished, and not if a matching open task exists. "
     "Set project to the ongoing project it belongs to, by meaning, if any.",
     {"title": (S, "the task"),
      "due": (S, "due date YYYY-MM-DD if said"),
      "time": (S, "time of day HH:MM if said (evening = 20:00)"),
      "project": (S, "exact name of the ongoing project this belongs to, if any")}, ["title"], {}),
    ("task_list", "List the open tasks.", {}, [], {}),
    ("task_done", "Mark open tasks as done. Use it when the user says they finished, completed or already did something that "
     "matches an open task by meaning (speech-to-text may distort words). Ids come from the open tasks list. "
     "ONE call can close many: ids='3,5,7'. For 'everything is done except X and Y' pass keep_ids='<ids of X and Y>': "
     "ALL other open tasks are closed, so list only the tasks that stay open. Tasks the user says they are still "
     "working on, still practicing or that are still open must stay open. "
     "Also pass the ongoing project it belongs to, by meaning, if there is one (single task only).",
     {"id": (I, "id of ONE task"),
      "ids": (S, "comma-separated ids of the tasks to close, e.g. '3,5,7'"),
      "keep_ids": (S, "comma-separated ids that must STAY open; every other open task is closed. Only with at least one id"),
      "text": (S, "words from the task, only if you have no id"),
      "note": (S, "TWO sentences, in English, of what was achieved by finishing this task (single task only). "
                  "Written to the project's progress log."),
      "project": (S, "exact name of the ongoing project this task belongs to, if any")}, [], {}), 
      ("song_add", "Add a song or piece to the repertoire. FIRST work out the proper title and the composer or artist from your own knowledge, then save with full details. Never save a bare guess.",
     {"name": (S, "proper title, e.g. 'Game of Thrones (Main Theme)'"), "composer": (S, "composer or artist, e.g. 'Ramin Djawadi'"),
      "instrument": (S, "piano or guitar"), "status": (S, "learning, polishing or repertoire"),
      "notes": (S, "one short line (difficulty, key, arrangement) if known")}, ["name"], {}),
    ("music", "Control Spotify. action=play with query plays that specific song or artist (e.g. query 'The Nights Avicii'); action=play without query resumes; also pause, next, previous. ONLY when the user explicitly asks for music.",
     {"action": (S, "play, pause, next or previous"), "query": (S, "song and artist to play")}, ["action"], {"query": "text"}),
    ("journal_add", "Personal diary only: feelings, thoughts, reflections. NOT for work done or things improved (use task_done or log_progress).",
     {"text": (S, "the entry")}, ["text"], {}),
    ("search_notes", "Search the user's OWN notes, papers, project docs and journal.",
     {"query": (S, "the question"), "source": (S, "notes, papers, projects or journal if said")}, ["query"],
     {"query": "text", "source": "topic"}),
    ("stats", "Progress and statistics.", {"topic": (S, "practice, chess, driving or tasks; empty for everything"), **TG}, [], {}),
    ("web_answer", "Answer a question that needs CURRENT information from the web (news, sports, prices, 'latest', 'today').",
     {"query": (S, "the question"), **TG}, ["query"], {}),
    ("save_note", "Save something the user said to a personal file. file=profile: lasting facts about the user (languages, schedule, instruments, health, preferences). NOT facts about a course or project (its students, dates): those go to save_reference. file=goals: targets and ambitions (yearly/monthly/weekly). file=ideas: random thoughts and project ideas to keep. Tasks with a due date go to task_add, diary entries to journal_add.",
     {"file": (S, "profile, goals or ideas"),
      "text": (S, "the note itself, written out in full from the conversation (replace 'it', 'that', 'about it' with what they refer to). Never the instruction like 'write this to ideas'"),
      "section": (S, "optional heading such as Yearly, Monthly, Weekly, Music")}, ["file", "text"], {}),
    ("complete_goal", "Mark a goal in goals.md as done.", {"text": (S, "a few words from the goal")}, ["text"], {}),
    ("remove_note", "Delete an outdated or wrong line from profile, goals or ideas.",
     {"file": (S, "profile, goals or ideas"), "text": (S, "words from the line to delete")}, ["file", "text"], {}),
    ("log_progress", "Record finished work that matches NO open task (past tense). Compare the update with the open tasks first: "
     "if one matches by meaning, use task_done instead. Then look at 'ongoing projects' in the index: if the work belongs to one "
     "of them by meaning (e.g. pizza data collection belongs to the Data Collection project), use that exact name. "
     "Set new_project=true ONLY for a clearly different project that is not in the list. One clear sentence in English.",
     {"project": (S, "exact name of an ongoing project, or the new project's name"),
      "text": (S, "what was done and what is now possible"),
      "new_project": (B, "true only if this is a new project not in the ongoing list"),
      "no_matching_task": (B, "true only after you checked the open tasks and none matches")}, ["project", "text"], {}),
    ("practice_log", "Log a practice session the user ALREADY DID (past tense: 'I practiced', 'I played'). "
     "If the user says they need, want or plan to practice, that is NOT a session: use task_add instead. "
     "date is the real YYYY-MM-DD if it was not today ('yesterday' = yesterday's actual date).",
     {"minutes": (I, "duration in minutes, exactly as said"), "name": (S, "song, if said"), "instrument": (S, "piano or guitar"),
      "date": (S, "YYYY-MM-DD if not today"), "rating": (I, "self-rating 1-5 if said"), "focus": (S, "what was practiced"),
      "already_done": (B, "true only if the user said the practice already happened")},
     ["minutes", "already_done"], {}),
    ("practice_delete", "Remove wrongly logged practice sessions. Use when the user says a session was logged by mistake or did not happen. "
     "Removes the most recent ones (count), or the most recent ones of one date.",
     {"count": (I, "how many of the latest sessions to remove, default 1"),
      "date": (S, "YYYY-MM-DD, only if the user names a day")}, [], {}),
    ("read_note", "Read out the user's saved profile, goals or ideas, or answer questions about them ('what are my goals').",
     {"file": (S, "profile, goals or ideas")}, ["file"], {}),
    ("birthday_lookup", "Tell when a specific person's birthday is. 'my birthday' means the user's own.",
     {"name": (S, "person's name, a relation such as 'brother', or 'me'")}, [], {}),
    ("save_reference", "Save information to a topic note (data/notes/<topic>.md): a course outline, lesson list, schedule, "
     "resources, how-to, checklist, AND facts about an ongoing course or project (its students, start date, rules), even "
     "short ones. Copy the user's text in full, keeping its lines. Not for finished work (log_progress).",     {"topic": (S, "the subject, used as the file name, e.g. 'IT course'"),
      "text": (S, "the user's full text, copied exactly with its line breaks")}, ["topic", "text"], {}),
    ("read_reference", "Read a saved reference note in full (course outline, lessons, resources). Use it when the user asks "
     "about the content of such a note.",
     {"topic": (S, "which note, e.g. 'IT course'; empty lists the saved notes")}, [], {}),
("query_data", "Answer a QUESTION about the user's stored data by querying it: tasks, practice and songs, finance, clients, "
 "chess games. Use for counts, totals, 'latest', 'how many', per-period or per-person questions. Not for saving.",
 {"question": (S, "the question in full"), "domain": (S, "finance, tasks, practice")}, ["question", "domain"], {}),

("finance_report", "Any question about the user's money (revenue, income, expenses, spending, balance, purchases, "
 "transactions). Handled by code behind a PIN: never answer it yourself and never state finance numbers.",
 {"question": (S, "the user's question in full")}, ["question"], {}),
]



# Tools that WRITE data run one after another in the order requested; the rest can run in parallel.
SEQUENTIAL = {"add_birthday", "delete_birthday", "calendar_add", "calendar_undo", "remember", "song_add", "practice_log",
              "practice_delete", "log_driving", "task_add", "task_done", "journal_add", "timer", "chess", "quiz", "mac",
              "volume", "music", "save_note", "complete_goal", "remove_note", "log_progress"}
SEQUENTIAL.update({"process_download", "clipboard", "telegram_send", "birthday_greeting", "save_reference", "read_reference"})
LANG_WORDS = {"english": "en", "russian": "ru", "uzbek": "uz"}
SPEC = {t[0]: t for t in TABLE}
_TYPE = {S: types.Type.STRING, I: types.Type.INTEGER, B: types.Type.BOOLEAN}


def declarations():
    out = []
    for name, desc, params, required, _ in {r[0]: r for r in TABLE}.values():
        kw = {"name": name, "description": desc}
        if params:
            kw["parameters"] = types.Schema(
                type=types.Type.OBJECT, required=required or None,
                properties={k: types.Schema(type=_TYPE[tp], description=d) for k, (tp, d) in params.items()})
        out.append(types.FunctionDeclaration(**kw))
    return out


# ===================== write handlers =====================

def open_tasks_text(agent, limit=40):
    """Open tasks with ids, today's first, so the model can match an update to a task by meaning."""
    today = str(datetime.date.today())
    rows = sorted(agent.store.open_tasks(), key=lambda r: (str(r[2] or "") != today, str(r[2] or "9999")))
    return "; ".join(f"id {r[0]}: {r[1]}" + (f" (due {r[2]})" if r[2] else "") for r in rows[:limit]) or "none"


def _can_retry(agent):
    """True inside the provider loop (speaker is a Capture): the model reads the tool result and may call again.
    On the Gemini path nothing is read back, so errors must not be spoken."""
    return type(agent.speaker).__name__ == "Capture"


def _resolve_project(agent, project, new=False):
    """-> (path, error). Existing project (spelling variants allowed); a new file only when the model says it is new."""
    pdir = agent.data_dir / "projects"
    names = {p.stem: p for p in pdir.glob("*.md")} if pdir.exists() else {}
    slug = re.sub(r"[^a-z0-9]+", "-", str(project).lower()).strip("-") or "general"
    if slug in names:
        return names[slug], None
    close = difflib.get_close_matches(slug, list(names), n=1, cutoff=0.85)
    if close:
        return names[close[0]], None
    if new or not _can_retry(agent):
        return pdir / f"{slug}.md", None
    return None, (f"error: there is no project called '{project}'. Existing projects: {', '.join(names) or 'none'}. "
                  "If this work belongs to one of them, call log_progress again with that exact project. "
                  "Only if it is clearly a different, new project, call again with new_project=true.")


def _log_progress(agent, a, text):
    project, entry = str(a.get("project") or "General").strip(), str(a.get("text") or "").strip()
    if not entry:
        return agent.k("error")
    if not a.get("no_matching_task") and _can_retry(agent):
        try:
            done = _done_on(agent, [datetime.date.today()])
        except Exception:
            done = []
        return agent.say(
            "error: not logged yet. Compare the user's update with the open tasks: " + open_tasks_text(agent) +
            ". Finished today: " + ("; ".join(done) or "none") + ". "
            "If it matches an open task by meaning, call task_done with that id and nothing else. "
            "If it matches a task already finished today, do not call anything: tell the user it is already done. "
            "Only if nothing matches, call log_progress again with no_matching_task=true.")
    p, err = _resolve_project(agent, project, bool(a.get("new_project")))
    if err:
        return agent.say(err)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(f"# {project}: progress log\n\n", encoding="utf-8")
    if entry.lower() in p.read_text(encoding="utf-8", errors="ignore").lower():
        return agent.say("That is already in the log.")
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.date.today()} · {entry}\n")
    if agent.docs:
        def _idx():
            try:
                agent.docs.index_file(p, "projects")
            except Exception as e:
                print("[index]", e)
        threading.Thread(target=_idx, daemon=True).start()
    agent.say({"en": f"Logged for {project}.", "ru": f"Записала: {project}.", "uz": f"{project} uchun yozildi."}[agent.lang])


def project_context(agent, n=12):
    rows = []
    try:
        for p in (agent.data_dir / "projects").glob("*.md"):
            for l in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.match(r"-\s*(\d{4}-\d{2}-\d{2})\s*·\s*(.+)", l)
                if m:
                    rows.append((m.group(1), p.stem, m.group(2).strip()))
    except Exception as e:
        print("[progress]", repr(e))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [f"{d} [{name}] {t}" for d, name, t in rows[:n]]


def coerce(args, alias):
    """LLM arguments -> Intent fields (renames aliases, fixes types, drops unknowns)."""
    out = {}
    for k, v in (args or {}).items():
        k = alias.get(k, k)
        field = Intent.model_fields.get(k)
        if field is None or v is None or k == "intent":
            continue
        try:
            if field.annotation is int:
                v = int(float(v))
            elif field.annotation is bool:
                v = bool(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        if k == "language":
            v = LANG_WORDS.get(v.lower(), v.lower()[:2])
        out[k] = v
    return out


def _calendar_add(agent, a, text):
    cal = agent._cal()
    if not cal:
        return
    start = datetime.datetime.fromisoformat(str(a["start"]))
    if start.tzinfo:
        start = start.astimezone().replace(tzinfo=None)
    title = str(a["title"])
    agent.last_event = cal.add(title, start, max(5, int(float(a.get("duration_minutes") or 60))))
    agent.k("cal_added", title=title, day=day_label(start.date(), agent.lang), time=start.strftime("%H:%M"))


_NUMW = {"1st": "first", "2nd": "second", "3rd": "third", "4th": "fourth", "5th": "fifth",
         "6th": "sixth", "7th": "seventh", "8th": "eighth", "9th": "ninth", "10th": "tenth"}


def _norm(s):
    t = re.sub(r"[^\w ]+", "", str(s).lower()).strip()
    return " ".join(_NUMW.get(w, w) for w in t.split())

def _tt_db(cfg):
    p = pathlib.Path(cfg["db_path"])
    p = p if p.is_absolute() else ROOT / p
    con = sqlite3.connect(p, timeout=10)
    con.execute("create table if not exists task_times(k text primary key, at text)")
    return con


def task_times(cfg):
    con = _tt_db(cfg)
    try:
        return dict(con.execute("select k, at from task_times").fetchall())
    finally:
        con.close()


_STOP = {"the", "a", "an", "to", "for", "on", "my", "of", "and", "also", "some", "do", "work", "company",
         "task", "i", "need", "want", "wanna", "today", "tomorrow", "you"}


def _key(s):
    t = re.sub(r"recommendation systems?|recommender systems?|rec systems?|recsys", "recsys", _norm(s))
    return {w for w in t.split() if w not in _STOP}


def _same(a, b):
    ka, kb = _key(a), _key(b)
    return bool(ka and kb) and len(ka & kb) / min(len(ka), len(kb)) >= 0.75


def _dmy(d):
    try:
        return datetime.date.fromisoformat(str(d)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return "no date"


def sync_tasks_md(agent):
    """data/tasks.md: 'Open tasks - <due date>' groups, then 'Done - <date done>' groups with strikethrough."""
    try:
        tt = task_times(agent.cfg)
        lines = ["# Tasks", ""]
        open_by = {}
        for r in agent.store.open_tasks():
            open_by.setdefault(str(r[2] or "")[:10], []).append(r[1])
        for due in sorted(open_by, key=lambda d: d or "9999"):
            lines += [f"## Open tasks - {_dmy(due) if due else 'no date'}", ""]
            for t in open_by[due]:
                at = tt.get(_norm(t), "")
                lines.append(f"- [ ] {t}" + (f" ({at})" if at else ""))
            lines.append("")
        con = _tt_db(agent.cfg)
        try:
            done = con.execute("select title, done_ts from tasks where status='done' "
                               "order by done_ts desc limit 150").fetchall()
        finally:
            con.close()
        done_by = {}
        for title, ts in done:
            try:
                day = str(datetime.date.fromtimestamp(float(ts)))
            except (TypeError, ValueError):
                day = ""
            done_by.setdefault(day, []).append(title)
        for day in sorted(done_by, reverse=True):
            lines += [f"## Done - {_dmy(day) if day else 'no date'}", ""]
            lines += [f"- [x] {t.strip().rstrip('.!')}" for t in done_by[day]] + [""]
        (agent.data_dir / "tasks.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return agent.data_dir / "tasks.md"
    except Exception as e:
        print("[tasks.md]", repr(e)[:150])


def _task_project(agent, title):
    con = _tt_db(agent.cfg)
    try:
        r = con.execute("select project from tasks where title like ? limit 1", (title + "%",)).fetchone()
        return (r[0] or "").strip() if r else ""
    except sqlite3.OperationalError:          # no 'project' column
        return ""
    finally:
        con.close()

def _existing_project(agent, project):
    """-> (path, display name) of an EXISTING project log, or (None, ''). Never creates a file."""
    pdir = agent.data_dir / "projects"
    names = {p.stem: p for p in pdir.glob("*.md")} if pdir.exists() else {}
    slug = re.sub(r"[^a-z0-9]+", "-", str(project or "").lower()).strip("-")
    if not slug or slug == "general":
        return None, ""
    p = names.get(slug)
    if p is None:
        close = difflib.get_close_matches(slug, list(names), n=1, cutoff=0.85)
        p = names[close[0]] if close else None
    if p is None:
        return None, ""
    first = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
    title = first[0].lstrip("# ").split(":")[0].strip() if first and first[0].startswith("#") else p.stem
    return p, title or p.stem


# tools.py: replace _strike_in_project with this
def _log_done(agent, project, title, note=""):
    """Same line format as log_progress: '- date · what was achieved'. Never creates a project file."""
    p, _ = _existing_project(agent, project)
    if p is None:
        return None
    note = " ".join(str(note or "").split())[:300]
    line = f"- {datetime.date.today()} · {note or 'Completed: ' + title}\n"
    if line not in p.read_text(encoding="utf-8", errors="ignore"):
        with p.open("a", encoding="utf-8") as f:
            f.write(line)
    return p
            
_GENERIC = _STOP | {"prepare", "improve", "improving", "add", "study", "working", "make", "new",
                    "start", "finish", "read", "learn", "practice"}

def _belongs(agent, project, title):
    """A project the model guessed must really fit the task: its name is in the title,
    or at least 2 content words are shared with the project log."""
    p, name = _existing_project(agent, project)
    if p is None:
        return False
    tl = _norm(title)
    n = _norm(name or p.stem)
    if n and (n in tl or p.stem.replace("-", " ") in tl):
        return True
    words = {w for w in tl.split() if len(w) > 3 and w not in _GENERIC}
    log = set(_norm(p.read_text(encoding="utf-8", errors="ignore")).split())
    return len(words & log) >= 2


def finish_task(agent, tid=None, words="", project="", note=""):
    """The one place where a task becomes done: DB + tasks.md + struck-through line in its project log."""
    title, own = None, ""
    if tid not in (None, ""):
        con = _tt_db(agent.cfg)
        try:
            n = int(float(tid))
            try:
                row = con.execute("select title, status, project from tasks where id=?", (n,)).fetchone()
            except sqlite3.OperationalError:                  # no project column
                r = con.execute("select title, status from tasks where id=?", (n,)).fetchone()
                row = (r + ("",)) if r else None
            if row and row[1] == "open":
                con.execute("update tasks set status='done', done_ts=? where id=?", (time.time(), n))
                con.commit()
                title, own = row[0], (row[2] or "")
            elif row:
                return agent.say(f"You already finished that one: {row[0].rstrip('.!')}.")
        finally:
            con.close()
    if title is None and words:
        title = agent.store.complete_task(words)
    if not title:
        return agent.k("task_notfound")
    title = title.strip().rstrip(".!")
    own = (own or _task_project(agent, title)).strip()
    if own:
        project = own                                   # the task's stored project always wins
    elif not (project and _belongs(agent, project, title)):
        project = ""                                    # model guessed a project that doesn't fit
    files = [sync_tasks_md(agent), _log_done(agent, project, title, note) if project else None]
    # which md files this close touched, so the Telegram reply can show them (read by briefing.done_files)
    agent.done_files = {**(getattr(agent, "done_files", None) or {}), _norm(title): [f for f in files if f]}
    agent.k("task_done", title=title)

def _ids(v):
    return [int(x) for x in re.findall(r"\d+", str(v if v is not None else ""))]


def _task_done(agent, a, text):
    ids, keep = _ids(a.get("ids")), _ids(a.get("keep_ids"))
    one = _ids(a.get("id"))
    open_ids = [int(r[0]) for r in agent.store.open_tasks()]
    note, project = str(a.get("note") or "").strip(), str(a.get("project") or "")
    if keep:
        if not set(keep) & set(open_ids):
            return agent.say("error: none of keep_ids is an open task id. Use the ids from the open tasks list.")
        targets = [i for i in open_ids if i not in keep]
        if not targets:
            return agent.say("Nothing to close: every open task is in keep_ids.")
    else:
        targets = [i for i in dict.fromkeys(ids + one) if i in open_ids]
    if len(targets) > 1 or (targets and (ids or keep)):
        single = len(targets) == 1
        for tid in targets:
            finish_task(agent, tid, "", project if single else "", note if single else "")
        return
    finish_task(agent, a.get("id"), str(a.get("text") or "").strip(), project, note)


def add_task_once(agent, title, due=None, project="", at=""):
    title = title.strip().rstrip(".!")
    new = _norm(title)
    for r in agent.store.open_tasks():
        old = _norm(r[1])
        if old == new or difflib.SequenceMatcher(None, old, new).ratio() > 0.85:
            agent.say({"en": "That task is already on your list.", "ru": "Эта задача уже в списке.",
                       "uz": "Bu vazifa ro'yxatda bor."}[agent.lang])
            return False
    agent.store.add_task(title, due, project=project)
    if at:
        con = _tt_db(agent.cfg)
        try:
            con.execute("insert or replace into task_times values (?,?)", (new, at))
            con.commit()
        finally:
            con.close()
    sync_tasks_md(agent)
    agent.k("task_saved", title=title)
    return True


def _task_add(agent, a, text):
    due = a.get("due") or None
    if due:
        try:
            datetime.date.fromisoformat(str(due))
        except ValueError:
            due = None
    _, project = _existing_project(agent, a.get("project"))      # unknown / "General" -> no project
    add_task_once(agent, str(a["title"]), due, project, str(a.get("time") or ""))

def _web_answer(agent, a, text):
    q = str(a.get("query") or text)
    agent._out(reply=agent._stream_say(agent.gemini.stream(q, agent._chat_system(), grounding=True), agent.lang))


from . import memory_files  # noqa: E402  (kept here, as before, to avoid changing import order)
from . import reference  # noqa: E402


def _mem(name):
    return lambda agent, a, text: memory_files.handle(agent, name, a)


def _read_note(agent, a, text):
    f = str(a.get("file") or "goals").lower()
    f = f if f in ("profile", "goals", "ideas") else "goals"
    p = agent.data_dir / f"{f}.md"
    lines = []
    if p.exists():
        for l in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            l = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+|\*\*|~~|\[[ xX]\]\s*", "", l).strip()
            if l and not l.startswith(("#", "<!--")):
                lines.append(l)
    if not lines:
        return agent.say({"en": f"Your {f} are empty.", "ru": "Пока пусто.", "uz": "Hozircha bo'sh."}[agent.lang])
    agent._out(reply=agent._explain({"file": f, "items": lines[:25]},
                                    f"Tell the user what is in their {f}, briefly and naturally."))


ME = {"", "me", "my", "mine", "i", "myself", "men", "mening", "я", "мой", "моё"}


def _is_me(agent, b):
    user = (agent.cfg.get("user_name") or "").lower()
    return (b.relation or "").lower() == "me" or b.name.lower() in ("me", user)


def birthday_lookup(agent, a, text):
    who = str(a.get("name") or "").strip().lower()
    own = who in ME
    rows = agent.store.upcoming(366)
    if own:
        hits = [x for x in rows if _is_me(agent, x)]
    else:
        toks = _expand(set(_tok(who)) - STOP)
        hits = [x for x in rows if _has(x.name, toks) or _has(x.relation or "", toks)]
    if not hits:
        return agent.say({"en": "I don't have that birthday yet. Tell me the name and the date.",
                          "ru": "Этого дня рождения у меня ещё нет. Скажи имя и дату.",
                          "uz": "Bu tug'ilgan kun menda yo'q. Ism va sanani ayting."}[agent.lang])
    for b in hits[:4]:
        d = fmt_date(b.date, agent.lang)
        agent.say({"en": f"Your birthday is {d}, in {b.n} days." if own else f"{b.name}: {d}, in {b.n} days.",
                   "ru": f"Твой день рождения {d}, через {b.n} дн." if own else f"{b.name}: {d}, через {b.n} дн.",
                   "uz": f"Tug'ilgan kuningiz {d}, {b.n} kundan keyin." if own else f"{b.name}: {d}, {b.n} kundan keyin."}[agent.lang])


def _resolve_day(v):
    """'yesterday' / 'вчера' / 'kecha' / YYYY-MM-DD -> ISO date string, never in the future; None = today."""
    s, today = str(v or "").strip().lower(), datetime.date.today()
    if s in ("yesterday", "вчера", "kecha"):
        return str(today - datetime.timedelta(days=1))
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return str(d) if d <= today else None


def _practice_log(agent, a, text):
    try:
        minutes = int(float(a.get("minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return agent.k("need_minutes")
    if not a.get("already_done"):
        if _can_retry(agent):
            return agent.say("error: this is a plan, not a finished session, so it is NOT logged. If no matching task exists, "
                             "call task_add (title like 'Practice <song> on piano', due today, time if said). Open tasks: "
                             + open_tasks_text(agent))
        return agent.say({"en": "That sounds like a plan, so I didn't log it. Tell me when you've played.",
                          "ru": "Это план, поэтому я не записала. Скажи, когда сыграешь.",
                          "uz": "Bu reja, shuning uchun yozmadim. Chalib bo'lgach ayting."}[agent.lang])
    if minutes > 480:
        return agent.say(f"That is {minutes} minutes. How many minutes did you mean?")
    name, instr = str(a.get("name") or ""), str(a.get("instrument") or "").lower()
    song = agent.store.find_song(name, instr) if name else None
    try:
        r = int(float(a.get("rating") or 0))
    except (TypeError, ValueError):
        r = 0
    agent.store.log_practice(minutes, song.id if song else None, song.instrument if song else instr,
                             str(a.get("focus") or ""), r if 1 <= r <= 5 else None, day=_resolve_day(a.get("date")))
    week = agent.store.practice_summary(7)["total_minutes"]
    if song:
        agent.k("practice_logged_song", minutes=minutes, title=song.title, week=week)
    else:
        agent.k("practice_logged", minutes=minutes, week=week)


def _practice_delete(agent, a, text):
    try:
        n = max(1, min(int(float(a.get("count") or 1)), 20))
    except (TypeError, ValueError):
        n = 1
    day = _resolve_day(a.get("date"))
    con = _tt_db(agent.cfg)
    try:
        if day:
            rows = con.execute("select rowid, minutes from practice_logs where day=? order by rowid desc limit ?",
                               (day, n)).fetchall()
        else:
            rows = con.execute("select rowid, minutes from practice_logs order by rowid desc limit ?", (n,)).fetchall()
        for r in rows:
            con.execute("delete from practice_logs where rowid=?", (r[0],))
        con.commit()
    except sqlite3.OperationalError as e:
        print("[practice_delete]", e)
        return agent.k("error")
    finally:
        con.close()
    if not rows:
        return agent.say({"en": "There are no sessions to remove.", "ru": "Нечего удалять.",
                          "uz": "O'chirish uchun mashg'ulot yo'q."}[agent.lang])
    total = sum(int(r[1] or 0) for r in rows)
    week = agent.store.practice_summary(7)["total_minutes"]
    agent.say({"en": f"Removed {len(rows)} session(s), {total} minutes. {week} minutes this week now.",
               "ru": f"Удалила сессий: {len(rows)}, {total} минут. Теперь за неделю {week} минут.",
               "uz": f"{len(rows)} ta mashg'ulot o'chirildi, {total} daqiqa. Bu hafta {week} daqiqa."}[agent.lang])


def _song_db(cfg):
    con = _tt_db(cfg)
    con.execute("create table if not exists song_meta(k text primary key, composer text, notes text)")
    return con


def song_meta(cfg):
    con = _song_db(cfg)
    try:
        return dict(con.execute("select k, composer from song_meta where composer != ''").fetchall())
    finally:
        con.close()


def _song_add(agent, a, text):
    name = str(a.get("name") or "").strip()
    if not name or name.lower() in ("piano", "guitar", "song"):
        return agent.say({"en": "Which song should I add?", "ru": "Какую песню добавить?",
                          "uz": "Qaysi qo'shiqni qo'shay?"}[agent.lang])
    instr = str(a.get("instrument") or agent.cfg["goals"]["instruments"][0]).lower()
    status = str(a.get("status") or "learning").lower()
    status = status if status in ("learning", "polishing", "repertoire") else "learning"
    comp, notes = str(a.get("composer") or "").strip(), str(a.get("notes") or "").strip()
    st = agent.store
    ex = st.find_song(name, instr)
    if ex:
        name = ex.title
        if comp or notes:                # upsert overwrites, so carry the old values over
            st.upsert_song(ex.title, ex.instrument, artist=comp or ex.artist or "", status=ex.status,
                           priority=ex.priority, target_bpm=ex.target_bpm, melody=ex.melody or "",
                           notes=notes or ex.notes or "")
    else:
        st.upsert_song(name, instr, artist=comp, status=status, notes=notes)
    agent.k("song_saved", title=name + (f" by {comp}" if comp else ""), instrument=instr)


def _finance_report(agent, a, text):
    tg = getattr(agent, "tg", None)
    brain = getattr(agent, "brain", None)
    if tg is None or not getattr(brain, "extra_rules", ""):      # extra_rules is only set for Telegram
        return agent.say("error: finance reports are only available in Telegram, behind the PIN.")
    from .telegram_linda import briefing
    briefing.finance_request(agent, tg, str(a.get("question") or text))
    agent._fin_done = True
    agent.say("Done: the finance answer was sent to Telegram. Reply with one short line.")

def _query_data(agent, a, text):
    q, dom = str(a.get("question") or text), str(a.get("domain") or "").lower()
    da = getattr(agent, "data_ask", None)
    if da is None or dom not in da.domains:
        return agent.say(f"error: unknown domain. Use one of: {', '.join(da.domains) if da else 'none'}.")
    tg = getattr(agent, "tg", None)
    if dom == "finance":                                  # PIN door stays in code
        return _finance_report(agent, a, text)
    ans = da.ask(q, dom)
    agent.say(ans)                                        # the model sees it and can word it in the reply


# One registry of special handlers.
SPECIAL = {
    "calendar_add": _calendar_add, "task_add": _task_add, "web_answer": _web_answer,
    "save_note": _mem("save_note"), "complete_goal": _mem("complete_goal"), "remove_note": _mem("remove_note"),
    "read_note": _read_note, "birthday_lookup": birthday_lookup,
    "practice_log": _practice_log, "practice_delete": _practice_delete, "log_progress": _log_progress,
    "task_done": _task_done, "song_add": _song_add,
    "finance_report": _finance_report,
    "query_data": _query_data
}


# ---- news / birthday list: built in code from RSS and the database (see briefing.py) ----
def _news(agent, a, text):
    from .telegram_linda import briefing
    briefing.run_news(agent, a, text)


def _birthday_list(agent, a, text):
    from .telegram_linda import briefing
    briefing.run_birthday_list(agent, a, text)


SPECIAL.update({"news": _news, "list_birthdays": _birthday_list})
SPECIAL.update({"save_reference": reference.save_reference, "read_reference": reference.read_reference})


def execute(agent, name, args, text):
    print(f"[tool] {name} {args}")
    if name not in SPEC:
        raise KeyError(f"unknown tool {name}")
    from . import safety
    if not safety.request_is_supported(name, text):
        agent.store.audit_action(name, args, "rejected_unsubstantiated")
        agent.say({"en": "I may have misheard. Please repeat what you want me to do.",
                   "ru": "Возможно, я ослышалась. Повторите, пожалуйста, что нужно сделать.",
                   "uz": "Noto'g'ri eshitgan bo'lishim mumkin. Iltimos, nima qilishim kerakligini qaytaring."}
                  [agent.lang])
        return

    def invoke():
        if name in SPECIAL:
            SPECIAL[name](agent, args, text)
        else:
            fields = coerce(args, SPEC[name][4])
            getattr(agent, "do_" + name)(Intent(intent=name, **fields), text)
        if args.get("telegram") and agent._produced and name != "telegram_send":
            agent.send_last()

    if name == "telegram_send":
        invoke()  # send_last owns the confirmation gate and records the actual payload.
    else:
        agent.run_action(name, args, invoke)


def missing(it):
    """Required fields the local model left at their default. Used to reject guessed intents."""
    spec = SPEC.get(it.intent)
    if not spec:
        return []
    out = []
    for p in spec[3]:
        f = spec[4].get(p, p)
        fld = Intent.model_fields.get(f)
        if fld is not None and getattr(it, f) == fld.default:
            out.append(p)
    return out


# ===================== memory read: recall + prefetch =====================

REL_GROUPS = [
    {"brother", "brothers", "брат", "братья", "aka", "uka", "akam", "ukam", "birodar"},
    {"sister", "sisters", "сестра", "сестры", "opa", "opam", "singil", "singlim"},
    {"mother", "mom", "mum", "мама", "мать", "ona", "onam"},
    {"father", "dad", "отец", "папа", "ota", "otam"},
    {"friend", "friends", "друг", "друзья", "dost", "dostim"},
    {"aunt", "тётя", "тетя", "xola", "xolam", "amma", "ammam", "yanga", "yangam"},
    {"grandmother", "бабушка", "buvi", "buvim"},
    {"grandfather", "дедушка", "bobo", "bobom"},
]
STOP = {"the", "and", "what", "when", "who", "where", "which", "how", "did", "does", "for", "you", "are", "was",
        "have", "has", "had", "with", "about", "tell", "send", "telegram", "birthday", "birthdays", "please",
        "can", "could", "next", "list", "give", "show", "know", "remember", "that", "this", "from", "your",
        "mine", "just", "some", "any", "get", "say", "said", "want", "need", "like", "yes", "not", "but", "now",
        "also", "there", "they", "his", "her", "our", "all", "one", "today", "yesterday",
        "когда", "что", "кто", "мой", "моя", "мои", "день", "рождения", "вчера", "сегодня",
        "qachon", "nima", "kim", "mening", "tug", "ilgan", "kuni", "kunim", "kecha", "bugun"}
KIND_ALIAS = {"people": "all", "family": "all", "friends": "all", "relatives": "all", "project": "progress",
              "projects": "progress", "diary": "journal", "note": "notes", "remember": "memory", "reminders": "memory",
              "yesterday": "day", "today": "day", "activity": "day", "date": "day"}
DAY_STORES = {"journal", "progress", "practice", "tasks"}
ADVICE_STORES = {"goals", "ideas", "profile", "tasks", "progress", "journal", "practice", "songs"}
LOOKUP = re.compile(r"\b(when|what|who|where|which|how many|how old|tell me|do you know|did i|yesterday|today|"
                    r"birthdays?|tasks?|goals?|songs?|brother|sister|mother|father|friend|cousin|uncle|aunt|"
                    r"aka|uka|opa|qachon|kim|nima|kecha|bugun)\b|когда|кто|что|где|сколько|вчера|сегодня|брат|сестр|друг",
                    re.I)
PROGRESS_INTENT = re.compile(
    r"\bimprov\w*|\bprogress\b|capabilit\w*|what can you do|worked on|work on|what have i (?:done|worked|improved)|"
    r"changelog|умеешь|можешь|прогресс|улучш|сделал|qila olasan|yaxshilad|ishlad", re.I)
CAPS = re.compile(r"capabilit\w*|what can you do|what are you able|умеешь|что ты можешь|qila olasan", re.I)
# keep in step with the tools you add (one line per area, read aloud in 1-3 sentences)
CAPABILITIES = ("tasks (add, finish, list), birthdays (save, look up, greetings), calendar (add, read; asks to confirm), "
                "practice and songs, Spotify music, Telegram messages, timers and reminders, weather and news, "
                "chess and quizzes, profile/goals/ideas notes, diary journal, project progress log, Mac controls, "
                "answering questions about your own saved data"
                "project progress log, Mac controls, finance (add expenses and revenue in Telegram, saved to Google Sheets; "
"reports need a PIN), answering questions about your own saved data"
                
                
                )
NAME = re.compile(r"\b[A-ZА-ЯЁ][a-zа-яё']{2,}\b")
NOT_NAMES = {"linda", "uzbek", "uzbekistan", "russian", "english", "family", "chess", "friends", "friend", "brother"}


def _tok(s):
    return [w for w in re.findall(r"\w+", str(s).lower()) if len(w) > 2]


def _expand(tokens):
    out = set(tokens)
    out |= {t[:-1] for t in tokens if len(t) > 5 and t.endswith("s")} - STOP   # "ahmadjons" -> "ahmadjon"
    for g in REL_GROUPS:
        if out & g:
            out |= g
    return out


def _match(s, t):                       # short tokens must be whole words ("aka" must not hit "Zakaria")
    return bool(re.search(r"\b" + re.escape(t) + r"\b", s)) if len(t) <= 4 else t in s


def _has(s, toks):
    s = str(s).lower()
    return any(_match(s, t) for t in toks)


def _filter(rows, toks, keep=40):
    """rows: list of dicts. Returns (rows, matched)."""
    if not toks:
        return rows[:keep], True
    hit = [r for r in rows if _has(" ".join(map(str, r.values())), toks)]
    return (hit[:keep], True) if hit else (rows[:keep], False)


def _lines(lines, toks, keep=12):
    if not toks:
        return lines[:keep], True
    hit = [l for l in lines if _has(l, toks)]
    return hit[:keep], bool(hit)


def _dates_in(text):
    t, today, out = str(text).lower(), datetime.date.today(), []
    if re.search(r"yesterday|вчера|\bkecha\b", t):
        out.append(today - datetime.timedelta(days=1))
    if re.search(r"\btoday\b|сегодня|\bbugun\b", t):
        out.append(today)
    for m in re.findall(r"\d{4}-\d{2}-\d{2}", t):
        try:
            out.append(datetime.date.fromisoformat(m))
        except ValueError:
            pass
    return out


def _file_lines(agent, f):
    p = agent.data_dir / f"{f}.md"
    if not p.exists():
        return []
    return [l.strip("-*+ ").strip() for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
            if l.strip() and not l.lstrip().startswith(("<!--", "#"))]


def _diary(agent, day, limit=10):
    """Journal lines for a day. Lines from Linda's own chat log are labelled so they are not read as things you did."""
    jd, out = agent.data_dir / "journal", []
    f = jd / f"{day}.md"
    if f.exists():
        out += [l.strip() for l in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                if l.strip() and not l.startswith("#")][:limit]
    a = jd / f"activity-{day}.md"
    if a.exists():
        asked = [l.split("**You:**", 1)[1].strip().replace("**", "")[:140]
                 for l in a.read_text(encoding="utf-8", errors="ignore").splitlines() if "**You:**" in l]
        out += ["(asked Linda) " + s for s in asked[:3]]
    return out


def _practice_on(agent, day):
    rows = agent.store.q("SELECT s.title, p.instrument, p.minutes, p.focus, p.rating FROM practice_logs p "
                         "LEFT JOIN songs s ON s.id = p.song_id WHERE p.day = ?", (str(day),))
    return [{"song": t or "", "instrument": i or "", "minutes": m, "focus": f or "", "rating": r}
            for t, i, m, f, r in rows]


def _done_on(agent, dates):             # (verify) assumes tasks.done_ts is an epoch time, as finish_task writes it
    con = _tt_db(agent.cfg)
    try:
        rows = con.execute("select title, done_ts from tasks where status='done' and done_ts is not null").fetchall()
    finally:
        con.close()
    want = {str(d) for d in dates}
    out = []
    for t, ts in rows:
        try:
            if str(datetime.date.fromtimestamp(float(ts))) in want:
                out.append(t)
        except (TypeError, ValueError):
            pass
    return out


def song_meta_count(cfg):
    con = _song_db(cfg)
    try:
        return con.execute("select id from songs").fetchall()
    finally:
        con.close()


def projects_brief(agent, per=2):
    """One line per existing project log: title + latest entries, so the model can tell which project work belongs to."""
    pdir = agent.data_dir / "projects"
    out = []
    for p in (sorted(pdir.glob("*.md")) if pdir.exists() else []):
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        title = next((l.lstrip("# ").split(":")[0].strip() for l in lines if l.startswith("#")), p.stem)
        last = [re.sub(r"^-\s*\d{4}-\d{2}-\d{2}\s*·\s*", "", l)[:90] for l in lines if l.startswith("- ")][-per:]
        out.append(title + (f" (recent: {' | '.join(last)})" if last else ""))
    return out


def memory_index(agent):
    """One short line per store, put in the system prompt so the model knows what exists and how to read it."""
    st, d = agent.store, agent.data_dir

    def n(fn):
        try:
            return len(fn())
        except Exception:
            return 0
    jd = d / "journal"
    days = sorted(f.stem for f in jd.glob("2*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)) if jd.exists() else []
    try:
        tl = open_tasks_text(agent, 25)
    except Exception:
        tl = ""
    try:
        pj = "; ".join(projects_brief(agent))
    except Exception:
        pj = ""
    return "\n".join([
        f"birthdays: {n(lambda: st.upcoming(366))} people (name, date, relation)",
        f"open tasks (id: title): {tl or 'none'}",
        f"ongoing projects: {pj or 'none'}",
        f"songs: {n(lambda: song_meta_count(agent.cfg))}",
        "practice: sessions by date",
        f"profile / goals / ideas lines: {n(lambda: _file_lines(agent, 'profile'))} / "
        f"{n(lambda: _file_lines(agent, 'goals'))} / {n(lambda: _file_lines(agent, 'ideas'))}",
        f"journal: {len(days)} days" + (f", latest {days[-1]}" if days else ""),
        f"quick notes ('remember that...'): {n(lambda: st.notes(200))} (recall kind='memory')",
        f"reference notes (full text via read_reference): {reference.names(agent) or 'none'}",
        "For any question about one day (yesterday, today, a date) call recall(kind='day') once: it returns "
        "journal, progress, practice and finished tasks together. For 'what can you do' or 'what did I improve on you' "
        "call recall(kind='capabilities'). For advice (what should I do, ideas, plans, wishes) "
        "call recall(kind='advice', query=<person if any>).",
    ])


def _gather(agent, kind, q, user_text, use_text=True):
    st = agent.store
    kind = KIND_ALIAS.get(kind, kind)
    qq = q.strip() or (user_text if use_text else "")
    toks = _expand(set(_tok(qq)) - STOP)
    ptoks = set()                       # advice mode: only names and relations are search words ("should", "today" are not)
    if kind == "advice":
        ptoks = _expand({w for w in (n.lower() for n in NAME.findall(f"{q} {user_text}")) if w not in STOP and w not in NOT_NAMES}
                        | {t for t in _tok(f"{q} {user_text}") if any(t in g for g in REL_GROUPS)})
        toks = set()
    dates = [] if kind == "advice" else _dates_in(f"{q} {user_text}")
    if dates and kind in DAY_STORES - {"tasks"}:
        kind = "day"                    # one call covers every date-bound store; kind='tasks' stays a task listing
    dayq = bool(dates) and not toks     # "what did I do yesterday": date stores only
    strict = kind == "all"              # in 'all' mode a section appears only if it matched
    both = f"{q} {user_text}"
    caps = kind == "capabilities" or bool(CAPS.search(both))
    prog_intent = caps or bool(PROGRESS_INTENT.search(both))      # "what did I work on / improve", "what can you do"

    def want(k):
        if kind == "day":
            return k in DAY_STORES
        if kind == "capabilities":
            return k == "progress"
        if kind == "advice":
            return k in ADVICE_STORES or (k == "birthdays" and bool(ptoks))
        if kind == "all":
            if dayq:
                return k in DAY_STORES
            if prog_intent:
                return k == "progress"
            return True
        return kind == k

    out = {"today": f"{datetime.date.today():%A %Y-%m-%d}"}
    if dates:
        out["asked_dates"] = [str(d) for d in dates]
    hits_text = []
    if caps and kind in ("all", "capabilities"):
        out["linda_can_do"] = CAPABILITIES

    def add(label, fn):
        try:
            v = fn()
        except Exception as e:
            print(f"[recall {label}] {e!r}"[:200])
            return
        if v is not None and (v or not strict):
            out[label] = v

    # 1) free text first: names found here are searched in the birthdays table (the 'brother' -> Yusuf join)
    if want("memory"):
        def memo():
            rows, ok = _lines([str(n) for n in (st.notes(60) or [])], toks, 40 if kind == "memory" else 10)
            if strict and not ok:
                return None
            hits_text.extend(rows if toks else [])
            return rows
        add("remembered", memo)
    for f in ("profile", "goals", "ideas"):
        if want(f):
            def sect(f=f):
                every = _file_lines(agent, f)
                rows, ok = _lines(every, toks, 10 if strict else 25)
                if strict and not ok:
                    return None
                if toks and not ok:
                    out[f + "_note"] = "no line matched the query; showing the file"
                    rows = every[:12]
                if toks and ok:
                    hits_text.extend(rows)
                return rows
            add(f, sect)

    if kind == "advice" and ptoks:       # what the data says about the person (wishes, gifts, "my brother")
        pool = _file_lines(agent, "profile") + [str(n) for n in (st.notes(60) or [])]
        mine = [l for l in pool if _has(l, ptoks)][:6]
        if mine:
            out["about_person"] = mine
            hits_text.extend(mine)

    user = (agent.cfg.get("user_name") or "").lower()
    names = {n.lower().removesuffix("'s") for l in hits_text for n in NAME.findall(l)} - NOT_NAMES - {user}
    btoks = toks | names | ptoks
    tb = bool(toks or ptoks)

    if want("birthdays"):
        def bday():
            rows = []
            for b in st.upcoming(366):
                r = {"name": b.name, "date": f"{b.date:%b} {b.date.day}", "in_days": b.n, "relation": b.relation or ""}
                if b.lang:
                    r["greeting_lang"] = b.lang
                if b.age:
                    r["turning"] = b.age
                rows.append(r)
            hit, ok = _filter(rows, btoks, 8 if tb else 12)
            if strict and toks and not ok:
                return None
            if tb and not ok:
                out["birthdays_note"] = "no name or relation matched; these are just the next ones by date"
            if tb and ok:
                for r in hit:           # tell the model how each row matched: "Yusuf" must not silently become "Yusufbek"
                    words = set(re.findall(r"\w+", r["name"].lower()))
                    r["match"] = ("name" if words & btoks
                                  else "relation" if _has(r["relation"], btoks) else "partial")
            if not tb:
                out["birthdays_total"] = len(rows)
            for r in hit[:5]:
                y = getattr(st.find_birthday(r["name"]), "year", None)
                if y:
                    r["birth_year"] = y
            return hit
        add("birthdays", bday)

    if want("tasks"):
        def task_rows():
            tt = task_times(agent.cfg)
            rows = [{"id": r[0], "title": r[1], "due": r[2], "time": tt.get(_norm(r[1]), "")} for r in st.open_tasks()]
            hit, ok = _filter(rows, toks, 25)
            return None if (strict and toks and not ok) else hit
        if kind == "tasks" or not (kind == "day" or dayq):
            add("open_tasks", task_rows)
        if dates:
            add("tasks_done", lambda: _done_on(agent, dates))

    if want("songs"):
        def songs():
            con = _song_db(agent.cfg)
            try:
                have = [r[1] for r in con.execute("pragma table_info(songs)")]
                cols = [c for c in ("id", "title", "artist", "instrument", "status", "priority") if c in have]
                rows = [dict(zip(cols, r)) for r in con.execute(f"select {', '.join(cols)} from songs")]
            finally:
                con.close()
            meta = song_meta(agent.cfg)
            for r in rows:
                t = str(r.get("title", ""))
                r["composer"] = (r.pop("artist", "") or meta.get(_norm(t))
                                 or meta.get(_norm(re.sub(r"\s*\([^)]*\)", "", t))) or "")
            hit, ok = _filter(rows, toks, 20)
            return None if (strict and not ok) else hit
        add("songs", songs)

    if want("practice"):
        def prac():
            if dates:
                return {str(d): _practice_on(agent, d) or "nothing logged" for d in dates}
            if strict and not (toks & {"practice", "practiced", "piano", "guitar", "played"}):
                return None
            return {"last_7_days": st.practice_summary(7)}
        add("practice", prac)

    if want("progress"):
        def prog():
            rows = project_context(agent, 300)
            low = both.lower()
            stems = [p.stem for p in (agent.data_dir / "projects").glob("*.md")]
            named = [x for x in stems if x in low or x.replace("-", " ") in low]
            if not named and "linda" in stems and (caps or (prog_intent and re.search(r"\b(you|your|linda)\b|тебя|тебе", low))):
                named = ["linda"]               # "what did I improve on you" = the Linda project
            if named:
                rows = [r for r in rows if any(f"[{x}]" in r for x in named)]
            if dates:
                return [r for r in rows if r.startswith(tuple(str(d) for d in dates))][:10] or "nothing logged"
            if prog_intent or not toks:         # a listing request: the user's verbs are not search words
                return rows[:10] or "nothing logged"
            hit, ok = _lines(rows, toks, 8)
            return None if (strict and not ok) else hit
        add("progress_log", prog)

    if want("journal"):
        def journal():
            if dates:
                return {str(d): _diary(agent, d) or "nothing recorded" for d in dates}
            if strict:
                return None
            jd = agent.data_dir / "journal"
            days = sorted((f.stem for f in jd.glob("2*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)), reverse=True)[:3]
            return {d: _lines(_diary(agent, d, 8), toks, 6)[0] for d in days}
        add("journal", journal)

    if kind in ("all", "notes") and toks and agent.docs and not dayq:
        add("notes", lambda: [{"file": pathlib.Path(h.path).name, "text": h.text[:600]}
                              for h in agent.docs.search(qq, 10, None)
                              if not pathlib.Path(h.path).name.startswith(("activity-", "root_"))][:3])

    if strict and set(out) <= {"today", "asked_dates"}:
        out["found"] = "nothing matched"
    return out


def _size(x):
    return len(json.dumps(x, ensure_ascii=False, default=str))


def _dump(out, budget):
    """Shrink the biggest section until it fits, so the JSON stays valid and no store is cut off whole."""
    out = dict(out)
    while _size(out) > budget:
        big = max((k for k, v in out.items() if isinstance(v, (list, dict)) and len(v) > 1),
                  key=lambda k: _size(out[k]), default=None)
        if big is None:
            break
        v = out[big]
        out[big] = v[:len(v) // 2] if isinstance(v, list) else dict(list(v.items())[:max(1, len(v) // 2)])
    return json.dumps(out, ensure_ascii=False, default=str)[:budget]


def recall_data(agent, a, user_text="", budget=2000):
    """Read-only. Returns JSON text for the model; never speaks."""
    kind, q = str(a.get("kind") or "all").lower(), str(a.get("query") or "")
    print(f"[recall] kind={kind} query={q!r}")
    res = _dump(_gather(agent, kind, q, user_text, use_text=False), budget)
    print(f"[recall] -> {len(res)} chars: {res[:160]}")
    return res


def prefetch(agent, text, budget=1500, think=False):
    """Retrieval first: search every store in code before the model sees the message.
    think=True (advice, planning, wishes): goals, ideas, profile, tasks, recent work, journal, practice, songs,
    plus the person's birthday row and profile lines when a name or relation is mentioned.
    Open tasks and ongoing projects are not fetched here: memory_index() shows them on every turn."""
    if reference.MEM_ASK.search(text):               # "what did I tell you to remember": read EVERYTHING stored
        try:
            out = _gather(agent, "memory", "", text, use_text=False)
            out["reference_files"] = reference.names(agent) or "none"
            res = _dump(out, max(budget, 3000))
        except Exception as e:
            print("[prefetch memory]", repr(e)[:120])
            return ""
        print(f"[prefetch memory] {len(res)} chars: {res[:120]}")
        return res
    if think:
        try:
            res = _dump(_gather(agent, "advice", "", text), max(budget, 2400))
        except Exception as e:
            print("[prefetch think]", repr(e)[:120])
            return ""
        print(f"[prefetch think] {len(res)} chars: {res[:120]}")
        return res
    if not LOOKUP.search(text):
        return ""
    try:
        out = _gather(agent, "all", "", text)
    except Exception as e:
        print("[prefetch]", repr(e)[:120])
        return ""
    if "found" in out:
        return ""
    res = _dump(out, budget)
    print(f"[prefetch] {len(res)} chars: {res[:120]}")
    return res