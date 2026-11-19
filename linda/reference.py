"""Reference notes: longer material you dictate (course outlines, lesson lists, resources, schedules) saved in FULL
as data/notes/<topic>.md, plus helpers that tell you WHERE something was saved and recognise "what do you remember?".

Pure standard library: no imports from the rest of Linda, so tools.py / brain.py / briefing.py can all import it.
"""
import datetime
import difflib
import re
import threading

# a READ question about what is stored: must never trigger a save
MEM_ASK = re.compile(
    r"what (?:did|have) (?:i|we) (?:tell|told|ask|asked|say|said)\w*(?: you| u)?(?: to)? (?:remember|save|note)|"
    r"what (?:do|did) (?:you|u) (?:remember|know about me|save|store)|what (?:are|r) (?:ur|your) things|"
    r"what (?:is|are) (?:saved|stored|remembered)|what (?:i|we) told (?:you|u)|"
    r"(?:show|list|read)(?: me)?(?: all)? (?:my |the )?(?:notes|memories|remembered)|"
    r"что я (?:тебе )?(?:просил\w*|говорил\w*) запомнить|что ты (?:помнишь|запомнил\w*)|"
    r"nimalarni eslab|nimani eslab|nimalar saqlangan", re.I)

TG_RULES_EXTRA = (
    " Longer information the user dictates (course outlines, lesson or topic lists, schedules, resources, how-tos, "
    "checklists) must be SAVED with save_reference: topic = the subject (for example 'IT course'), text = the user's "
    "full text copied exactly, keeping its lines and structure (never a summary). Never answer 'got it' without a tool "
    "call. 'Remember that ...' about a future project, plan or idea goes to save_note file=ideas; about the user's own "
    "life or facts to file=profile; only tiny one-off reminders use remember. A question about what is stored or "
    "remembered ('what did I tell you to remember', 'what do you know about X') is a READ: answer from the stored data "
    "(notes and reference_files) and NEVER call remember, save_note or save_reference for it. To show a saved long "
    "note use read_reference."
    " Facts about an ongoing course or project (a student, a start date, a rule, a resource) go to save_reference "
    "with that course or project as topic (for example 'IT course'), never to profile. "
    "One message can need several tools: an event ahead ('class at 18:00') is task_add, and preparation already "
    "finished ('presentation and tasks are ready') is ALSO log_progress for that project, in the same turn."
    " To delete or correct something stored, ALWAYS call remove_note (file + a few words from the line) and let the "
    "tool decide whether it exists; never say 'not found' yourself. Treat near-identical names (Xolid / Xolida) as "
    "the same person, a typo."
    )

NUDGE_REF = (" You answered without saving anything. If this message is information worth keeping (an outline, a list "
             "of topics or lessons, a schedule, resources), call save_reference now with topic = the subject and "
             "text = the user's full text, copied exactly. If it is only a question or chat, answer normally.")


def looks_like_reference(text):
    t = (text or "").strip()
    if not t or t.endswith("?") or MEM_ASK.search(t):
        return False
    return len(t) >= 200 or t.count("\n") >= 3


def _slug(topic):
    return re.sub(r"[\W_]+", "-", str(topic).lower(), flags=re.U).strip("-")


def _flat(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def ref_files(agent):
    d = agent.data_dir / "notes"
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.md") if not p.name.startswith(("root_", "activity-")))


def rel(agent, p):
    try:
        return "data/" + p.relative_to(agent.data_dir).as_posix()
    except ValueError:
        return str(p)


def names(agent, limit=15):
    return ", ".join(p.stem for p in ref_files(agent)[:limit])


def listing(agent):
    return [(rel(agent, p), len(p.read_text(encoding="utf-8", errors="ignore"))) for p in ref_files(agent)]


def _stem(topic):
    s = re.sub(r"(?:-notes?)+$", "", _slug(topic))
    return (s or "notes") + "-notes"


def find(agent, topic, loose=True):
    """The saved note that best matches a topic. Files are named <topic>-notes.md; matching ignores the suffix."""
    by = {p.stem.removesuffix("-notes"): p for p in ref_files(agent)}
    q = re.sub(r"(?:-notes?)+$", "", _slug(topic))
    if not q or not by:
        return None
    if q in by:
        return by[q]
    inside = [s for s in by if q in s or s in q]
    if inside:
        return by[min(inside, key=len)]
    close = difflib.get_close_matches(q, list(by), n=1, cutoff=0.85 if not loose else 0.7)
    if close:
        return by[close[0]]
    if loose:
        words = set(q.split("-"))
        best = max(by, key=lambda s: len(words & set(s.split("-"))))
        if words & set(best.split("-")):
            return by[best]
    return None


def _say(agent, en, ru=None, uz=None):
    agent.say({"en": en, "ru": ru or en, "uz": uz or en}.get(getattr(agent, "lang", "en"), en))


def save_reference(agent, a, text):
    topic, body = str(a.get("topic") or "").strip(), str(a.get("text") or "").strip()
    if not topic or len(body) < 15:
        return agent.say("error: save_reference needs a topic and the user's full text.")
    d = agent.data_dir / "notes"
    p = find(agent, topic, loose=False) or d / f"{_stem(topic)}.md"
    d.mkdir(parents=True, exist_ok=True)
    old = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
    if _flat(body) in _flat(old):
        return _say(agent, f"That is already saved in {rel(agent, p)}.",
                    f"Это уже сохранено в {rel(agent, p)}.", f"Bu allaqachon {rel(agent, p)} da saqlangan.")
    with p.open("a", encoding="utf-8") as f:
        if not old:
            f.write(f"# {topic}\n\n")
        f.write(f"## Added {datetime.date.today()}\n\n{body}\n\n")
    if getattr(agent, "docs", None):
        def _idx():
            try:
                agent.docs.index_file(p, "notes")
            except Exception as e:
                print("[index]", e)
        threading.Thread(target=_idx, daemon=True).start()
    _say(agent, f"Saved in full to {rel(agent, p)}.", f"Сохранила полностью в {rel(agent, p)}.",
         f"To'liq {rel(agent, p)} ga saqladim.")


def read_reference(agent, a, text):
    topic = str(a.get("topic") or "").strip()
    p = find(agent, topic) if topic else None
    if p is None:
        files = listing(agent)
        if not files:
            return agent.say("No reference notes are saved yet.")
        return agent.say(f"error: no saved note matches '{topic}'. Saved notes: " + ", ".join(r for r, _ in files))
    body = p.read_text(encoding="utf-8", errors="ignore").strip()
    more = (f"\n[cut here: {len(body) - 3500} more characters. The user can open the whole file with /notes {p.stem}]"
            if len(body) > 3500 else "")
    agent.say(f"[{rel(agent, p)}]\n{body[:3500]}{more}")


def where_saved(agent, name, args):
    """Where a tool call put its data, as a short readable string ('' if unknown)."""
    if agent is None:
        return ""
    d, today = agent.data_dir, datetime.date.today()
    try:
        if name == "log_progress":
            names_ = {p.stem: p for p in (d / "projects").glob("*.md")} if (d / "projects").exists() else {}
            slug = re.sub(r"[^a-z0-9]+", "-", str(args.get("project") or "general").lower()).strip("-") or "general"
            p = names_.get(slug)
            if p is None:
                close = difflib.get_close_matches(slug, list(names_), n=1, cutoff=0.85)
                p = names_[close[0]] if close else d / "projects" / f"{slug}.md"
            return rel(agent, p) if p.exists() else ""
        if name == "save_note":
            f = str(args.get("file") or "").lower()
            return rel(agent, d / f"{f}.md") if f in ("profile", "goals", "ideas") else ""
        if name == "save_reference":
            p = find(agent, args.get("topic") or "", loose=False)
            return rel(agent, p) if p else ""
        if name == "journal_add":
            return rel(agent, d / "journal" / f"{today}.md")
        if name == "remember":
            return "linda.db, quick notes (see /notes)"
        if name in ("practice_log", "song_add"):
            return "linda.db"
    except Exception as e:
        print("[where_saved]", repr(e)[:100])
    return ""