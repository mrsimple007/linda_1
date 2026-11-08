"""Offline test of the study modules: no mic, no Ollama, no Gemini. Run: python tests/smoke_v3.py"""
import datetime, pathlib, sys, tempfile, time
import numpy as np
ROOT = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "main.py").exists())
sys.path.insert(0, str(ROOT))
import linda.agent as agent_mod
import linda.docs as docs_mod
from linda.agent import Agent
from linda.audio_analysis import SR, analyze, tune
from linda.config import defaults_only
from linda.docs import Docs, chunk
from linda.planner import build_plan
from linda.quizbank import load_banks, parse, parse_choice
from linda.router import Intent
from linda.scheduler import due_jobs
from linda.store import Store

agent_mod.online = lambda: True
tmp = pathlib.Path(tempfile.mkdtemp())
cfg = defaults_only(); cfg["data_dir"] = str(tmp / "data")
today = datetime.date.today()


class Sp:
    def __init__(self): self.said = []
    def say(self, text, lang="en"): self.said.append((lang, text)); print("  SAY", lang, "|", text[:110])
    def is_speaking(self): return False


class Rt:
    nxt = None
    def route(self, text): return self.nxt


class G:
    def stream(self, contents, system=None, grounding=False):
        s = str(contents); print("  [LLM prompt tail]", s[-230:].replace("\n", " ⏎ "))
        yield "Practice plan explained. "; yield "Because of the reasons."
    def structured(self, contents, schema, system=None, max_out=400):
        return schema(summary="Good day.", tomorrow_focus="Fur Elise left hand.", new_tasks=["Book driving lesson"])


store = Store(str(tmp / "t.db")); sp, rt = Sp(), Rt()
ag = Agent(cfg, store, sp, G(), rt)
(ag.data_dir / "quizzes").mkdir(parents=True)

print("== songs yaml + practice + planner")
(ag.data_dir / "songs.yaml").write_text(
    'songs:\n  - {title: "Fur Elise", instrument: piano, status: learning, priority: 3, melody: "E5 D#5 E5"}\n'
    '  - {title: "Blackbird", instrument: guitar, status: polishing, priority: 2}\n'
    '  - {title: "C scale", instrument: piano, status: repertoire, priority: 1}\n', encoding="utf-8")
assert store.import_songs(ag.data_dir / "songs.yaml") == 3
rt.nxt = Intent(intent="practice_log", name="Blackbird", action="guitar", minutes=20, score=4, text="intro")
ag.run("log", "en")
cfg["goals"]["driving_test_date"] = str(today + datetime.timedelta(days=20))
plan = build_plan(store, cfg, 30)
for i in plan: print("  PLAN", i.minutes, i.title, i.reasons)
assert plan[0].title.startswith("Fur Elise") and sum(i.minutes for i in plan) == 30
assert any(i.kind == "quiz" for i in plan), "driving theory should appear with a test date"
rt.nxt = Intent(intent="plan", minutes=30, topic="practice"); ag.run("30 minutes tonight what should I practice", "en")

print("== quiz parse + voice session")
bank = ("1. Q one?\n+ Right A\n- Wrong 1\n- Wrong 2\n- Wrong 3\n\n2. Q two\nsecond line?\n+ Yes\n- No\n- Maybe\n- Never\n\n3. Skip me\n+ ???\n- x\n")
(ag.data_dir / "quizzes" / "signs.txt").write_text(bank, encoding="utf-8")
assert len(parse(bank, "signs")) == 2 and parse_choice("I think two", 4) == 2 and parse_choice("три", 4) == 3
rt.nxt = Intent(intent="quiz"); ag.run("start quiz", "en")
n = 0
while ag.quiz and n < 5:
    z = ag.quiz; right = z["items"][z["i"]][2]
    ag.run(str(right if n == 0 else (right % 4) + 1), "en"); n += 1
assert ag.quiz is None
print("  quiz stats:", store.quiz_bank_stats(), store.driving_recent(1))
assert store.quiz_bank_stats()["signs"][0] == 2

print("== tasks / journal / review")
rt.nxt = Intent(intent="task_add", text="Buy strings", when="tomorrow"); ag.run("task", "en")
rt.nxt = Intent(intent="task_list"); ag.run("tasks", "en")
rt.nxt = Intent(intent="task_done", text="strings"); ag.run("done", "en")
assert store.open_tasks() == []
rt.nxt = Intent(intent="journal_add", text="Hands felt tired today"); ag.run("journal", "en")
ag.job_evening_checkin(); assert ag.pending_review and ag.active_until > time.time()
ag.run("Practiced a bit, tired, want to book a lesson", "en")
assert store.recent_reviews(1)[0]["tomorrow"] and [t[1] for t in store.open_tasks()] == ["Book driving lesson"]
assert "Evening review" in (ag.data_dir / "journal" / f"{today}.md").read_text(encoding="utf-8")

print("== scheduler")
class S2:
    kv = {}
    def get(self, k): return self.kv.get(k)
jobs = cfg["schedule"]
now = datetime.datetime.combine(today, datetime.time(18, 45))
ids = [j["id"] for j in due_jobs(jobs, S2(), now)]
print("  due at 18:45 ->", ids); assert ids == ["birthdays", "practice"]     # morning disabled, evening not yet

print("== semantic search (fake embeddings)")
VOC = ["fingering", "left", "hand", "piano", "database", "postgres", "sqlite", "driving", "rules"]
def fake_embed(self, texts, prefix):
    m = np.array([[t.lower().count(w) for w in VOC] for t in texts], dtype=np.float32) + 1e-3
    return m / np.linalg.norm(m, axis=1, keepdims=True)
Docs._embed = fake_embed
d = Docs(store, cfg["docs"], ag.data_dir); ag.docs = d
(ag.data_dir / "notes").mkdir(exist_ok=True); (ag.data_dir / "projects").mkdir(exist_ok=True)
(ag.data_dir / "notes" / "piano.md").write_text("Fur Elise left hand fingering: use 5 1 3 on the piano.", encoding="utf-8")
(ag.data_dir / "projects" / "app.md").write_text("Decision: use sqlite not postgres database for the app.", encoding="utf-8")
print("  indexed", d.index(), "files (journal included)")
h = d.search("which database did I choose", 2); print("  top hit:", pathlib.Path(h[0].path).name)
assert pathlib.Path(h[0].path).name == "app.md"
rt.nxt = Intent(intent="search_notes", text="piano left hand fingering"); ag.run("search", "en")
assert d.index() == 0 and len(chunk("a\n\n" * 2000)) >= 1

print("== audio analysis on synthetic playing")
def tone(f, dur=0.5, gap=0.1):
    t = np.arange(int(SR * dur)) / SR
    return np.concatenate([np.sin(2*np.pi*f*t) * np.hanning(len(t)) ** 0.3, np.zeros(int(SR * gap))])
hz = lambda n: 440 * 2 ** ((n - 69) / 12)
seq = [76, 75, 76, 75, 76, 71, 74, 72, 69]                       # E5 D#5 E5 D#5 E5 B4 D5 C5 A4
y = np.concatenate([np.zeros(SR // 3)] + [tone(hz(m)) for m in seq]).astype(np.float32)
m = analyze(y, SR, expected="E5 D#5 E5 D#5 E5 B4 D5 C5 A4", target_bpm=100)
print("  perfect:", {k: m[k] for k in m if k not in ("played",)}); assert m["accuracy"] >= 0.8
bad = seq[:]; bad[5] = 70
m2 = analyze(np.concatenate([np.zeros(SR // 3)] + [tone(hz(x)) for x in bad]).astype(np.float32), SR,
             expected="E5 D#5 E5 D#5 E5 B4 D5 C5 A4")
print("  one wrong note:", m2["accuracy"], m2["mistakes"]); assert m2["accuracy"] < m["accuracy"]
tn = tune(np.concatenate([tone(hz(40) * 2 ** (-20 / 1200), 2.5)]).astype(np.float32), SR)
print("  tuner:", tn); assert tn["note"] == "E2" and -30 < tn["cents"] < -10
ag.recorder = lambda s: y
rt.nxt = Intent(intent="analyze_playing", name="Fur Elise", action="piano"); ag.run("listen", "en")
assert store.last_analysis(store.find_song("Fur Elise").id) is not None

print("== config: the mistakes a hand-edited config.yaml can contain")
import yaml
from linda import config as cfgmod
from linda.util import parse_date
assert parse_date("10/01/2027") == datetime.date(2027, 1, 10) and parse_date("2027-01-10") == datetime.date(2027, 1, 10)
assert parse_date("25.12.2026") == datetime.date(2026, 12, 25) and parse_date("01/25/2027") == datetime.date(2027, 1, 25)
assert parse_date("nonsense") is None and parse_date("31/02/2027") is None
user_cfg = tmp / "user.yaml"
user_cfg.write_text('goals: {driving_test_date: "10/01/2027"}\nschedule:\n  - {id: morning, at: "07:30", enabled: false}\n'
                    '  - {id: birthdays, at: "09:00"}\n  - {id: practice, at: "18:30"}\n  - {id: evening, at: "21:00"}\n'
                    '  - {id: bogus, at: "09:00"}\n  - {id: bad_time, at: 450, action: evening_checkin}\n', encoding="utf-8")
c = cfgmod.load(str(user_cfg), quiet=True)
assert c["goals"]["driving_test_date"] == "2027-01-10" and c["goals"]["instruments"] == ["piano", "guitar"]
assert [(j["id"], j["action"]) for j in c["schedule"]] == [("morning", "morning_briefing"), ("birthdays", "birthdays"),
                                                           ("practice", "practice_reminder"), ("evening", "evening_checkin")]
cfg["goals"]["driving_test_date"] = "10/01/2027"                       # even a raw, non-ISO value must not crash the planner
assert any(i.kind == "quiz" for i in build_plan(store, cfg, 30))
print("ALL OK")