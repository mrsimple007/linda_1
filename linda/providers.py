"""OpenAI-compatible providers (Groq, Mistral). Each has its own cooldown. Tool list = CORE + recall."""
import json, os, re, time
from openai import OpenAI
from . import tools

# Read-only tools are gone: recall covers them. Only actions that write or do something stay.
CORE = {"task_add", "task_done", "timer", "plan", "stats", "practice_log", "song_add", "add_birthday",
        "delete_birthday", "save_note", "complete_goal", "remove_note", "journal_add", "log_progress",
        "time", "battery", "volume", "music", "mac", "open_app", "chess", "quiz", "remember",
        "calendar_agenda", "calendar_add", "log_driving", "telegram_send", "save_reference", "read_reference", "finance_report", "query_data",
        "client_add", "client_update", "client_info", "client_list", "client_undo", "bot_stats", "smart_home"}
_T = {"string": "string", "integer": "integer", "boolean": "boolean"}

# sent with EVERY message; everything else only when the message is about it (keeps each call far below the
# providers' tokens-per-minute limits: all tools at once were ~4 000 tokens)
BASE = {"recall", "task_add", "task_done", "save_note", "remember", "journal_add", "log_progress"}
CLIENT_TOOLS = {"client_add", "client_update", "client_info", "client_list", "client_undo"}
TOOL_RULES = {}                  # tool -> extra instruction, added to the prompt only when that tool is sent
EXTRA = [
    (r"birthday|tug'?il|день рожд|\bborn\b|turns \d", {"add_birthday", "delete_birthday"}),
    (r"practi|played|piano|guitar|song|mashq|qo'?shiq|пианино|гитар|песн|repertoire", {"practice_log", "song_add"}),
    (r"\bplay|music|spotify|qo'?shiq|музык|включи|pause|next track", {"music"}),
    (r"\bsend\b|telegram|yubor|jo'?nat|отправ|пришли|forward|greeting|tabrik|поздрав", {"telegram_send"}),
    (r"note|save|keep|course|lesson|outline|reference|schedule|scholar|stipend|grant|programme|program|apply|"
     r"saqla|eslatma|kurs|dars|сохрани|заметк|конспект|курс|урок", {"save_reference", "read_reference"}),
    (r"revenue|income|expens|spen[dt]|earned|profit|financ|purchase|bought|transaction|payment|balance|"
     r"pul|daromad|xarajat|xarid|доход|расход|выручк|прибыл|покупк|трат|баланс", {"finance_report", "query_data"}),
    (r"how many|how much|qancha|nechta|сколько|count|statisti", {"query_data"}),
    (r"\bbots?\b|quizzer|quiz bot|slides|slayd|presentation|prezentatsiya|users?\b|foydalanuvchi|пользоват|юзер|"
     r"\bstats?\b|active|subscri|premium|referat", {"bot_stats"}),
    (r"client|klient|mijoz|клиент|\bTLS\b|expa\w{2,3}io|proton|videx|viza\b|visa\b|passport|pasport|паспорт",
     CLIENT_TOOLS),
    (r"свет|ламп|люстр|light|lamp|chiroq|умн\w* дом|smart home|яркост|brightness", {"smart_home"}),
    (r"timer|remind|alarm|таймер|напомни|taymer|eslat", {"timer"}),
    (r"volume|louder|quieter|mute|screenshot|lock|громк|тише|громче|ovoz", {"volume", "mac"}),
    (r"\bopen\b|launch|открой|\boch\b", {"open_app"}),
    (r"chess|шахмат|shaxmat", {"chess"}),
    (r"quiz|\btest\b|квиз|тест", {"quiz", "log_driving"}),
    (r"\bplan\b|practi[cs]e|stats|progress|how am i|план|занят|mashq|reja", {"plan", "stats"}),
    (r"calendar|meeting|event|календар|встреч|uchrashuv", {"calendar_add", "calendar_agenda"}),
    (r"\bgoal|\bidea|цель|идея|maqsad", {"complete_goal", "remove_note"}),
    (r"\btime\b|battery|время|батаре|soat|batareya", {"time", "battery"}),
    (r"remove|delete|forget|erase|удал|забудь|o'?chir", {"remove_note", "complete_goal", "delete_birthday"}),
]

RECALL_KINDS = ["all", "day", "birthdays", "tasks", "songs", "practice", "profile", "goals", "ideas", "decisions",
                "conversations", "progress", "journal", "notes", "memory", "capabilities", "advice"]
RECALL_DESC = (
    "Read the user's saved data. Call it before answering questions about their data and before saving, changing or "
    "deleting anything. kind: "
    "day = everything for one date (journal, work log, practice, finished tasks): use it for 'yesterday', 'today' or a date; "
    "birthdays = people with date and relation (brother, friend...); tasks = open tasks; songs; practice; "
    "profile / goals / ideas / decisions = the user's own files; conversations = past chats with Linda by meaning "
    "('what did I say about X', 'last time we talked about'); progress = work log by project; "
    "journal = diary entries; memory = quick notes saved with 'remember that...'; "
    "notes = semantic search of notes/papers/projects; capabilities = what Linda can do plus her improvement log; advice = goals, ideas, profile, tasks, recent work, journal, practice and songs (+ the person if named) for suggestions, plans and wishes; all = search everything. "
    "query: words to filter by (names, relations, topics), IN ENGLISH (the data is stored in English, translate "
    "Russian/Uzbek words; names stay as written); empty lists everything. "
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
        """Rest a failing provider so the next question doesn't wait for it again."""
        code, msg = getattr(e, "status_code", 0), str(e)
        if code == 404:                                  # model missing: don't retry it all day
            wait = 86400
        elif code in (400, 413):                         # this request was the problem, not the provider
            return
        elif code == 429 or "RateLimit" in repr(e) or "rate limit" in msg.lower():
            m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", msg)
            asked = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))) if m else 0
            daily = "per day" in msg or "TPD" in msg or "RPD" in msg
            wait = max(asked, 900 if daily else 20)      # a used-up daily quota won't come back in seconds
        elif "Timeout" in repr(e) or "timed out" in msg.lower():
            wait = 120
        else:
            wait = 30
        self.cool_until = time.time() + wait
        print(f"[{self.name}] resting {wait / 60:.0f} min" if wait >= 120 else f"[{self.name}] resting {wait:.0f}s")

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