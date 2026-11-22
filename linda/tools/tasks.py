"""Tasks and projects: the tasks table, data/tasks.md, project logs, finishing / adding tasks, argument helpers."""
import datetime
import difflib
import pathlib
import re
import sqlite3
import threading
import time
from linda.config import ROOT
from linda.i18n import day_label, fmt_date
from linda.router import Intent

# ===================== write handlers =====================

def open_tasks_text(agent, limit=40):
    """Open tasks with ids, today's first, so the model can match an update to a task by meaning."""
    today = str(datetime.date.today())
    rows = sorted(agent.store.open_tasks(), key=lambda r: (str(r[2] or "") != today, str(r[2] or "9999")))
    from linda.memory.recall import due_when
    return "; ".join(f"id {r[0]}: {r[1]}" + (f" (due {r[2]}, {due_when(r[2])})" if r[2] else "")
                     for r in rows[:limit]) or "none"


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
    md = sync_tasks_md(agent)
    proj, _ = _existing_project(agent, project)
    # which md files show this task, so the Telegram reply can list them (read by briefing.added_files)
    agent.added_files = {**(getattr(agent, "added_files", None) or {}), new: [f for f in (md, proj) if f]}
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


# names from the sibling modules (imported last, so mutual use works)
from linda.tools.registry import LANG_WORDS  # noqa: E402
from linda.memory.recall import _done_on  # noqa: E402


# ---------------------------------------------------------------- grouped task lists (/tasks, reminders, review)
import zoneinfo  # noqa: E402

TZ_NAMES = {"cet": "Europe/Berlin", "cest": "Europe/Berlin", "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
            "msk": "Europe/Moscow", "moscow": "Europe/Moscow", "utc": "UTC", "gmt": "UTC", "eet": "Europe/Helsinki",
            "eest": "Europe/Helsinki", "tashkent": "Asia/Tashkent", "uzt": "Asia/Tashkent", "kst": "Asia/Seoul",
            "jst": "Asia/Tokyo", "est": "America/New_York", "edt": "America/New_York", "pst": "America/Los_Angeles"}
DEADLINE = re.compile(r"\s*\(?\s*(?:deadline|until|due|muddat|срок|до)\s*:?\s*(\d{1,2})[./](\d{1,2})[./](\d{4})"
                      r"(?:\s*,?\s*(\d{1,2}):(\d{2}))?\s*([A-Za-z]{2,8})?\s*\)?", re.I)


def local_tz(cfg):
    return zoneinfo.ZoneInfo(cfg.get("timezone") or (cfg.get("bot_stats") or {}).get("timezone") or "Asia/Tashkent")


def split_deadline(title, cfg):
    """'Apply: X (deadline 31.01.2027 12:00 CET)' -> ('Apply: X', datetime in local time, True if a time was given).
    No deadline in the title -> (title, None, False). An unknown time zone is taken as local time."""
    m = DEADLINE.search(title)
    if not m:
        return title, None, False
    d, mo, y, h, mi, zone = m.groups()
    try:
        tz = zoneinfo.ZoneInfo(TZ_NAMES.get((zone or "").lower(), "")) if zone and (zone or "").lower() in TZ_NAMES \
            else local_tz(cfg)
        dt = datetime.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), tzinfo=tz).astimezone(local_tz(cfg))
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return title, None, False
    clean = (title[:m.start()] + title[m.end():]).strip(" -–,;")
    return clean or title, dt, bool(h)


def task_groups(agent, today=None, rows=None):
    """[(header, [line, ...])]: 'Today' (no date, due today, overdue) first, then one group per date.
    A deadline written in the title (with a time zone) is shown in local time in the group header."""
    today = today or datetime.date.today()
    tt = task_times(agent.cfg)
    groups = {}                                          # date or None -> {"lines": [], "time": None}
    for r in (rows if rows is not None else agent.store.open_tasks()):
        title, dl, has_time = split_deadline(r[1], agent.cfg)
        try:
            due = datetime.date.fromisoformat(str(r[2])[:10]) if r[2] else None
        except ValueError:
            due = None
        if dl is not None:
            due = dl.date()
        key = None if (due is None or due <= today) else due
        g = groups.setdefault(key, {"lines": [], "time": None})
        at = tt.get(_norm(r[1]), "")
        extra = [f"⚠️ overdue since {due:%d.%m}" if due and due < today else "", at]
        g["lines"].append((title, " · ".join(x for x in extra if x)))
        if has_time:
            g["time"] = dl if g["time"] is None else min(g["time"], dl)
    out = []
    for key in sorted(groups, key=lambda k: (k is not None, k or today)):
        g = groups[key]
        if key is None:
            head = "Today"
        elif g["time"] is not None:
            head = f"Until {g['time']:%d.%m.%Y %H:%M} ({str(local_tz(agent.cfg)).split('/')[-1]})"
        else:
            head = f"{key:%d.%m.%Y} ({key:%a})" + (" · tomorrow" if key == today + datetime.timedelta(days=1) else "")
        out.append((head, g["lines"]))
    return out
