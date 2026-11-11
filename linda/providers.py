"""OpenAI-compatible providers (Groq, Mistral). Each has its own cooldown. Tool list = CORE + recall."""
import json, os, re, time
from openai import OpenAI
from . import tools

# Read-only tools are gone: recall covers them. Only actions that write or do something stay.
CORE = {"task_add", "task_done", "timer", "plan", "stats", "practice_log", "song_add", "add_birthday",
        "delete_birthday", "save_note", "complete_goal", "remove_note", "journal_add", "log_progress",
        "time", "battery", "volume", "music", "mac", "open_app", "chess", "quiz", "remember",
        "calendar_agenda", "calendar_add", "log_driving", "telegram_send"}
_T = {"string": "string", "integer": "integer", "boolean": "boolean"}

BASE = {"recall", "task_add", "task_done", "add_birthday", "delete_birthday", "journal_add", "log_progress",
        "save_note", "practice_log", "song_add", "telegram_send", "music", "remember"}
EXTRA = [
    (r"timer|remind|alarm|таймер|напомни|taymer|eslat", {"timer"}),
    (r"volume|louder|quieter|mute|screenshot|lock|громк|тише|громче|ovoz", {"volume", "mac"}),
    (r"\bopen\b|launch|открой|\boch\b", {"open_app"}),
    (r"chess|шахмат|shaxmat", {"chess"}),
    (r"quiz|\btest\b|квиз|тест", {"quiz", "log_driving"}),
    (r"\bplan\b|practi[cs]e|stats|progress|how am i|план|занят|mashq|reja", {"plan", "stats"}),
    (r"calendar|meeting|event|календар|встреч|uchrashuv", {"calendar_add", "calendar_agenda"}),
    (r"\bgoal|\bidea|цель|идея|maqsad", {"complete_goal", "remove_note"}),
    (r"\btime\b|battery|время|батаре|soat|batareya", {"time", "battery"}),
]

RECALL_KINDS = ["all", "day", "birthdays", "tasks", "songs", "practice", "profile", "goals", "ideas",
                "progress", "journal", "notes", "memory", "capabilities", "advice"]
RECALL_DESC = (
    "Read the user's saved data. Call it before answering questions about their data and before saving, changing or "
    "deleting anything. kind: "
    "day = everything for one date (journal, work log, practice, finished tasks): use it for 'yesterday', 'today' or a date; "
    "birthdays = people with date and relation (brother, friend...); tasks = open tasks; songs; practice; "
    "profile / goals / ideas = the user's own files; progress = work log by project; "
    "journal = diary entries; memory = quick notes saved with 'remember that...'; "
    "notes = semantic search of notes/papers/projects; capabilities = what Linda can do plus her improvement log; advice = goals, ideas, profile, tasks, recent work, journal, practice and songs (+ the person if named) for suggestions, plans and wishes; all = search everything. "
    "query: words to filter by (names, relations, topics); empty lists everything. "
    "A birthday row with match=partial is only a similar name, not the person asked about."
)


def pick(defs, text):
    names = set(BASE)
    for rx, extra in EXTRA:
        if re.search(rx, text, re.I):
            names |= extra
    return [d for d in defs if d["function"]["name"] in names]


def openai_tools():
    out = []
    for name, desc, params, required, _ in {r[0]: r for r in tools.TABLE}.values():
        if name not in CORE:
            continue
        props = {k: {"type": _T[tp], "description": d} for k, (tp, d) in params.items()}
        out.append({"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": required or []}}})
    out.append({"type": "function", "function": {"name": "recall", "description": RECALL_DESC,
        "parameters": {"type": "object",
                       "properties": {"kind": {"type": "string", "enum": RECALL_KINDS},
                                      "query": {"type": "string", "description": "names, relation or topic words; may be empty"}},
                       "required": ["kind"]}}})
    return out


class Provider:
    def __init__(self, name, base_url, key_env, model, timeout=8):
        self.name, self.model, self.cool_until = name, model, 0.0
        self.enabled = bool(os.getenv(key_env))
        self.client = OpenAI(base_url=base_url, api_key=os.getenv(key_env) or "none", timeout=timeout, max_retries=0)

    def ready(self):
        return self.enabled and time.time() >= self.cool_until

    def cool(self, e):
        if getattr(e, "status_code", 0) == 400:        # a malformed tool call is not a quota problem
            return
        wait = 30.0
        try:
            wait = float(e.response.headers.get("retry-after", wait))
        except Exception:
            pass
        self.cool_until = time.time() + min(max(wait, 5.0), 3600.0)

    def chat(self, msgs, tool_defs, effort="low", temp=0.3, timeout=None):
        kw = dict(model=self.model, messages=msgs, max_tokens=2500, temperature=temp)
        if tool_defs:
            kw.update(tools=tool_defs, tool_choice="auto")
        if "gpt-oss" in self.model:
            kw["extra_body"] = {"reasoning_effort": effort}     # check Groq's reasoning page; remove if it errors
        client = self.client.with_options(timeout=timeout) if timeout else self.client
        return client.chat.completions.create(**kw).choices[0].message


def build_providers(cfg):
    out = [Provider(p["name"], p["base_url"], p["key_env"], p["model"]) for p in cfg.get("providers", [])]
    return [p for p in out if p.enabled]