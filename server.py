"""Linda web server.   python server.py   ->  http://127.0.0.1:4700
Two voice modes (config: server.local_ears):
  false: browser = ears (speech recognition) + voice (audio playback) + screen
  true:  Python  = ears (mic -> VAD -> Whisper -> wake word) + voice (Speaker); browser = screen only
Python = brain, skills, database. Keys stay in .env on this side; only ui/ is served."""
import asyncio, datetime, pathlib, os, re, shutil, subprocess, sys, threading, time, webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from linda import memory_files, scheduler
from linda.agent import Agent
from linda.audio_analysis import SR
from linda.brain import Brain
from linda.config import ROOT, load
from linda.planner import build_plan
from linda.web import Bridge, Client, Timer, WebBoard, WebSpeaker
from linda import tools

UI_DIR = ROOT / "ui"

_STOP = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|linda|please|hey)[, ]+)*"
    r"(stop(?: it| talking)?|be quiet|quiet|shut up|enough|that'?s enough|cancel|never ?mind|"
    r"хватит|стоп|замолчи|тихо|перестань|"
    r"to'?xta|bas|yetarli|jim)\W*$", re.I)


def build_deps(cfg):
    from linda.docs import Docs
    from linda.store import Store
    from linda.telegram import Telegram
    store = Store(cfg["db_path"])
    data_dir = pathlib.Path(cfg["data_dir"])
    n = store.import_songs(data_dir / "songs.yaml")
    print(f"[songs] {n} imported" if n else "[songs] none yet: edit data/songs.yaml")
    gemini = None
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        from linda.gemini import Gemini
        gemini = Gemini(cfg["gemini"])
    else:
        print("[!] GEMINI_API_KEY not set: only fast-path commands and the offline model will work.")
    cal = None
    if cfg["calendar"]["enabled"]:
        try:
            from linda.calendar_skill import Calendar
            cal = Calendar()
        except Exception as e:
            print("[calendar disabled]", e)
    tg = Telegram.from_env()
    if not tg:
        print("[telegram not configured] run: python telegram_setup.py")
    return SimpleNamespace(store=store, gemini=gemini, docs=Docs(store, cfg["docs"], data_dir), cal=cal, tg=tg)


def data_path(cfg):
    p = pathlib.Path(cfg["data_dir"])
    return p if p.is_absolute() else ROOT / p


def md_items(path, limit=40):
    """Entries of a markdown file for the UI: [{text, done}].
    Structured files (### Title followed by "- **Status:** x") give one entry per ### heading; otherwise bullet lines."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if any(l.startswith("### ") for l in lines):
        out, cur = [], None
        for l in lines:
            if l.startswith("### "):
                cur = {"text": l[4:].strip(), "done": False}
                out.append(cur)
            elif l.startswith("## "):
                cur = None
            elif cur is not None:
                m = re.match(r"\s*[-*]\s*\*\*Status:?\*\*:?\s*(\w+)", l)
                if m:
                    cur["text"] += f"  ·  {m.group(1)}"
                    cur["done"] = m.group(1).lower() in ("implemented", "done", "completed")
        return out[:limit]
    out = []
    for line in lines:
        s = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        if not s or s.startswith("#") or s.startswith("<!--"):
            continue
        out.append({"text": re.sub(r"^\[[ xX]\]\s*", "", s).replace("~~", ""),
                    "done": bool(re.match(r"\[[xX]\]", s)) or s.startswith("~~") or "✅" in s})
    return out[:limit]


def file_birthdays(cfg):
    """Parse data/birthdays.md (dates as MM-DD or YYYY-MM-DD, any layout: bullets or tables)."""
    path, today, out = data_path(cfg) / "birthdays.md", datetime.date.today(), []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.search(r"(?:\d{4}-)?(\d{1,2})-(\d{1,2})(?!\d)", line)
        if not m or line.lstrip().startswith("#"):
            continue
        mo, d = int(m.group(1)), int(m.group(2))
        cells = [c.strip() for c in line.replace(m.group(0), "|").split("|")]
        name = next((re.sub(r"^[\s>*+\-–—:]+|[\s*:\-–—]+$", "", c) for c in cells if re.search(r"\w", c)), "")
        if not name:
            continue
        try:
            nxt = datetime.date(today.year, mo, d)
            if nxt < today:
                nxt = datetime.date(today.year + 1, mo, d)
        except ValueError:
            continue
        out.append({"name": name, "date": str(nxt), "n": (nxt - today).days})
    return out


def merged_birthdays(store, cfg):
    out = {b.name.lower(): {"name": b.name, "date": str(b.date), "n": b.n} for b in store.upcoming(30)}
    for b in file_birthdays(cfg):
        if b["n"] <= 30:
            out.setdefault(b["name"].lower(), b)
    return sorted(out.values(), key=lambda b: b["n"])[:6]


_bday_seen = {"mtime": 0.0}


def sync_birthdays(cfg, force=False):
    """Put the people from data/birthdays.md into the database (only missing ones, matched by name + date).
    Runs at startup and whenever the file changes. Reads the real columns of the `birthdays` table, so it needs
    no knowledge of store.py. Returns how many were added."""
    import sqlite3
    path = data_path(cfg) / "birthdays.md"
    if not path.exists() or (not force and path.stat().st_mtime <= _bday_seen["mtime"]):
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    people, heading = [], ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            heading = line.lstrip("# ").strip()
            continue
        m = re.search(r"(?:\d{4}-)?(\d{1,2})-(\d{1,2})(?!\d)", line)
        if not m or not (1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31):
            continue
        cells = [c.strip() for c in line.replace(m.group(0), "|").split("|")]
        words = [re.sub(r"^[\s>*+\-–—:]+|[\s*:\-–—]+$", "", c) for c in cells]
        words = [w for w in words if re.search(r"\w", w)]
        if not words:
            continue
        name, rel = words[0], " · ".join(words[1:])
        paren = re.match(r"^(.*?)\s*\((.*)\)$", name)
        if paren:
            name, rel = paren.group(1), paren.group(2) or rel
        people.append((name, int(m.group(1)), int(m.group(2)), rel or heading))
    db = pathlib.Path(cfg["db_path"])
    db = db if db.is_absolute() else ROOT / db
    con = sqlite3.connect(db, timeout=10)
    try:
        info = con.execute("PRAGMA table_info(birthdays)").fetchall()       # cid, name, type, notnull, default, pk
        cols = {r[1]: r for r in info}
        pick = lambda *names: next((c for c in names if c in cols), None)
        c_name, c_mo, c_day = pick("name"), pick("month"), pick("day")
        c_date = None if (c_mo and c_day) else pick("date", "birthday", "mmdd", "dob", "bday")
        c_rel = pick("relation", "relationship", "side", "group", "note", "notes")
        c_lang = next((c for c in cols if "lang" in c.lower()), None)
        if not c_name or not ((c_mo and c_day) or c_date):
            print("[birthdays] cannot map the table columns:", ", ".join(f"{r[1]} {r[2]}" for r in info) or "no table")
            return 0
        gl = re.search(r"^greeting_language:\s*(\w+)", text, re.M)
        lang = gl.group(1) if gl else cfg.get("birthdays", {}).get("greeting_language", cfg.get("default_language", "en"))
        sel = f"select {c_name}, {c_mo}, {c_day} from birthdays" if c_mo else f"select {c_name}, {c_date} from birthdays"
        rows = con.execute(sel).fetchall()
        base = lambda n: re.sub(r"\s*\(.*\)$", "", str(n)).strip().lower()       # "Annam (other)" -> "annam"
        have = {(base(r[0]),) + tuple(str(v).lower() for v in r[1:]) for r in rows}
        used = {str(r[0]).lower() for r in rows}                                    # the name column is UNIQUE
        added = 0
        for name, mo, d, rel in people:
            row = {c_name: name}
            if c_mo:
                row[c_mo], row[c_day] = mo, d
                key = (name.lower(), str(mo), str(d))
            else:
                row[c_date] = f"{mo:02d}-{d:02d}"
                key = (name.lower(), row[c_date])
            if key in have:
                continue
            if name.lower() in used:                                                # same name, other person/date
                options = ([f"{name} ({rel})"] if rel else []) + [f"{name} ({mo:02d}-{d:02d})"]
                row[c_name] = next((o for o in options if o.lower() not in used), None)
                if not row[c_name]:
                    continue
            if c_rel:
                row[c_rel] = rel
            if c_lang:
                row[c_lang] = lang
            for c, r in cols.items():                                               # NOT NULL columns we did not fill
                if c not in row and r[3] and r[4] is None and not r[5]:
                    row[c] = 0 if "INT" in (r[2] or "").upper() else ""
            try:
                con.execute(f"insert into birthdays ({', '.join(row)}) values ({', '.join('?' * len(row))})", list(row.values()))
            except sqlite3.IntegrityError as e:                                     # one bad row must not block the rest
                print(f"[birthdays] skipped {name}: {e}")
                continue
            have.add(key)
            used.add(row[c_name].lower())
            added += 1
        con.commit()
        _bday_seen["mtime"] = path.stat().st_mtime
        print(f"[birthdays] {len(people)} in birthdays.md, {added} added to the database")
        return added
    except Exception as e:
        print("[birthdays] sync failed:", repr(e))
        return 0
    finally:
        con.close()


def sync_root_md(cfg):
    """docs.py indexes data/notes, papers, ...: mirror the loose data/*.md files there so search finds them."""
    d = data_path(cfg)
    dst = d / "notes"
    dst.mkdir(exist_ok=True)
    n = 0
    for f in d.glob("*.md"):
        t = dst / f"root_{f.name}"
        if not t.exists() or f.stat().st_mtime > t.stat().st_mtime:
            shutil.copy2(f, t)
            n += 1
    return n


_TURN = {"replies": [], "tools": []}                          # what happened during the current command


def log_turn(cfg, text, lang, tools_used, reply):
    """Remember what was asked and done: table `history` in linda.db + data/journal/activity-YYYY-MM-DD.md
    (the journal folder is indexed, so "what did I ask yesterday?" can be answered by notes search)."""
    import json, sqlite3
    now = datetime.datetime.now()
    try:
        db = pathlib.Path(cfg["db_path"])
        db = db if db.is_absolute() else ROOT / db
        con = sqlite3.connect(db, timeout=10)
        try:
            con.execute("create table if not exists history(id integer primary key, ts text, lang text, "
                        "user_text text, tools text, reply text)")
            con.execute("insert into history(ts, lang, user_text, tools, reply) values (?,?,?,?,?)",
                        (now.isoformat(timespec="seconds"), lang, text, json.dumps(tools_used), reply))
            con.commit()
        finally:
            con.close()
        folder = data_path(cfg) / "journal"
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"activity-{now:%Y-%m-%d}.md"
        if not f.exists():
            f.write_text(f"# Activity {now:%Y-%m-%d}\n\n", encoding="utf-8")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"- {now:%H:%M} **You:** {text}\n  - tools: {', '.join(tools_used) or '-'}\n  - **Linda:** {reply}\n")
    except Exception as e:
        print("[history] could not save:", repr(e))


def recent_history(cfg, n=8):
    import sqlite3
    db = pathlib.Path(cfg["db_path"])
    db = db if db.is_absolute() else ROOT / db
    con = sqlite3.connect(db, timeout=10)
    try:
        rows = con.execute("select ts, user_text, reply from history order by id desc limit ?", (n,)).fetchall()
    except sqlite3.OperationalError:                           # table not created yet
        return []
    finally:
        con.close()
    return [{"t": r[0][11:16], "q": r[1][:90], "a": (r[2] or "")[:110]} for r in rows]


def snapshot(agent, cfg):
    """Everything the side panels show. Each section is isolated: one failure never blanks the others."""
    store, today = agent.store, datetime.date.today()

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as e:
            print(f"[panel {label}] {e!r}")
            return default

    tt = safe("task times", lambda: tools.task_times(cfg), {})

    def due_label(d):
        if d == str(today):
            return "today"
        if d == str(today + datetime.timedelta(days=1)):
            return "tomorrow"
        return d

    def calendar_today():
        start = datetime.datetime.combine(today, datetime.time.min)
        out = []
        if agent.cal:
            out = [{"time": "" if e.all_day else e.start.strftime("%H:%M"), "title": e.title}
                   for e in agent.cal.events(start, start + datetime.timedelta(days=1))]
        out += [{"time": tt.get(tools._norm(r[1]), ""), "title": r[1]}
                for r in store.open_tasks() if str(r[2]) == str(today)]
        return out[:8]

    def task_rows():
        rows = []
        for r in store.open_tasks()[:8]:
            due = due_label(str(r[2]) if r[2] else "")
            rows.append({"title": r[1], "due": f"{due} {tt.get(tools._norm(r[1]), '')}".strip()})
        return rows

    def quiz_card():
        if not agent.quiz:
            return None
        z = agent.quiz
        q, opts, _ = z["items"][z["i"]]
        return {"i": z["i"] + 1, "n": len(z["items"]), "ok": z["ok"], "question": q.text, "options": opts}

    session = ("chess" if agent.chess else "quiz" if agent.quiz else
               "review" if agent.pending_review and time.time() < agent.pending_review else None)
    
    meta = safe("song meta", lambda: tools.song_meta(cfg), {})

    def plan_rows():
        rows = []
        for i in build_plan(store, cfg, cfg["practice"]["default_minutes"]):
            base = tools._norm(re.sub(r"\s*\([^)]*\)\s*$", "", i.title))
            c = meta.get(base)
            rows.append({"title": i.title + (f" · {c}" if c else ""), "minutes": i.minutes, "why": i.reasons})
        return rows    
    
    return {"type": "panels", "session": session,
            "quiz": safe("quiz", quiz_card, None),
            "calendar": safe("calendar", calendar_today, []),
            "tasks": safe("tasks", task_rows, []),
            "plan": safe("plan", lambda: [{"title": i.title, "minutes": i.minutes, "why": i.reasons}
                                          for i in build_plan(store, cfg, cfg["practice"]["default_minutes"])], []),
            "practice": safe("practice", lambda: store.practice_summary(7),
                             {"total_minutes": 0, "by_instrument": {}, "by_song": {}}),
            "birthdays": safe("birthdays", lambda: merged_birthdays(store, cfg), []),
            "memory": safe("memory", lambda: {k: md_items(data_path(cfg) / f"{k}.md")
                                              for k in ("profile", "goals", "ideas")}, {}),
            "history": safe("history", lambda: recent_history(cfg), []),
            "goal_minutes": cfg["goals"].get("weekly_practice_minutes", 150)}


import collections, difflib
class LocalSpeaker:
    """Local mode: Python plays the sound (linda.tts.Speaker); the browser only gets the text to display.
    Anything else the agent asks of the speaker is forwarded to the web speaker."""

    def __init__(self, bridge, web, tts):
        self.bridge, self.web, self.tts = bridge, web, tts
        self.recent= collections.deque(maxlen=10)  # last few texts spoken (for debugging)
        self.silent = False                                   # text-only reply (typed message without the 🔊 switch)

    def say(self, text, lang="en", **_):
        log = getattr(self, "turn_log", None)
        if isinstance(log, list):
            log.append(text)
        self.bridge.send({"type": "audio", "text": text, "lang": lang})   # no b64: the UI only shows it
        self.recent.append((time.time(), text.lower()))
        if not self.silent:
            self.tts.say(text, lang)

    def stop(self):
        self.tts.stop()
        self.bridge.send({"type": "stop_audio"})

    def is_speaking(self):
        return self.tts.is_speaking()


    def is_echo(self, heard):
        h, now = heard.lower().strip(" .!?"), time.time()
        return any(now - t < 40 and (h in s or difflib.SequenceMatcher(None, h, s).ratio() > 0.6)
                   for t, s in self.recent)        

    @property
    def quiet_until(self):
        return self.tts.quiet_until

    def __getattr__(self, name):                              # only called for attributes not defined above
        return getattr(self.web, name)


def create_app(cfg, deps, start_background=True, synth=None, recorder=None):
    memory_files.init(cfg)
    d = data_path(cfg)
    found = [n for n in ("profile", "goals", "ideas", "birthdays") if (d / f"{n}.md").exists()]
    print(f"[data] {d}  ->  md files found: {', '.join(found) or 'NONE (set data_dir in config.yaml to the folder that holds them)'}")
    srv = cfg["server"]
    local_ears = bool(srv.get("local_ears"))

    allowed = {f"http://{h}:{srv['port']}" for h in ("localhost", "127.0.0.1")}
    bridge = Bridge(cfg, synth)
    _send = bridge.send

    def send_and_note(o):                                       # remember what she says during this command
        if isinstance(o, dict) and o.get("type") in ("audio", "speak_local") and o.get("text"):
            _TURN["replies"].append(o["text"])
        return _send(o)
    bridge.send = send_and_note
    speaker = WebSpeaker(bridge, cfg)
    if local_ears:
        from linda.tts import Speaker
        speaker = LocalSpeaker(bridge, speaker, Speaker(cfg["tts"]))
    cur = {"lang": cfg["default_language"]}                    # language the user picked (used as a Whisper hint)
    ears_ref = {"ears": None, "listener": None}                # filled in by lifespan when local ears start

    router = None
    try:
        from linda.router import Router
        router = Router(cfg, deps.gemini)                      # only used when offline
    except Exception:
        pass

    def record(secs):
        bridge.send({"type": "mic", "enabled": False})         # browser stops listening while Python records
        lis = ears_ref["listener"]
        was_paused = lis.paused if lis else False
        if lis:
            lis.paused = True                                  # same for the local microphone
        try:
            from linda.recorder import record_audio
            return (recorder or record_audio)(secs, SR, device=cfg["audio"].get("music_device", ""))
        finally:
            if lis:
                lis.drain()                                    # don't "hear" the piano we just recorded
                lis.paused = was_paused
            bridge.send({"type": "mic", "enabled": True})

    def set_lang(code):
        cur["lang"] = code
        bridge.send({"type": "set_lang", "lang": code})

    def open_window(seconds=300):
        ears = ears_ref["ears"]
        if ears:
            ears.window_until = time.time() + seconds
        else:
            bridge.send({"type": "open_window", "seconds": seconds})

    agent = Agent(cfg, deps.store, speaker, deps.gemini, router, cal=deps.cal, tg=deps.tg, docs=deps.docs, recorder=record)
    agent.bw = WebBoard(bridge)
    agent.open_window = open_window
    deps.docs.on_hits = lambda hits: bridge.send({"type": "galaxy_hits", "paths": [h.path for h in hits]})
    brain = Brain(cfg, agent, speaker, deps.gemini, set_lang=set_lang)
    executor = ThreadPoolExecutor(max_workers=1)               # one command at a time
    try:                                                        # note which tools ran (works if brain calls tools.execute)
        from linda import tools as _tools
        if not getattr(_tools.execute, "_logged", False):
            _orig = _tools.execute

            def _logged_execute(agent_, name, args, text):
                _TURN["tools"].append(name)
                return _orig(agent_, name, args, text)
            _logged_execute._logged = True
            _tools.execute = _logged_execute
    except Exception as e:
        print("[history] tool logging off:", e)

    def push_state():
        try:
            bridge.send(snapshot(agent, cfg))
        except Exception as e:
            print("[panels]", e)

    def thinking(flag):
        if ears_ref["ears"]:
            ears_ref["ears"].thinking = flag

    async def run_command(text, lang, speak=True):
        loop = asyncio.get_running_loop()
        if _STOP.match(text):
            brain.interrupt()
            if agent.quiz:
                agent.quiz_turn("stop")
            elif agent.chess:
                agent.chess_turn("stop")
            agent.cancel_pending_actions()
            thinking(False)
            bridge.send({"type": "turn_done"})
            await loop.run_in_executor(None, push_state)
            return
        if hasattr(speaker, "silent"):
            speaker.silent = not speak
        _TURN["replies"].clear()
        _TURN["tools"].clear()
        timer = bridge.timer = bridge.last_timer = Timer()
        thinking(True)
        bridge.send({"type": "state", "state": "thinking"})
        try:
            await loop.run_in_executor(executor, brain.handle, text, lang, timer)
        finally:
            thinking(False)
            loop.run_in_executor(None, log_turn, cfg, text, lang, list(_TURN["tools"]), " ".join(_TURN["replies"]))
        timer.mark("done")
        bridge.send({"type": "turn_done"})
        await loop.run_in_executor(None, push_state)
        if not bridge.clients:
            print(timer.line())

    def start_local_ears(loop):
        """Mic -> VAD -> Whisper -> wake word, in background threads. Never takes the server down if the mic fails."""
        try:
            from linda.audio import Listener
            from linda.ears import Ears
            from linda.stt import STT
            listener = Listener(cfg["audio"], speaker.is_speaking)

            def on_command(text, lang):                        # called from the ears thread
                cur["lang"] = lang
                if brain.cancel and not brain.cancel.is_set():
                    brain.interrupt()
                asyncio.run_coroutine_threadsafe(run_command(text, lang), loop)

            def on_interrupt():
                brain.interrupt()
                speaker.stop()
                thinking(False)
                bridge.send({"type": "turn_done"})

            ears = Ears(listener, STT(cfg["stt"]), speaker, cfg["audio"]["wake_pattern"],
                        cfg["audio"]["conversation_window_s"],
                        on_command=on_command, on_interrupt=on_interrupt, notify=bridge.send,
                        in_session=lambda: bool(agent.chess or agent.quiz), lang=lambda: cur["lang"])
            ears_ref["ears"], ears_ref["listener"] = ears, listener
            ears.start()
            print("[ears] local microphone + Whisper active")
        except Exception as e:
            print("[ears] could not start local ears:", repr(e))

    async def refresher():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(60)
            if start_background:
                await loop.run_in_executor(None, sync_birthdays, cfg)   # picks up edits of birthdays.md
            if bridge.clients:
                await loop.run_in_executor(None, push_state)

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        bridge.start(loop)
        tasks = []
        if start_background:
            threading.Thread(target=scheduler.run_loop, args=(cfg, deps.store, agent), daemon=True).start()
            if deps.cal:
                threading.Thread(target=scheduler.calendar_watch, args=(cfg, agent, deps.cal), daemon=True).start()
            def index_docs():
                sync_birthdays(cfg)
                try:
                    n = sync_root_md(cfg)
                    print(f"[docs] mirrored {n} md file(s) from data/ into data/notes/" if n else "[docs] data/*.md already mirrored")
                except Exception as e:
                    print("[docs] mirror failed:", e)
                print(f"[docs] indexed {deps.docs.index()} new/changed files")
            threading.Thread(target=index_docs, daemon=True).start()
            tasks.append(asyncio.create_task(refresher()))
            if local_ears:
                start_local_ears(loop)
            if srv.get("open_browser"):
                threading.Thread(target=open_ui, args=(cfg, bridge), daemon=True).start()
        yield
        for t in tasks:
            t.cancel()
        bridge.shutdown()
        executor.shutdown(wait=False)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])   # blocks DNS rebinding

    @app.get("/api/galaxy")
    async def galaxy():
        return await asyncio.get_running_loop().run_in_executor(None, deps.docs.graph)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin")
        if origin and origin not in allowed:                   # other websites must not be able to drive Linda
            await ws.close(code=4403)
            return
        await ws.accept()
        client = Client(ws)
        writer = asyncio.create_task(client.writer())
        bridge.clients.add(client)
        client.q.put_nowait({"type": "hello", "name": cfg["name"], "wake_pattern": cfg["audio"]["wake_pattern"],
                             "window": cfg["audio"]["conversation_window_s"], "languages": ["en", "ru", "uz"],
                             "default_language": cfg["default_language"], "tts_engine": cfg["tts"]["engine"],
                             "local_ears": local_ears})
        asyncio.get_running_loop().run_in_executor(None, push_state)
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "command" and str(msg.get("text", "")).strip():
                    cur["lang"] = msg.get("lang", cur["lang"])
                    if brain.cancel and not brain.cancel.is_set():
                        brain.interrupt()                      # a new command cancels the running one
                    if local_ears:
                        speaker.stop()                         # typing or speaking a new command cuts the current speech
                    asyncio.create_task(run_command(str(msg["text"]).strip(), msg.get("lang", "en"), msg.get("speak", True)))
                elif kind == "interrupt":
                    brain.interrupt()
                    bridge.send({"type": "turn_done"})
                elif kind == "playback":
                    bridge.client_playing = msg.get("state") == "playing"
                    if not bridge.client_playing:
                        bridge.quiet_until = time.time() + 0.5
                elif kind == "latency" and bridge.last_timer:
                    print(bridge.last_timer.line(msg.get("client_ms")))
                elif kind == "lang" and msg.get("lang") in ("en", "ru", "uz"):
                    cur["lang"] = msg["lang"]
                elif kind == "mic_toggle" and ears_ref["listener"]:
                    ears_ref["listener"].paused = not msg.get("enabled", True)
                elif kind == "barge" and ears_ref["listener"]:
                    ears_ref["listener"].barge_in = bool(msg.get("enabled"))
        except WebSocketDisconnect:
            pass
        finally:
            bridge.clients.discard(client)
            client.q.put_nowait(None)
            writer.cancel()

    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")      # ONLY ui/ is public
    app.state.bridge, app.state.agent, app.state.brain = bridge, agent, brain
    return app


def open_ui(cfg, bridge):
    srv, url = cfg["server"], f"http://{cfg['server']['host']}:{cfg['server']['port']}"
    time.sleep(1.5)
    if bridge.clients:                                         # a tab is already connected
        return
    try:
        cmd = (["open", "-na", srv["browser"], "--args", f"--app={url}"] if srv.get("app_mode")
               else ["open", "-a", srv["browser"], url])
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            raise RuntimeError("could not launch the browser")
    except Exception:
        webbrowser.open(url)


def main():
    load_dotenv(ROOT / ".env")
    cfg = load()
    deps = build_deps(cfg)
    app = create_app(cfg, deps)
    import uvicorn
    print(f"Linda: http://{cfg['server']['host']}:{cfg['server']['port']}   (Chrome recommended)")
    uvicorn.run(app, host=cfg["server"]["host"], port=cfg["server"]["port"], log_level="warning")


if __name__ == "__main__":
    main()