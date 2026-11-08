"""The small local model: turns a spoken command into a structured intent (JSON).
It never answers questions itself; anything open-ended becomes intent 'chat'.
Every intent is checked by sanitize(): a guess the user's words cannot back up becomes 'chat'."""
#linda.router.py
import re
from typing import Literal
import ollama
from pydantic import BaseModel
from .util import online


class Intent(BaseModel):
    intent: Literal["process_download", "news", "morning", "weather", "add_birthday",
                    "list_birthdays", "delete_birthday", "birthday_greeting", "calendar_add",
                    "calendar_agenda", "calendar_undo", "telegram_send", "clipboard", "mac",
                    "chess", "timer", "open_app", "volume", "music", "time", "battery",
                    "remember", "song_add", "practice_log", "plan", "analyze_playing", "tune", "quiz",
                    "log_driving", "task_add", "task_list", "task_done", "journal_add",
                    "search_notes", "stats", "chat"] = "chat"
    language: str = ""
    name: str = ""
    relation: str = ""
    day: int = 0
    month: int = 0
    year: int = 0
    seconds: int = 0
    level: int = -1
    app: str = ""
    action: str = ""
    color: str = ""
    text: str = ""
    instruction: str = ""
    preset: str = ""
    topic: str = ""
    when: str = ""
    telegram: bool = False
    minutes: int = 0
    score: int = -1
    total: int = 0


PROMPT = """You classify a voice command (English, Russian or Uzbek) into ONE intent and fill its fields. Reply with JSON only.
If the command is a question, small talk, a thank-you or unclear, use chat. Never invent field values: leave a field empty if the user did not say it.

Intents:
- process_download: ONLY when the user says download/downloads/file. Do something with the newest file in the Downloads folder (convert, rewrite, summarize, extract, translate...). instruction = what to do, in the user's words. preset = one of the names below if it clearly matches, else empty.
- news: latest world news. language = en, ru or uz ONLY if the user names a language. topic = optional topic.
- morning: ONLY "good morning" / morning briefing.
- weather: weather. topic = city if named.
- add_birthday: name, relation (mother, friend, ... if said), day (1-31), month (1-12), year (0 if not said), language = greeting language if said.
- list_birthdays: who has an upcoming birthday.
- delete_birthday: name.
- birthday_greeting: write a birthday greeting for a person. name, relation, language (en/ru/uz if said).
- calendar_add: put an event, meeting or appointment at a specific date or time into the calendar.
- calendar_agenda: what is on the calendar. when = today, tomorrow or week.
- calendar_undo: remove/undo the event just added.
- telegram_send: send something to the user's own Telegram. text = the message if the user dictates one, empty if they mean "that" / the last answer or file.
- clipboard: work with the text the user copied (summarize, translate, fix, explain). instruction = what to do.
- mac: action = lock, mute, unmute or screenshot.
- chess: start a chess game. color = white or black if said.
- timer: countdown or reminder after a delay ("in 20 minutes"). seconds = total seconds, text = what to be reminded about.
- open_app: app = macOS application name.
- volume: level 0-100.
- music: action = play, pause, next or previous.
- time: ONLY the current local time or date.
- battery: battery level.
- remember: text = the fact to remember.
- song_add: add a NAMED song/piece to learn. name = title (never just "piano"), action = piano or guitar, topic = learning, polishing or repertoire if said.
- practice_log: the user practiced. name = song (if said), action = piano or guitar, minutes, score = self-rating 1-5 if said, text = what they worked on.
- plan: ONLY when asked what to practice or how to use a stated amount of time. minutes = time available ONLY if the user said it, topic = practice if they ask only about practice.
- analyze_playing: listen to / check / record the user playing an instrument. name = song if said, action = piano or guitar.
- tune: tune the guitar / check a single note.
- quiz: start a quiz or driving theory test practice. topic = quiz set name if said, total = number of questions if said.
- log_driving: save a driving test result. topic = theory, practical or mock_test, score, total, text = notes.
- task_add: add a to-do. text = the task. when = today or tomorrow, or day + month; topic = project name if said.
- task_list: list open tasks.
- task_done: mark a task done. text = words from the task.
- journal_add: write something in the journal/diary. text = the entry.
- search_notes: look something up in the user's OWN notes, papers, project docs or journal. text = the question, topic = notes, papers, projects or journal if said.
- stats: progress or statistics (practice, chess record, driving quiz, tasks). topic = practice, chess, driving or tasks if said.
- chat: anything else, including every question, conversation, translation, calculation or request for information.

telegram = true on ANY intent if the user also asks to send/forward the result to their Telegram.

Presets for process_download:
%s

Month words: yanvar/январь=1, fevral/февраль=2, mart/март=3, aprel/апрель=4, may/май=5, iyun/июнь=6, iyul/июль=7, avgust/август=8, sentabr/сентябрь=9, oktabr/октябрь=10, noyabr/ноябрь=11, dekabr/декабрь=12."""


# ---- guard: the small model must not act on guesses --------------------------------------------
INSTR = {"piano", "guitar", "song", "songs", "music", "learning", "practice", ""}
BAD_NAMES = {"", "name", "your name", "my", "me", "you", "user", "unknown", "your relation"}
ASKING = re.compile(r"^\s*(when|what|who|how|where|do you|can you|когда|что|кто|как|qachon|nima|kim)\b", re.I)
HAS_NUM = re.compile(r"\d|\b(one|two|three|four|five|ten|fifteen|twenty|thirty|forty|sixty|hour|half|"
                     r"один|два|три|пять|десять|час|bir|ikki|besh|o'?n|soat)\b", re.I)
NEEDS = {   # the user's own words must contain evidence for the intent
    "process_download": r"download|downloads|скача|загруз|yuklab",
    "morning": r"morning|briefing|утр|ertalab",
    "plan": r"\bplan\b|practi[cs]e|should i|what to|what should|план|занят|mashq|reja",
    "song_add": r"\b(add|learn|repertoire)\b|добав|выуч|qo'?sh|o'?rgan",
    "practice_log": r"practi[cs]ed|played|finished|\bdone\b|занимал|сыграл|играл|chaldim",
    "birthday_greeting": r"greeting|wish|congrat|поздрав|tabrik",
    "weather": r"weather|rain|temperature|погод|ob-?havo",
    "news": r"\bnews\b|новост|yangilik",
    "chess": r"chess|шахмат|shaxmat",
    "tune": r"\btune\b|tuning|настро",
    "quiz": r"\bquiz|\btest\b|квиз|тест",
}


def sanitize(it, text):
    k, why = it.intent, None
    if k in NEEDS and not re.search(NEEDS[k], text, re.I):
        why = "no matching words"
    elif k == "song_add" and it.name.strip().lower() in INSTR:
        why = "no song title"
    elif k == "add_birthday" and (it.name.strip().lower() in BAD_NAMES or ASKING.match(text)
                                  or not (1 <= it.day <= 31 and 1 <= it.month <= 12)):
        why = "bad birthday fields"
    elif k == "birthday_greeting" and it.name.strip().lower() in BAD_NAMES:
        why = "no name"
    elif (k == "timer" and it.seconds <= 0) or (k == "open_app" and not it.app) or (k == "volume" and it.level < 0):
        why = "missing value"
    if why:
        print(f"[router] REJECTED {k} ({why}) -> chat")
        return Intent(intent="chat")
    WORDS = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "sixty": 60}
    nums = [int(n) for n in re.findall(r"\d+", text)] + [v for w, v in WORDS.items() if re.search(rf"\b{w}\b", text, re.I)]
    if k == "practice_log":
        if nums and it.minutes not in nums and it.minutes not in [n * 60 for n in nums]:
            print(f"[router] fixed minutes {it.minutes} -> {nums[0]}")
            it.minutes = nums[0]
        elif not nums:
            why = "no minutes said"
    if k == "plan" and it.minutes and not HAS_NUM.search(text):
        print(f"[router] dropped invented minutes={it.minutes}")
        it.minutes = 0
    return it


class Router:
    def __init__(self, cfg, gemini):
        self.model, self.keep = cfg["local"]["model"], cfg["local"]["keep_alive"]
        self.gemini = gemini
        self.gemini_ok = lambda: True
        self.prefer = cfg.get("router", "local")
        presets = "\n".join(f'- {k}: {v["description"]}' for k, v in cfg.get("presets", {}).items())
        self.system = PROMPT % (presets or "(none)")

    def warm(self):
        try:
            ollama.chat(model=self.model, messages=[{"role": "user", "content": "hi"}],
                        options={"num_predict": 1}, keep_alive=self.keep, think=False)
        except Exception as e:
            print("[local model not ready]", e)

    def route(self, text):
        return sanitize(self._route(text), text)

    def _route(self, text):
        if self.prefer == "gemini" and self.gemini and online() and self.gemini_ok():
            try:
                it = self.gemini.structured(text, Intent, system=self.system)
                print(f"[router:gemini] {it.model_dump(exclude_defaults=True)}")
                return it
            except Exception as e:
                print("[gemini router failed]", e)
        try:
            r = ollama.chat(model=self.model,
                            messages=[{"role": "system", "content": self.system},
                                      {"role": "user", "content": text}],
                            format=Intent.model_json_schema(),
                            options={"temperature": 0, "num_ctx": 4096}, keep_alive=self.keep)
            print(f"[router:local] raw JSON: {r.message.content}")
            return Intent.model_validate_json(r.message.content)
        except Exception as e:
            print("[local router failed]", e)
        if self.gemini and online() and self.gemini_ok():
            try:
                it = self.gemini.structured(text, Intent, system=self.system)
                print(f"[router:gemini] {it.model_dump(exclude_defaults=True)}")
                return it
            except Exception as e:
                print("[gemini router failed]", e)
        return Intent(intent="chat")


print("[router] guard active")