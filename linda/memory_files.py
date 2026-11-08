"""Voice-writable memory files: data/profile.md, data/goals.md, data/ideas.md.
Called from tools.py:   memory_files.handle(agent, tool_name, args)   for save_note / complete_goal / remove_note
Also:                   init(cfg) at startup, context() = the three files as plain text for the LLM prompt.
New entries go under their section heading (e.g. "## Yearly") or, without one, under "## Added by Linda",
in the format the UI already shows (ideas: "### Title" + "- **Status:** raw")."""
import datetime, pathlib, re, shutil, threading

try:
    from .config import ROOT
except ImportError:                                           # standalone use
    ROOT = pathlib.Path(".")

_dir = None
FILES = {"profile": "profile.md", "goals": "goals.md", "ideas": "ideas.md"}
DEFAULT = "Added by Linda"
PHRASES = {
    "saved":   {"en": "Saved.", "ru": "Сохранила.", "uz": "Saqlandi."},
    "exists":  {"en": "I already have that.", "ru": "Это уже записано.", "uz": "Bu allaqachon yozilgan."},
    "goal":    {"en": "Goal marked as done.", "ru": "Цель отмечена выполненной.", "uz": "Maqsad bajarildi deb belgilandi."},
    "removed": {"en": "Removed.", "ru": "Удалила.", "uz": "O'chirildi."},
    "nomatch": {"en": "I couldn't find a matching line.", "ru": "Не нашла подходящую строку.", "uz": "Mos qator topilmadi."},
}


def init(cfg):
    global _dir
    p = pathlib.Path(cfg["data_dir"])
    _dir = p if p.is_absolute() else ROOT / p


def _path(file):
    return (_dir or pathlib.Path("data")) / FILES[file]


def _kind(v):
    v = str(v or "").lower()
    return "ideas" if v.startswith("idea") else "goals" if v.startswith("goal") else "profile" if v.startswith("profile") else None


def _read(file):
    p = _path(file)
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


def _write(file, lines):
    p = _path(file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _words(s):
    return set(re.findall(r"\w{3,}", s.lower()))


def _level(line):
    m = re.match(r"(#{1,6})\s", line)
    return len(m.group(1)) if m else 0


def _insert(lines, section, new):
    """Append `new` lines at the end of the section whose heading contains `section` (created if missing)."""
    want = (section or DEFAULT).strip().lstrip("# ").casefold()
    start = next((i for i, l in enumerate(lines) if 0 < _level(l) <= 2 and want in l.lstrip("# ").casefold()), None)
    if start is None:
        return lines + ["", f"## {(section or DEFAULT).strip().lstrip('# ')}", ""] + new
    lvl, end = _level(lines[start]), len(lines)
    for j in range(start + 1, len(lines)):
        if 0 < _level(lines[j]) <= lvl:
            end = j
            break
    while end > start + 1 and not lines[end - 1].strip():      # keep the blank line before the next heading
        end -= 1
    return lines[:end] + new + ([""] if end < len(lines) and lines[end].strip() else []) + lines[end:]


def _title(text):
    head = text.split(":", 1)[0] if 0 < text.find(":") < 60 else " ".join(text.split()[:8])
    return head.strip(" .,!?").capitalize() or "Idea"


def _find(lines, query, file):
    """Index of the best matching entry (ideas: its ### heading; others: its bullet line)."""
    q, best, score = _words(query), None, 0
    for i, l in enumerate(lines):
        if file == "ideas":
            if not l.startswith("### "):
                continue
            block = " ".join(lines[i:next((j for j in range(i + 1, len(lines)) if _level(lines[j])), len(lines))])
        elif re.match(r"\s*([-*+]|\d+[.)])\s+", l):
            block = l
        else:
            continue
        s = len(q & _words(block))
        if s > score:
            best, score = i, s
    return best


def save(file, text, section=None):
    text = text.strip()
    lines = _read(file)
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    if norm and any(norm in re.sub(r"\W+", " ", l.lower()) for l in lines):
        return "already saved"
    if file == "ideas":
        new = ["", f"### {_title(text)}", "- **Status:** raw", "- **Potential:** medium",
               f"- **Added:** {datetime.date.today().isoformat()}", "", text]
    elif file == "goals":
        new = [f"- [ ] {text}"]
    else:
        new = [f"- {text}"]
    _write(file, _insert(lines, section, new))
    return "saved"


def complete(file, query):
    lines = _read(file)
    i = _find(lines, query, file)
    if i is None:
        return "no matching line"
    if file == "ideas":
        for j in range(i + 1, len(lines)):
            if _level(lines[j]):
                break
            if re.match(r"\s*-\s*\*\*Status:?\*\*", lines[j]):
                lines[j] = "- **Status:** implemented"
                break
    elif "[ ]" in lines[i]:
        lines[i] = lines[i].replace("[ ]", "[x]", 1)
    elif not re.search(r"\[[xX]\]", lines[i]):
        lines[i] = re.sub(r"^(\s*[-*+]\s+)", r"\1[x] ", lines[i])
    _write(file, lines)
    return "completed"


def remove(file, query):
    lines = _read(file)
    i = _find(lines, query, file)
    if i is None:
        return "no matching line"
    j = i + 1
    if file == "ideas":
        while j < len(lines) and not _level(lines[j]):
            j += 1
    del lines[i:j]
    _write(file, lines)
    return "removed"


def _after_write(file, docs):
    """Keep the search copy in data/notes in step and re-index in the background."""
    try:
        p = _path(file)
        notes = p.parent / "notes"
        if notes.is_dir():
            shutil.copy2(p, notes / f"root_{p.name}")
        if docs is not None:
            threading.Thread(target=docs.index, daemon=True).start()
    except Exception as e:
        print("[memory] reindex skipped:", e)


def run_tool(name, args, docs=None):
    """Returns "saved" | "already saved" | "completed" | "removed" | "no matching line"."""
    file = "goals" if name == "complete_goal" else _kind(args.get("file"))
    text = str(args.get("text") or "").strip()
    if not file or not text:
        return "no text"
    res = (complete(file, text) if name == "complete_goal" else remove(file, text) if name == "remove_note"
           else save(file, text, args.get("section")))
    if not res.startswith(("no ", "already")):
        _after_write(file, docs)
    return res


def handle(agent, name, args):
    """Run a memory tool and say the result in the user's language."""
    if name != "complete_goal" and _kind(args.get("file")) is None:
        return agent.k("error")
    result = run_tool(name, args, docs=getattr(agent, "docs", None))
    key = ("exists" if result == "already saved" else "nomatch" if result.startswith("no ")
           else {"save_note": "saved", "complete_goal": "goal", "remove_note": "removed"}[name])
    agent.say(PHRASES[key].get(agent.lang, PHRASES[key]["en"]), agent.lang)


def context_block(*args, **kw):
    """What brain.py puts into the prompt: the user's own files, or "" if they are empty."""
    c = context()
    return ("What you know about the user (from their own files profile.md, goals.md, ideas.md):\n" + c) if c else ""


def context(max_chars=3500):
    """Profile, goals and idea titles as plain text for the LLM prompt."""
    parts = []
    for file, label in (("profile", "About the user"), ("goals", "Goals"), ("ideas", "Ideas")):
        lines = _read(file)
        if file == "ideas":
            body = [l[4:].strip() for l in lines if l.startswith("### ")]
        else:
            body = [re.sub(r"^\s*[-*+]\s+", "", l).strip() for l in lines if re.match(r"\s*[-*+]\s+", l)]
        if body:
            parts.append(label + ":\n" + "\n".join("- " + b for b in body))
    return "\n\n".join(parts)[:max_chars]