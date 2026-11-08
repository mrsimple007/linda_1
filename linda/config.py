"""Loads config.yaml and fills every missing setting from built-in defaults,
so an older or hand-edited config.yaml can never crash Linda."""
import copy, pathlib, re, yaml
from .util import parse_date

ROOT = pathlib.Path(__file__).resolve().parents[1]

QUIZ_PROMPT = """Rewrite the attached quiz document as plain text with NO tables.
Use exactly this format for every question, with one blank line between questions:

1. Question text
+ Right answer
- Wrong answer
- Wrong answer
- Wrong answer

Rules:
- Keep the original numbering, language and wording. Do not translate, shorten or reword. Fix only obvious spacing glitches.
- The right answer may be marked by **bold**, ==highlight==, ^^color^^, a check mark or + sign, or by a separate answer key or column. Find it carefully. The right answer is always printed first, with "+".
- Remove all formatting markers and the original answer letters or bullets.
- If a question has more or fewer than 3 wrong answers, keep exactly what the document has.
- If you truly cannot tell the right answer, print "+ ???" first.
- Output EVERY question. Never summarize, skip or add commentary.
"""

DEFAULTS = {
    "name": "Linda", "user_name": "", "default_language": "en", "city": "Tashkent",
    "downloads_dir": "~/Downloads", "data_dir": "data", "db_path": "linda.db", "router": "local",
    "local": {"model": "gemma4:e2b-it-qat", "keep_alive": "30m"},
    "gemini": {"model": "gemini-3.8-flash", "brain_model": "", "minimal_thinking": True,
               "max_output_tokens": 32000, "grounding_for_chat": True},
    "brain": {"history_turns": 8},
    "server": {"host": "127.0.0.1", "port": 4700, "open_browser": True, "browser": "Google Chrome", "app_mode": True},
    "stt": {"model": "mlx-community/whisper-large-v3-turbo", "refine_with_gemini": True},
    "tts": {"engine": "edge", "edge_rate": "+10%", "say_rate": 190, "voices": {
        "en": {"edge": "en-US-AriaNeural", "say": "Samantha"},
        "ru": {"edge": "ru-RU-SvetlanaNeural", "say": "Milena"},
        "uz": {"edge": "uz-UZ-MadinaNeural", "say": "Samantha"}}},
    "audio": {"sample_rate": 16000, "vad_aggressiveness": 2, "silence_ms": 700, "conversation_window_s": 20,
              "wake_pattern": "(lin+d[aeoh]|lynd[ae]|linder|линд[аыуео])",
              "input_device": "", "music_device": ""},
    "goals": {"driving_test_date": "", "instruments": ["piano", "guitar"], "weekly_practice_minutes": 150},
    "practice": {"default_minutes": 30, "stale_days": 14},
    "analysis": {"record_seconds": 20},
    "quiz": {"questions_per_session": 10, "language": "uz"},
    "docs": {"embed_model": "nomic-embed-text", "folders": ["notes", "papers", "projects", "journal"],
             "answer_with": "gemini", "top_k": 5},
    "schedule": [
        {"id": "birthdays", "at": "09:00", "action": "birthdays", "grace_h": 12},
        {"id": "morning", "at": "07:30", "action": "morning_briefing", "grace_h": 2, "enabled": False},
        {"id": "practice", "at": "18:30", "action": "practice_reminder", "grace_h": 3},
        {"id": "evening", "at": "21:30", "action": "evening_checkin", "grace_h": 3}],
    "birthdays": {"notify": True, "greeting_language": "uz"},
    "calendar": {"enabled": True, "remind_minutes": 10},
    "morning": {"news": True, "weather": True},
    "chess": {"skill_level": 5, "think_time": 0.3, "window": True},
    "presets": {"quiz_txt": {
        "description": "rewrite a quiz/test document (tables with questions and answers) as plain text: "
                       "numbered question, + right answer, - wrong answers",
        "prompt": QUIZ_PROMPT}},
}


def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def _abs(p):
    p = pathlib.Path(p).expanduser()
    return str(p if p.is_absolute() else ROOT / p)


JOB_ACTIONS = {"birthdays": "birthdays", "morning": "morning_briefing", "practice": "practice_reminder",
               "evening": "evening_checkin"}


def _fix_schedule(jobs):
    out = []
    for j in jobs or []:
        j = dict(j)
        j["action"] = j.get("action") or JOB_ACTIONS.get(j.get("id"), j.get("id"))
        if j["action"] not in JOB_ACTIONS.values():
            print(f"[config] schedule job {j.get('id')!r}: unknown action {j['action']!r}, skipped "
                  f"(use one of: {', '.join(JOB_ACTIONS.values())})")
            continue
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(j.get("at", ""))):
            print(f"[config] schedule job {j.get('id')!r}: 'at' must be a quoted time like \"18:30\", skipped")
            continue
        j.setdefault("id", j["action"])
        j.setdefault("grace_h", 3)
        out.append(j)
    return out


def _fix_date(cfg):
    raw = cfg["goals"].get("driving_test_date")
    if not raw:
        cfg["goals"]["driving_test_date"] = ""
        return
    d = parse_date(raw)
    if d is None:
        print(f"[config] goals.driving_test_date {raw!r} is not a date: ignored. Write it as \"2027-01-10\".")
        cfg["goals"]["driving_test_date"] = ""
        return
    if str(raw).strip() != d.isoformat():
        print(f"[config] goals.driving_test_date {raw!r} is read as {d.day} {d:%B %Y} (day/month/year). "
              f"Write \"{d.isoformat()}\" to be explicit.")
    cfg["goals"]["driving_test_date"] = d.isoformat()


def defaults_only():
    """Built-in defaults, ignoring config.yaml (used by the tests)."""
    return load(path="/nonexistent/defaults.yaml", quiet=True)


def load(path=None, quiet=False):
    p = pathlib.Path(path) if path else ROOT / "config.yaml"
    user = (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None) or {}
    cfg = _merge(copy.deepcopy(DEFAULTS), user)
    missing = sorted(k for k in DEFAULTS if k not in user)
    if missing and not quiet:
        print("[config] not in your config.yaml, using defaults for:", ", ".join(missing))
    cfg["schedule"] = _fix_schedule(cfg["schedule"])
    _fix_date(cfg)
    cfg["db_path"], cfg["data_dir"] = _abs(cfg["db_path"]), _abs(cfg["data_dir"])   # same files from any folder
    return cfg