#!/usr/bin/env python3
"""Linda Telegram bot ONLY: no web server, no microphone, no voice.

    python telegram_bot.py                    run in the terminal (Ctrl-C to stop)
    python telegram_bot.py install            start at every macOS login, restart after a crash
    python telegram_bot.py install --awake    same, and keep the Mac from idle-sleeping while it runs
    python telegram_bot.py status | logs | uninstall

It builds the same objects as server.py (via server.build_deps), but with a silent speaker, then calls
briefing.start_telegram: finance, /news, /tasks, ideas, goals and questions work as in the main app.
Free text goes through the same Brain agent loop as voice mode (recall + tools + answer in one pass).
"""
import argparse
import fcntl
import os
import pathlib
import plistlib
import signal
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent
LABEL = "com.linda.telegram"
PLIST = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = ROOT / "logs" / "telegram_bot.log"
LOCK = ROOT / "logs" / ".telegram_bot.lock"


# ---------------------------------------------------------------- launchd (autostart)
def build_plist(python, script, awake=False):
    prog = [python, script]
    if awake:
        prog = ["/usr/bin/caffeinate", "-i"] + prog
    return {
        "Label": LABEL,
        "ProgramArguments": prog,
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},   # restart after a crash, not after a normal exit
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }


def _lc(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(awake):
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        print("Run this inside the virtual environment (source .venv/bin/activate),\n"
              "otherwise the system Python would be registered for autostart.")
        sys.exit(1)
    LOG.parent.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open("wb") as f:
        plistlib.dump(build_plist(sys.executable, str(pathlib.Path(__file__).resolve()), awake), f)
    domain = f"gui/{os.getuid()}"
    _lc("bootout", f"{domain}/{LABEL}")                      # already installed? replace it
    r = _lc("bootstrap", domain, str(PLIST))
    if r.returncode:
        print("launchctl failed:", r.stderr.strip())
        sys.exit(1)
    _lc("kickstart", "-k", f"{domain}/{LABEL}")
    print(f"Installed. The bot is running and will start at every login.\nPython: {sys.executable}\nLog: {LOG}\n"
          f"Check: python telegram_bot.py status   |   Logs: python telegram_bot.py logs")


def uninstall():
    _lc("bootout", f"gui/{os.getuid()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
    print("Autostart removed.")


def status():
    r = _lc("print", f"gui/{os.getuid()}/{LABEL}")
    if r.returncode:
        print("Not installed (or not running). Install: python telegram_bot.py install")
        return
    for line in r.stdout.splitlines():
        if any(k in line for k in ("state =", "pid =", "last exit code", "runs =")):
            print(line.strip())


# ---------------------------------------------------------------- the bot itself
class NullSpeaker:
    """Stands in for the speaker: says nothing aloud. Any other speaker method is a silent no-op."""
    def say(self, *a, **k):
        pass

    def stop(self):
        pass

    def is_speaking(self):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


def no_recorder(secs):
    raise RuntimeError("There is no microphone in bot mode.")


def one_instance():
    """A second copy would fight over the bot token (Telegram error 409). Hold a lock while running."""
    LOCK.parent.mkdir(exist_ok=True)
    fh = LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[bot] already running (see: python telegram_bot.py status). Exiting.")
        sys.exit(0)
    return fh


def run(with_scheduler=False):
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    _lock = one_instance()                                   # noqa: F841  (keep the file open)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from linda.config import load
    from linda.agent import Agent
    from linda.telegram_linda.briefing import start_telegram
    import server                                            # reuse build_deps: same Store/Docs/Gemini/Telegram

    cfg = load()
    from linda import memory_files
    memory_files.init(cfg)
    cfg["calendar"]["enabled"] = False                       # no Calendar permission prompts from a background process
    deps = server.build_deps(cfg)
    if not deps.tg:
        print("[bot] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env. Run: python telegram_setup.py")
        sys.exit(1)

    threading.Thread(target=lambda: _safe_index(deps.docs), daemon=True).start()
    router = None
    try:
        from linda.router import Router
        router = Router(cfg, deps.gemini)
    except Exception:
        pass
    speaker = NullSpeaker()
    agent = Agent(cfg, deps.store, speaker, deps.gemini, router, cal=None, tg=deps.tg, docs=deps.docs,
                  recorder=no_recorder)
    agent.is_bot = True                                      # tells briefing.start_telegram: "this IS the bot, listen"
    agent.brain = None
    try:                                                     # the web Brain builds the Groq/Mistral/Gemini chain
        from linda.brain import Brain
        # KEEP the instance: briefing._converse() calls agent.brain._agent_loop(text) for every free-text message
        agent.brain = Brain(cfg, agent, speaker, deps.gemini, set_lang=lambda code: None)
    except Exception as e:
        print("[bot] Brain not started, free text will use the old classifier / Gemini only:", repr(e)[:120])
    names = [getattr(p, "name", "?") for p in (getattr(agent, "providers", None) or [])]
    print("[bot] LLM chain:", names or "Gemini only")
    print("[bot] free text ->", "Brain agent loop (one call: recall + tools + answer)" if agent.brain and names
          else "old classifier fallback (no providers)")

    if with_scheduler:
        from linda.scheduler import run_loop
        threading.Thread(target=run_loop, args=(cfg, deps.store, agent), daemon=True).start()

    start_telegram(agent, deps.tg)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print("[bot] running. Write to your bot in Telegram. Ctrl-C / SIGTERM stops it.")
    stop.wait()
    print("[bot] stopped.")


def _safe_index(docs):
    try:
        print(f"[docs] indexed {docs.index()} new/changed files")
    except Exception as e:
        print("[docs] indexing skipped:", repr(e)[:120])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Linda: Telegram bot only")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "install", "uninstall", "status", "logs"])
    ap.add_argument("--awake", action="store_true", help="install: keep the Mac from idle-sleeping")
    ap.add_argument("--with-scheduler", action="store_true", help="run: also start the daily scheduler")
    a = ap.parse_args(argv)
    if a.command == "install":
        install(a.awake)
    elif a.command == "uninstall":
        uninstall()
    elif a.command == "status":
        status()
    elif a.command == "logs":
        LOG.parent.mkdir(exist_ok=True)
        LOG.touch()
        os.execvp("tail", ["tail", "-n", "50", "-f", str(LOG)])
    else:
        run(a.with_scheduler)


if __name__ == "__main__":
    main()