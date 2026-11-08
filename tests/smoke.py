"""Offline smoke test with fakes: no mic, no Gemini, no Ollama, no Mac. Run: python tests/smoke.py"""
import datetime, pathlib, sys, tempfile
ROOT = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "main.py").exists())
sys.path.insert(0, str(ROOT))
import linda.agent as agent_mod
from linda.agent import Agent, EventSpec
from linda.board import parse_fen, uci_squares
try:
    from linda.calendar_skill import Event
except ModuleNotFoundError:       # calendar is optional; tests only need the record type
    import collections
    Event = collections.namedtuple("Event", "id title start end all_day")
from linda.config import defaults_only
from linda.files import docx_to_text, latest_download
from linda.router import Intent
from linda.store import Store

agent_mod.online = lambda: True
agent_mod.sysm.reveal = lambda p: None


class FakeSpeaker:
    def __init__(self): self.said = []
    def say(self, text, lang="en"): self.said.append((lang, text)); print("  SAY", lang, "|", text)


class FakeRouter:
    nxt = None
    def route(self, text): return self.nxt


class FakeGemini:
    def text(self, contents, system=None, grounding=False, max_out=None):
        s = str(contents)
        if "birthday greeting" in s: return "Tug'ilgan kuning muborak, azizim!"
        return "TITLE: Biology Test\n\n1. Q one?\n+ Right\n- Wrong\n- Wrong\n- Wrong"
    def stream(self, contents, system=None, grounding=False):
        yield "Hello there. "; yield "This is a test answer."
    def structured(self, contents, schema, system=None, max_out=400):
        return EventSpec(title="Meeting with Ali", start=(datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT15:00"), duration_minutes=45)


class FakeCal:
    def __init__(self): self.evs = {}
    def add(self, title, start, minutes=60):
        self.evs["id1"] = Event("id1", title, start, start + datetime.timedelta(minutes=minutes), False); return "id1"
    def delete(self, i): return self.evs.pop(i, None) is not None
    def events(self, s, e): return sorted([v for v in self.evs.values() if s <= v.start < e], key=lambda x: x.start)


class FakeTG:
    def __init__(self): self.sent = []
    def send(self, text): self.sent.append(("msg", text)); print("  TG msg |", text)
    def send_file(self, path, caption=""): self.sent.append(("file", str(path))); print("  TG file |", path)


tmp = pathlib.Path(tempfile.mkdtemp())
cfg = defaults_only(); cfg["downloads_dir"] = str(tmp)
sp, rt, cal, tg = FakeSpeaker(), FakeRouter(), FakeCal(), FakeTG()
ag = Agent(cfg, Store(str(tmp / "t.db")), sp, FakeGemini(), rt, cal=cal, tg=tg)
today = datetime.date.today()

print("== birthdays + telegram greeting")
rt.nxt = Intent(intent="add_birthday", name="Aziza", relation="mother", day=today.day, month=today.month, year=1965, language="uz")
ag.run("save", "ru")
assert ag.birthday_telegram("en") == 0 and not tg.sent
rt.nxt = Intent(intent="birthday_greeting", name="Azizа"); ag.run("greet", "en")   # note Cyrillic 'а' in name: fuzzy find
assert not tg.sent
ag.run("confirm", "en")
assert tg.sent[-1][1].startswith("Tug'ilgan"), tg.sent
print(ag.birthday_lines("uz", horizon=7))

print("== calendar")
rt.nxt = Intent(intent="calendar_add", title="Meeting with Ali",
                start=(datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%dT15:00"))
ag.run("meeting with Ali tomorrow 3pm", "en")
assert not cal.evs
ag.run("confirm", "en")
assert cal.evs
rt.nxt = Intent(intent="calendar_agenda", when="tomorrow"); ag.run("what's tomorrow", "en")
rt.nxt = Intent(intent="calendar_undo"); ag.run("undo", "en")
assert cal.evs
ag.run("confirm", "en")
assert not cal.evs

print("== docx -> txt -> telegram flag")
from docx import Document
d = Document(); tb = d.add_table(rows=1, cols=2); tb.cell(0, 0).text = "1. What is DNA?"
p = tb.cell(0, 1).paragraphs[0]; p.add_run("Right").bold = True; d.save(str(tmp / "quiz.docx"))
rt.nxt = Intent(intent="process_download", preset="quiz_txt", telegram=True); ag.run("convert and send", "en")
assert not (tmp / "Biology Test.txt").exists()
ag.run("confirm", "en")
assert (tmp / "Biology Test.txt").exists() and tg.sent[-1][0] != "file"
ag.run("confirm", "en")
assert (tmp / "Biology Test.txt").exists() and tg.sent[-1][0] == "file"

print("== chat then 'send that'")
rt.nxt = Intent(intent="chat"); ag.run("hi", "en")
rt.nxt = Intent(intent="telegram_send"); ag.run("send that to telegram", "en")
assert tg.sent[-1][1] != "Hello there. This is a test answer."
ag.run("confirm", "en")
assert tg.sent[-1] == ("msg", "Hello there. This is a test answer.")

print("== timer label / clipboard / mac")
rt.nxt = Intent(intent="timer", seconds=1, text="call mom"); ag.run("remind", "en")
import time; time.sleep(1.3)

print("== chess (undo, hint, board payload)")
sent = []
class FakeBW:
    def open(self): pass
    def update(self, fen, last, status, flip=False): sent.append((last, status, flip))
    def close(self): pass
ag.bw = FakeBW()
rt.nxt = Intent(intent="chess", color="black"); ag.run("play chess as black", "en")
for phrase in ["hint", "e7 to e5", "undo", "stop chess"]:
    print(" >", phrase); ag.run(phrase, "en")
assert ag.chess is None and sent and sent[0][2] is True, sent

print("== board helpers")
assert parse_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")[(4, 3)] == "P"
assert uci_squares("e2e4") == {(4, 1), (4, 3)}
print("ALL OK")