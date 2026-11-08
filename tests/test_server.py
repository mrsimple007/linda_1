"""End-to-end test of the web server with fakes: no browser, no mic, no Gemini, no Ollama.
Run: python tests/test_server.py"""
import datetime, pathlib, sys, tempfile
from types import SimpleNamespace as NS
import numpy as np
ROOT = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "server.py").exists())
sys.path.insert(0, str(ROOT))
import linda.agent as agent_mod
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from linda import tools
try:
    from linda.calendar_skill import Event
except ModuleNotFoundError:       # calendar is optional; tests only need the record type
    import collections
    Event = collections.namedtuple("Event", "id title start end all_day")
from linda.config import defaults_only
from linda.docs import Docs
from linda.store import Store
import server

agent_mod.online = lambda: True
tmp = pathlib.Path(tempfile.mkdtemp())
cfg = defaults_only(); cfg["db_path"] = str(tmp / "t.db"); cfg["data_dir"] = str(tmp / "data")
data = pathlib.Path(cfg["data_dir"]); (data / "quizzes").mkdir(parents=True); (data / "notes").mkdir()
(data / "quizzes" / "signs.txt").write_text("1. Q one?\n+ Right\n- W1\n- W2\n- W3\n\n2. Q two?\n+ Yes\n- No\n- Maybe\n- Never\n", encoding="utf-8")
(data / "notes" / "piano.md").write_text("Fur Elise left hand fingering on the piano.", encoding="utf-8")
cfg["gemini"]["minimal_thinking"] = False


def chunk(*parts): return NS(candidates=[NS(content=NS(parts=list(parts)))])
def txt(s): return NS(text=s, function_call=None, thought=False)
def call(name, **a): return NS(text=None, function_call=NS(name=name, args=a), thought=False)


class FakeGemini:
    def __init__(self): self.script, self.calls = [], 0
    def stream_tools(self, contents, system, decls, model=None):
        self.calls += 1
        for c in self.script.pop(0): yield c
    def stream(self, contents, system=None, grounding=False):
        yield "Sunny today. "; yield "Twenty degrees."
    def structured(self, *a, **k): raise RuntimeError("not used")
    def text(self, *a, **k): return "ok"


class FakeCal:
    def __init__(self): self.evs = {}
    def add(self, title, start, minutes=60): self.evs["e1"] = Event("e1", title, start, start + datetime.timedelta(minutes=minutes), False); return "e1"
    def delete(self, i): return bool(self.evs.pop(i, None))
    def events(self, s, e): return [v for v in self.evs.values() if s <= v.start < e]


VOC = ["fingering", "piano", "left", "hand", "database"]
def fake_embed(self, texts, prefix):
    m = np.array([[t.lower().count(w) for w in VOC] for t in texts], dtype=np.float32) + 1e-3
    return m / np.linalg.norm(m, axis=1, keepdims=True)
Docs._embed = fake_embed


async def fake_synth(text, lang):
    return {"type": "audio", "mime": "audio/mpeg", "b64": "AAAA", "text": text, "lang": lang}


store, gem = Store(cfg["db_path"]), FakeGemini()
docs = Docs(store, cfg["docs"], data); docs.index()
deps = NS(store=store, gemini=gem, docs=docs, cal=FakeCal(), tg=None)
app = server.create_app(cfg, deps, start_background=False, synth=fake_synth, recorder=lambda s, sr, **k: np.zeros(1))
HOST = {"host": "localhost:4700"}            # the test client would otherwise send "testserver"
ORIGIN = {**HOST, "origin": "http://localhost:4700"}


def turn(ws, text, lang="en"):
    """Send a command, return everything up to and including the next panels refresh."""
    ws.send_json({"type": "command", "text": text, "lang": lang})
    msgs, done = [], False
    while True:
        m = ws.receive_json(); msgs.append(m)
        done = done or m["type"] == "turn_done"
        if done and m["type"] == "panels":
            return msgs

spoken = lambda msgs: [m["text"] for m in msgs if m["type"] in ("audio", "speak_local")]
of = lambda msgs, t: [m for m in msgs if m["type"] == t]

with TestClient(app, base_url="http://localhost:4700") as client:
    print("== security")
    for bad in ({**HOST, "origin": "http://evil.example"}, {**HOST, "origin": "http://localhost:9999"},
                {"host": "evil.com", "origin": "http://localhost:4700"}):
        try:
            with client.websocket_connect("/ws", headers=bad) as w:
                w.receive_json()
            raise AssertionError("evil origin accepted")
        except WebSocketDisconnect:
            pass
    assert client.get("/").status_code == 200 and "Linda" in client.get("/").text
    assert client.get("/.env").status_code == 404 and client.get("/config.yaml").status_code == 404
    assert client.get("/", headers={"host": "evil.com"}).status_code == 400
    g = client.get("/api/galaxy").json()
    assert [n["label"] for n in g["nodes"]].count("piano") == 1
    print("  ok: evil origins rejected, DNS-rebinding host rejected, only ui/ public, galaxy api")

    with client.websocket_connect("/ws", headers=ORIGIN) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "лин" in hello["wake_pattern"]
        assert ws.receive_json()["type"] == "panels"
        bridge = app.state.bridge

        print("== fast path: no LLM call")
        m = turn(ws, "what time is it"); print("  ", spoken(m))
        assert spoken(m)[0].startswith("It's") and gem.calls == 0 and bridge.last_timer.route == "fast"
        m = turn(ws, "который час", "ru"); assert spoken(m)[0].startswith("Сейчас") and gem.calls == 0
        gem.script = [[chunk(txt("The meeting is not in my data."))]]
        m = turn(ws, "what time is the meeting tomorrow")       # must NOT hit the fast path
        assert gem.calls == 1 and bridge.last_timer.route == "llm"

        print("== LLM: plain answer, sentence streaming, latency marks")
        gem.script = [[chunk(txt("Paris is the capital of France. ")), chunk(txt("It is a lovely city."))]]
        m = turn(ws, "capital of France?")
        print("  ", spoken(m)); assert spoken(m) == ["Paris is the capital of France.", "It is a lovely city."]
        marks = bridge.last_timer.marks
        assert {"first_token", "first_sentence", "tts_audio", "done"} <= set(marks), marks
        print("  ", bridge.last_timer.line(1234))

        print("== LLM: tool call (no second LLM round trip)")
        gem.script = [[chunk(call("task_add", title="Buy strings", due="2026-11-04"))]]
        before = gem.calls; m = turn(ws, "add a task buy strings for November 4")
        assert gem.calls == before + 1 and "Task added" in spoken(m)[0]
        assert [r[1] for r in store.open_tasks()] == ["Buy strings"]
        assert of(m, "panels")[-1]["tasks"][0]["title"] == "Buy strings"

        print("== parallel tools speak in the order requested")
        gem.script = [[chunk(call("weather", topic="Tashkent"), call("task_add", title="Call mom"))]]
        m = turn(ws, "weather and add task call mom"); print("  ", spoken(m))
        assert spoken(m)[-1].startswith("Task added") and "Sunny today." in spoken(m)
        assert spoken(m).index("Sunny today.") < spoken(m).index("Task added: Call mom.")

        print("== calendar through a tool call")
        t = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        gem.script = [[chunk(call("calendar_add", title="Meeting with Ali", start=t + "T15:00", duration_minutes=45))]]
        m = turn(ws, "meeting with Ali tomorrow at 3"); print("  ", spoken(m))
        assert "confirm" in spoken(m)[0].lower() and not deps.cal.evs
        m = turn(ws, "confirm")
        assert "Added to your calendar" in spoken(m)[0] and deps.cal.evs["e1"].title == "Meeting with Ali"

        print("== reject a hallucinated download action")
        gem.script = [[chunk(call("process_download", instruction="do something with newest file",
                                  preset="internet_connection"))]]
        m = turn(ws, "Can you help me with possible ideas?")
        assert any("misheard" in s.lower() for s in spoken(m)), spoken(m)
        assert not any("confirm" in s.lower() for s in spoken(m)), spoken(m)
        assert store.q("SELECT result FROM audit_log WHERE tool='process_download' ORDER BY id DESC LIMIT 1")[0][0] == "rejected_unsubstantiated"

        print("== notes search lights up the galaxy")
        gem.script = [[chunk(call("search_notes", query="piano left hand fingering"))]]
        m = turn(ws, "what did I write about fingering")
        hits = of(m, "galaxy_hits"); assert hits and hits[0]["paths"][0].endswith("piano.md")

        print("== chess: LLM starts it, then moves skip the LLM")
        gem.script = [[chunk(call("chess", color="white"))]]
        m = turn(ws, "let's play chess")
        assert of(m, "chess")[0]["active"] and "Let's play" in spoken(m)[0]
        before = gem.calls; m = turn(ws, "e2 to e4")
        assert gem.calls == before and bridge.last_timer.route == "session:chess"
        assert of(m, "chess")[0]["last"] == "e2e4", of(m, "chess")
        assert of(m, "panels")[-1]["session"] == "chess"
        m = turn(ws, "stop chess"); assert of(m, "chess")[0]["fen"] is None

        print("== quiz: card in panels, answers skip the LLM")
        gem.script = [[chunk(call("quiz"))]]
        m = turn(ws, "start a quiz"); q = of(m, "panels")[-1]["quiz"]
        assert q and q["n"] == 2 and len(q["options"]) == 4
        before = gem.calls; m = turn(ws, "1"); assert gem.calls == before and bridge.last_timer.route == "session:quiz"
        m = turn(ws, "stop"); assert of(m, "panels")[-1]["quiz"] is None
        assert store.quiz_bank_stats()["signs"][0] == 1

        print("== interrupt")
        ws.send_json({"type": "interrupt"})
        kinds = []
        while "turn_done" not in kinds:
            kinds.append(ws.receive_json()["type"])
        assert "stop_audio" in kinds

# ---- unit checks on the tool table
print("== tool table")
decls = tools.declarations(); print("  ", len(decls), "function declarations")
assert len(decls) == len(tools.TABLE) >= 36
c = tools.coerce({"instrument": "Guitar", "rating": 4.0, "minutes": "20", "language": "Uzbek", "bogus": 1},
                 tools.SPEC["practice_log"][4])
assert c == {"action": "Guitar", "score": 4, "minutes": 20, "language": "uz"}, c
assert not set(tools.SEQUENTIAL) - set(tools.SPEC), "unknown tool in SEQUENTIAL"
print("ALL OK")