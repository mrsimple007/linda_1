"""python check_setup.py   -> tells you exactly what is missing and what you still have to fill in."""
import os, pathlib, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent
MANIFEST = """server.py main.py config.yaml requirements.txt setup_data.py check_setup.py telegram_setup.py service.py
ui/index.html ui/style.css ui/app.js
linda/__init__.py linda/agent.py linda/audio.py linda/audio_analysis.py linda/board.py linda/brain.py linda/calendar_skill.py
linda/chess_game.py linda/config.py linda/docs.py linda/domains.py linda/fastpath.py linda/files.py linda/gemini.py
linda/i18n.py linda/i18n_study.py linda/planner.py linda/quizbank.py linda/recorder.py linda/router.py linda/scheduler.py
linda/stt.py linda/store.py linda/system.py linda/telegram.py linda/tools.py linda/tts.py linda/util.py linda/web.py
tests/smoke.py tests/smoke_v3.py tests/test_server.py""".split()
missing = [f for f in MANIFEST if not (ROOT / f).exists()]
print(f"-- project files: {len(MANIFEST) - len(missing)}/{len(MANIFEST)} present --")
for f in missing:
    print(f"❌ missing: {f}\n     → copy it into the project (same relative path)")
if "linda/config.py" in missing:
    sys.exit("Cannot continue without linda/config.py")
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
from linda.config import load  # noqa: E402

cfg = load()
data = pathlib.Path(cfg["data_dir"])
results = []


def check(ok, title, fix=""):
    results.append(ok)
    mark = {True: "✅", False: "❌", None: "⚠️ "}[ok]
    print(f"{mark} {title}" + (f"\n     → {fix}" if fix and ok is not True else ""))


print(f"Python {sys.version.split()[0]}")
check(True if sys.version_info >= (3, 10) else None, "Python version",
      "3.9 works but is end-of-life (Google libraries warn). Newer: brew install python@3.12, then rebuild the .venv.")

print("\n-- .env (secrets) --")
key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
check(bool(key) and "your_" not in key, "GEMINI_API_KEY", "Create a key at aistudio.google.com/apikey and put GEMINI_API_KEY=... in .env")
tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
check(bool(tok) and "token_from" not in tok, "TELEGRAM_BOT_TOKEN", "@BotFather -> /newbot -> paste the token into .env")
check(bool(chat) and "filled_by" not in chat, "TELEGRAM_CHAT_ID", "Start your bot in Telegram, then run: python telegram_setup.py")

print("\n-- config.yaml (your preferences) --")
check(True if cfg["user_name"] else None, "user_name", 'Set user_name: "YourName" so Linda greets you')
import datetime as _dt
_d = cfg["goals"]["driving_test_date"]
check(True if _d else None, f"goals.driving_test_date: {_d} ({(_dt.date.fromisoformat(_d) - _dt.date.today()).days} days away)" if _d else "goals.driving_test_date",
      'Set driving_test_date: "2027-01-10" (quoted, year-month-day) or the planner cannot count down')
print("   schedule: " + ", ".join(f"{j['id']} {j['at']}" + ("" if j.get("enabled", True) else " (off)") for j in cfg["schedule"]))
print(f"   default_language={cfg['default_language']}  quiz.language={cfg['quiz']['language']}  "
      f"birthday greetings={cfg['birthdays']['greeting_language']}  city={cfg['city']}")

print("\n-- data/ (your content) --")
check(data.exists(), "data/ folder", "python setup_data.py")
sp = data / "songs.yaml"
if sp.exists():
    import yaml
    songs = (yaml.safe_load(sp.read_text(encoding="utf-8")) or {}).get("songs") or []
    titles = {s.get("title") for s in songs}
    check(None if titles <= {"Fur Elise", "Blackbird", "C major scale"} else True,
          f"data/songs.yaml ({len(songs)} songs)", "Still the 3 examples: replace them with your real repertoire")
else:
    check(False, "data/songs.yaml", "python setup_data.py, then edit it")
from linda.quizbank import load_banks  # noqa: E402
banks = load_banks(data / "quizzes")
real = [b for b in banks if "SAMPLE" not in b]
check(True if real else None, f"quiz banks: {', '.join(f'{b} ({len(q)})' for b, q in banks.items()) or 'none'}",
      "Only the sample exists: put your real question files in data/quizzes/ (format: 1. / + / -)")
for f in cfg["docs"]["folders"]:
    n = len([p for p in (data / f).rglob("*") if p.is_file() and not p.name.startswith(".")]) if (data / f).exists() else 0
    check(True if n > 1 else None, f"data/{f}/ ({n} files)", "Add your own files here (.md .txt .pdf .docx)")

print("\n-- services --")
try:
    import ollama
    have = {getattr(m, "model", None) or m["name"] for m in ollama.list().models}
    have |= {h.split(":")[0] for h in have}
    for label, name in (("local model", cfg["local"]["model"]), ("embedding model", cfg["docs"]["embed_model"])):
        ok = name in have or name.split(":")[0] in have
        check(ok, f"Ollama {label}: {name}", f"ollama pull {name}")
except Exception as e:
    check(False, "Ollama is running", f"Start the Ollama app (or: brew services start ollama). ({type(e).__name__})")
if key:
    try:
        from google import genai
        client = genai.Client()          # keep a reference: a temporary client is closed mid-request
        client.models.generate_content(model=cfg["gemini"]["model"], contents="Reply with OK")
        check(True, f"Gemini answers with model {cfg['gemini']['model']}")
    except Exception as e:
        check(False, f"Gemini call failed ({str(e)[:90]})", "Fix the key, or set gemini.model to a name shown in AI Studio")
if tok:
    try:
        import requests
        r = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
        check(bool(r.get("ok")), "Telegram bot token", "Token rejected: copy it again from @BotFather")
    except Exception as e:
        check(None, f"Telegram not reachable ({type(e).__name__})")

print("\n-- web interface --")
check(True if pathlib.Path("/Applications/Google Chrome.app").exists() else None, "Google Chrome installed",
      "Chrome has the best speech recognition (Web Speech API). Safari/Firefox are not recommended.")
check(True if (ROOT / "ui" / "index.html").exists() else False, "ui/ folder", "Copy the ui/ folder into the project")
print("\n-- optional pieces --")
check(True if shutil.which("stockfish") else None, "Stockfish (strong chess opponent)", "brew install stockfish")
try:
    import tkinter  # noqa: F401
    check(True, "tkinter (chess board window)")
except Exception:
    check(None, "tkinter (chess board window)", "brew install python-tk@3.12  (match your Python version)")
for mod, hint in (("fastapi", "pip install fastapi"), ("httpx", "pip install httpx   (only needed to run tests/test_server.py)"), ("uvicorn", "pip install \"uvicorn[standard]\""),
                  ("edge_tts", "pip install edge-tts"), ("librosa", "pip install librosa"), ("mlx_whisper", "pip install mlx-whisper"),
                  ("sounddevice", "pip install sounddevice"), ("pypdf", "pip install pypdf"),
                  ("Foundation", "pip install pyobjc-framework-EventKit")):
    try:
        __import__(mod)
        check(True, f"python module {mod}")
    except Exception:
        check(False, f"python module {mod}", hint)
try:
    import sounddevice as sd
    name = sd.query_devices(kind="input")["name"]
    voice, music = cfg["audio"]["input_device"], cfg["audio"]["music_device"]
    if any(w in (voice or name).lower() for w in ("airpods", "bluetooth", "beats")) and not music:
        check(None, f"microphone: {voice or name} (Bluetooth)",
              'Bluetooth mics are low quality and make pitch analysis unreliable. In config.yaml under audio: set '
              'music_device: "MacBook Pro Microphone" (or an audio interface). Keep AirPods for voice if you like.')
    else:
        check(True, f"microphone: {voice or name}" + (f"  |  music mic: {music}" if music else ""))
except Exception as e:
    check(False, "microphone", f"System Settings > Privacy > Microphone: allow Terminal. ({e})")

bad = results.count(False)
print(f"\n{'All required checks passed.' if not bad else f'{bad} required item(s) need attention (marked ❌).'}"
      f"  ⚠️ items are optional or just still placeholders.")