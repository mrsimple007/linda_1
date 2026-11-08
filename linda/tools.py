"""Every skill as a Gemini function declaration. The LLM calls them directly: this replaces the intent router.
Handlers are the existing Agent.do_<intent> methods (they speak for themselves)."""
import datetime
from google.genai import types
from .i18n import day_label
from .router import Intent
import re
from .i18n import day_label, fmt_date
import difflib, sqlite3, pathlib 
from .config import ROOT
import time
import threading

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
    ("list_birthdays", "List upcoming birthdays.", {}, [], {}),
    ("delete_birthday", "Delete a saved birthday.", {"name": (S, "person")}, ["name"], {}),
    ("birthday_greeting", "Write a birthday greeting and send it to the user's Telegram so they can forward it.",
     {"name": (S, "person"), "relation": (S, "relation"), "language": (S, "en, ru or uz")}, ["name"], {}),
    ("calendar_add", "Add an event to the calendar.",
     {"title": (S, "short title in the user's language, no date words"),
      "start": (S, "local ISO datetime, e.g. 2026-10-28T15:00 (09:00 if no time said)"),
      "duration_minutes": (I, "default 60")}, ["title", "start"], {}),
    ("calendar_agenda", "Read what is on the calendar.", {"when": (S, "today, tomorrow or week")}, [], {}),
    ("calendar_undo", "Remove the calendar event that was just added.", {}, [], {}),
    ("telegram_send", "Send something to the user's own Telegram.",
     {"text": (S, "message text; empty means send the last answer or file")}, [], {}),
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
    ("practice_log", "Log a practice session the user just did.",
     {"minutes": (I, "duration"), "name": (S, "song, if said"), "instrument": (S, "piano or guitar"),
      "rating": (I, "self-rating 1-5 if said"), "focus": (S, "what was practiced")}, ["minutes"],
     {"instrument": "action", "rating": "score", "focus": "text"}),
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
    ("task_add", "Add a to-do item. If the user says today or tomorrow, put that real date in due.",
     {"title": (S, "the task"),
      "due": (S, "due date YYYY-MM-DD if said"),
      "time": (S, "time of day HH:MM if said"),
      "project": (S, "project if said")}, ["title"], {}),    ("task_list", "List the open tasks.", {}, [], {}),
    ("task_done", "Mark a task as done. Pick it from the open tasks list by meaning and pass its id.",
     {"id": (I, "id from the open tasks list"), "text": (S, "words from the task, only if you have no id")}, [], {}),
    ("song_add", "Add a song or piece to the repertoire. FIRST work out the proper title and the composer or artist from your own knowledge, then save with full details. Never save a bare guess.",
     {"name": (S, "proper title, e.g. 'Game of Thrones (Main Theme)'"), "composer": (S, "composer or artist, e.g. 'Ramin Djawadi'"),
      "instrument": (S, "piano or guitar"), "status": (S, "learning, polishing or repertoire"),
      "notes": (S, "one short line (difficulty, key, arrangement) if known")}, ["name"], {}),
    ("music", "Control Spotify. action=play with query plays that specific song or artist (e.g. query 'The Nights Avicii'); action=play without query resumes; also pause, next, previous. ONLY when the user explicitly asks for music.",
     {"action": (S, "play, pause, next or previous"), "query": (S, "song and artist to play")}, ["action"], {"query": "text"}),
    ("journal_add", "Personal diary only: feelings, thoughts, reflections. NOT for work done or things improved (use log_progress).",
     {"text": (S, "the entry")}, ["text"], {}),    ("search_notes", "Search the user's OWN notes, papers, project docs and journal.",
     {"query": (S, "the question"), "source": (S, "notes, papers, projects or journal if said")}, ["query"],
     {"query": "text", "source": "topic"}),
    ("stats", "Progress and statistics.", {"topic": (S, "practice, chess, driving or tasks; empty for everything"), **TG}, [], {}),
    ("web_answer", "Answer a question that needs CURRENT information from the web (news, sports, prices, 'latest', 'today').",
     {"query": (S, "the question"), **TG}, ["query"], {}),
    ("save_note", "Save something the user said to a personal file. file=profile: lasting facts about the user (languages, schedule, instruments, health, preferences, people). file=goals: targets and ambitions (yearly/monthly/weekly). file=ideas: random thoughts and project ideas to keep. Tasks with a due date go to task_add, diary entries to journal_add.",
     {"file": (S, "profile, goals or ideas"),
      "text": (S, "the note itself, written out in full from the conversation (replace 'it', 'that', 'about it' with what they refer to). Never the instruction like 'write this to ideas'"),
      "section": (S, "optional heading such as Yearly, Monthly, Weekly, Music")}, ["file", "text"], {}),
    ("complete_goal", "Mark a goal in goals.md as done.", {"text": (S, "a few words from the goal")}, ["text"], {}),
    ("remove_note", "Delete an outdated or wrong line from profile, goals or ideas.",
     {"file": (S, "profile, goals or ideas"), "text": (S, "words from the line to delete")}, ["file", "text"], {}),
    ("log_progress", "Record in the permanent project log what the user worked on, finished or improved (a project, a feature of Linda, studies). Write one clear full sentence in English: what was done and what is now possible. Use whenever the user says they worked on, finished or improved something. If it finishes an open task, also call task_done.",
     {"project": (S, "project name, e.g. Linda, recsys, driving licence"),
      "text": (S, "what was done and what is now possible")}, ["project", "text"], {}),
    ("practice_log", "Log a practice session the user did. date is the real YYYY-MM-DD if it was not today ('yesterday' = yesterday's actual date).",
     {"minutes": (I, "duration in minutes, exactly as said"), "name": (S, "song, if said"), "instrument": (S, "piano or guitar"),
      "date": (S, "YYYY-MM-DD if not today"), "rating": (I, "self-rating 1-5 if said"), "focus": (S, "what was practiced")},
     ["minutes"], {}),
    ("read_note", "Read out the user's saved profile, goals or ideas, or answer questions about them ('what are my goals').",
     {"file": (S, "profile, goals or ideas")}, ["file"], {}),
    ("birthday_lookup", "Tell when a specific person's birthday is. 'my birthday' means the user's own.",
     {"name": (S, "person, or 'me'")}, [], {}),

]
# Tools that WRITE data run one after another in the order requested; the rest can run in parallel.
SEQUENTIAL = {"add_birthday", "delete_birthday", "calendar_add", "calendar_undo", "remember", "song_add", "practice_log",
              "log_driving", "task_add", "task_done", "journal_add", "timer", "chess", "quiz", "mac", "volume", "music","save_note", "complete_goal", "remove_note"}
SEQUENTIAL.update({"process_download", "clipboard", "telegram_send", "birthday_greeting"})
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

def _log_progress(agent, a, text):
    project, entry = str(a.get("project") or "General").strip(), str(a.get("text") or "").strip()
    if not entry:
        return agent.k("error")
    p = agent.data_dir / "projects" / (re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-") + ".md")
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

def _norm(s):
    return re.sub(r"[^\w ]+", "", str(s).lower()).strip()

def _tt_db(cfg):
    p = pathlib.Path(cfg["db_path"]); p = p if p.is_absolute() else ROOT / p
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


def add_task_once(agent, title, due=None, project="", at=""):
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
            con.execute("insert or replace into task_times values (?,?)", (new, at)); con.commit()
        finally:
            con.close()
    agent.k("task_saved", title=title)
    return True

def _task_add(agent, a, text):
    due = a.get("due") or None
    if due:
        try:
            datetime.date.fromisoformat(str(due))
        except ValueError:
            due = None
    add_task_once(agent, str(a["title"]), due, str(a.get("project") or ""), str(a.get("time") or ""))


def _web_answer(agent, a, text):
    q = str(a.get("query") or text)
    agent._out(reply=agent._stream_say(agent.gemini.stream(q, agent._chat_system(), grounding=True), agent.lang))

from . import memory_files


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
    b = next((x for x in agent.store.upcoming(366)
              if (_is_me(agent, x) if own else who in x.name.lower())), None)
    if not b:
        return agent.say({"en": "I don't have that birthday yet. Tell me: my birthday is, and the date.",
                          "ru": "Этого дня рождения у меня ещё нет. Скажи дату.",
                          "uz": "Bu tug'ilgan kun menda yo'q. Sanani ayting."}[agent.lang])
    d = fmt_date(b.date, agent.lang)
    agent.say({"en": f"Your birthday is {d}, in {b.n} days." if own else f"{b.name}: {d}, in {b.n} days.",
               "ru": f"Твой день рождения {d}, через {b.n} дн." if own else f"{b.name}: {d}, через {b.n} дн.",
               "uz": f"Tug'ilgan kuningiz {d}, {b.n} kundan keyin." if own else f"{b.name}: {d}, {b.n} kundan keyin."}[agent.lang])
    

SPECIAL = {
    "calendar_add": _calendar_add, "task_add": _task_add, "web_answer": _web_answer,
    "save_note": _mem("save_note"), "complete_goal": _mem("complete_goal"), "remove_note": _mem("remove_note"),
    "read_note": _read_note, "birthday_lookup": birthday_lookup,
}


SPECIAL = {"calendar_add": _calendar_add, "task_add": _task_add, "web_answer": _web_answer,
           "save_note": _mem("save_note"), "complete_goal": _mem("complete_goal"), "remove_note": _mem("remove_note")}
SPECIAL.update({"read_note": _read_note, "birthday_lookup": birthday_lookup})

def _backdate(cfg, day):
    """Move the newest practice row to another day. Works out the table and column itself."""
    con = _tt_db(cfg)
    try:
        tbl = next((r[0] for r in con.execute("select name from sqlite_master where type='table' and name like '%practice%'")), None)
        cols = {r[1] for r in con.execute(f"pragma table_info({tbl})")} if tbl else set()
        col = next((c for c in ("ts", "day", "date", "created", "logged_at", "timestamp") if c in cols), None)
        if not col:
            print("[practice] cannot backdate: no date column found in", tbl, sorted(cols))
            return
        rid = con.execute(f"select max(rowid) from {tbl}").fetchone()[0]
        old = con.execute(f"select {col} from {tbl} where rowid=?", (rid,)).fetchone()[0]
        if isinstance(old, (int, float)):
            new = datetime.datetime.combine(day, datetime.time(12)).timestamp()
        else:
            new = f"{day}{str(old)[10:]}" if len(str(old)) > 10 else str(day)
        con.execute(f"update {tbl} set {col}=? where rowid=?", (new, rid))
        con.commit()
        print(f"[practice] backdated {tbl}.{col} -> {new}")
    finally:
        con.close()


def _practice_log(agent, a, text):
    try:
        minutes = int(float(a.get("minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return agent.k("need_minutes")
    if minutes > 480:
        return agent.say(f"That is {minutes} minutes. How many minutes did you mean?")
    name, instr = str(a.get("name") or ""), str(a.get("instrument") or "").lower()
    song = agent.store.find_song(name, instr) if name else None
    try:
        r = int(float(a.get("rating") or 0))
    except (TypeError, ValueError):
        r = 0
    agent.store.log_practice(minutes, song.id if song else None, song.instrument if song else instr,
                             str(a.get("focus") or ""), r if 1 <= r <= 5 else None)
    try:
        day = datetime.date.fromisoformat(str(a.get("date") or ""))
        if day != datetime.date.today():
            _backdate(agent.cfg, day)
    except ValueError:
        pass
    week = agent.store.practice_summary(7)["total_minutes"]
    if song:
        agent.k("practice_logged_song", minutes=minutes, title=song.title, week=week)
    else:
        agent.k("practice_logged", minutes=minutes, week=week)


SPECIAL["practice_log"] = _practice_log


SPECIAL["log_progress"] = _log_progress
SEQUENTIAL.add("log_progress")

def _task_done(agent, a, text):
    tid, words, title = a.get("id"), str(a.get("text") or "").strip(), None
    if tid not in (None, ""):
        con = _tt_db(agent.cfg)
        try:
            n = int(float(tid))
            row = con.execute("select title from tasks where id=? and status='open'", (n,)).fetchone()
            if row:
                con.execute("update tasks set status='done', done_ts=? where id=?", (time.time(), n))
                con.commit()
                title = row[0]
        finally:
            con.close()
    elif words:
        title = agent.store.complete_task(words)
    agent.k("task_done", title=title) if title else agent.k("task_notfound")


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
    existing = agent.store.find_song(name, instr)
    if existing:
        name = existing.title
    else:
        agent.store.upsert_song(name, instr, status=status)
    if comp or notes:
        con = _song_db(agent.cfg)
        try:
            con.execute("insert or replace into song_meta values (?,?,?)", (_norm(name), comp, notes))
            con.commit()
        finally:
            con.close()
    agent.k("song_saved", title=name + (f" by {comp}" if comp else ""), instrument=instr)


SPECIAL.update({"task_done": _task_done, "song_add": _song_add})


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



import re

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


def birthday_lookup(agent, a, text):
    name = str(a.get("name") or "").strip()
    if name.lower() in ("", "me", "my", "mine", "i"):
        name = agent.cfg.get("user_name") or ""
    items = agent.store.upcoming(366)
    b = next((x for x in items if (name and name.lower() in x.name.lower()) or (not name and (x.relation or "").lower() == "me")), None)
    if not b:
        return agent.say("I don't have that birthday yet. Tell me: my birthday is, and the date.")
    agent.say(f"{b.name}: {fmt_date(b.date, agent.lang)}, in {b.n} days.")


# ===================== memory read tools (recall) =====================
import json


def _tok(s):
    return [w for w in re.findall(r"\w+", str(s).lower()) if len(w) > 2]


def _filter(rows, q):
    toks = _tok(q)
    if not toks:
        return rows[:60], True
    hit = [r for r in rows if any(t in " ".join(map(str, r.values())).lower() for t in toks)]
    return (hit[:60], True) if hit else (rows[:60], False)


def song_meta_count(cfg):
    con = _song_db(cfg)
    try:
        return con.execute("select id from songs").fetchall()
    finally:
        con.close()


def recall_data(agent, a):
    """Read-only. Returns JSON text for the model; never speaks."""
    kind, q = str(a.get("kind") or "all").lower(), str(a.get("query") or "")
    print(f"[recall] kind={kind} query={q!r}")
    want = lambda k: kind in ("all", k)
    out, st = {}, agent.store
    out["today"] = f"{datetime.date.today():%A %Y-%m-%d}"

    def safe(label, fn):
        try:
            out[label] = fn()
        except Exception as e:
            print(f"[recall {label}] {e!r}"[:200])

    if want("birthdays"):
        def birthdays():
            rows = []
            for b in st.upcoming(366):
                p = st.find_birthday(b.name)
                rows.append({"name": b.name, "birthday": f"{b.date:%b} {b.date.day}", "in_days": b.n,
                             "relation": b.relation or "", "birth_year": getattr(p, "year", None)})
            res, exact = _filter(rows, q)
            if not exact:
                out["birthdays_note"] = "no name matched the query: this is the full list"
            if not q.strip():
                out["birthdays_total"] = len(rows)
                res = rows[:12]
            return res
        safe("birthdays", birthdays)
    if want("tasks"):
        def task_rows():
            tt = task_times(agent.cfg)
            rows = [{"id": r[0], "title": r[1], "due": r[2], "time": tt.get(_norm(r[1]), "")} for r in st.open_tasks()]
            return _filter(rows, q)[0]
        safe("open_tasks", task_rows)
    if want("songs"):
        def songs():
            con = _song_db(agent.cfg)
            try:
                have = [r[1] for r in con.execute("pragma table_info(songs)")]
                cols = [c for c in ("id", "title", "instrument", "status", "priority") if c in have]
                rows = [dict(zip(cols, r)) for r in con.execute(f"select {', '.join(cols)} from songs")]
            finally:
                con.close()
            meta = song_meta(agent.cfg)
            for r in rows:
                t = str(r.get("title", ""))
                r["composer"] = meta.get(_norm(t)) or meta.get(_norm(re.sub(r"\s*\([^)]*\)", "", t))) or ""
            return _filter(rows, q)[0]
        safe("songs", songs)
    if want("practice"):
        safe("practice", lambda: {"last_7_days": st.practice_summary(7), "last_30_days": st.practice_summary(30)})
    for f in ("profile", "goals", "ideas"):
        p = agent.data_dir / f"{f}.md"
        if want(f) and p.exists():
            safe(f, lambda p=p: [l.strip("-*+ ").strip() for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
                                 if l.strip() and not l.lstrip().startswith(("<!--", "#"))][:60])
    if want("progress"):
        safe("progress_log", lambda: project_context(agent, 15))
    if want("journal"):
        def journal():
            files = sorted([f for f in (agent.data_dir / "journal").glob("2*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)],
                           reverse=True)[:7]
            return [{"date": f.stem, "weekday": datetime.date.fromisoformat(f.stem).strftime("%A"),
                     "entries": f.read_text(encoding="utf-8", errors="ignore").splitlines()[-15:]} for f in files]
        safe("journal", journal)
    if kind in ("all", "notes") and q and agent.docs:
        safe("notes", lambda: [{"file": pathlib.Path(h.path).name, "text": h.text[:300]} for h in agent.docs.search(q, 4, None)])
    res = json.dumps(out, ensure_ascii=False, default=str)[:7000]
    print(f"[recall] -> {len(res)} chars: {res[:160]}")
    return res


def memory_index(agent):
    """A few lines for the prompt: what exists, not the data itself (the model reads it with recall)."""
    st, lines = agent.store, []
    if agent.cfg.get("user_name"):
        lines.append(f"user: {agent.cfg['user_name']}")

    def birthdays():
        bd = st.upcoming(366)
        n = min(bd, key=lambda b: b.n)
        return f"{len(bd)} saved; next is {n.name} in {n.n} days"

    for label, fn in (
        ("birthdays", birthdays),
        ("open tasks", lambda: str(len(st.open_tasks()))),
        ("songs", lambda: str(len(song_meta_count(agent.cfg)))),
        ("practice, last 7 days", lambda: f"{st.practice_summary(7)['total_minutes']} min"),
        ("progress log entries", lambda: str(len(project_context(agent, 1000)))),
    ):
        try:
            lines.append(f"{label}: {fn()}")
        except Exception as e:
            print(f"[index {label}]", repr(e)[:100])
    lines.append("md notes: profile, goals, ideas; diary: journal (last 7 days with dates); searchable: notes, papers, projects, journal")
    return "\n".join(lines)


def export_birthdays(agent):
    """Keep birthdays.md a mirror of the database, in the format the importer reads back."""
    p = agent.data_dir / "birthdays.md"
    keep = ""
    if p.exists():
        txt = p.read_text(encoding="utf-8", errors="ignore")
        bak = p.with_suffix(".md.bak")
        if not bak.exists():
            bak.write_text(txt, encoding="utf-8")                      # your original file, kept once
        m = re.search(r"^greeting_language:.*$", txt, re.M)
        keep = (m.group(0) + "\n\n") if m else ""
    lines = ["# Birthdays", "", "<!-- Generated from linda.db after each change. Edit here or by voice: both stay in sync. -->", "",
             keep + "| Name | Date | Relation |", "|---|---|---|"]
    for b in sorted(agent.store.upcoming(366), key=lambda b: b.name.lower()):
        y = getattr(agent.store.find_birthday(b.name), "year", None)
        lines.append(f"| {b.name} | {f'{y}-' if y else ''}{b.date.month:02d}-{b.date.day:02d} | {b.relation or ''} |")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")