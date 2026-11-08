"""Linda: hybrid voice assistant. Small local model routes commands; Gemini does the heavy work.
One central database; scheduler for briefings, practice reminders and evening check-ins."""
import datetime, os, pathlib, re, sys, threading, time
from dotenv import load_dotenv
from linda.config import load
from linda.audio import Listener, record_audio
from linda.audio_analysis import SR
from linda.stt import STT
from linda.tts import Speaker
from linda.store import Store
from linda.router import Router
from linda.agent import Agent
from linda.docs import Docs
from linda.i18n import LANGS, t
from linda.scheduler import run_loop
from linda.telegram import Telegram
from linda.util import online
from linda import system

STOP = {"stop", "cancel", "стоп", "хватит", "to'xta", "toxta"}


def enter_to_stop(speaker):
    """Press Enter in the terminal to cut Linda off mid-sentence."""
    while True:
        if not sys.stdin.readline():
            return
        speaker.stop()


def calendar_watcher(cfg, agent, cal):
    mins, lang, seen = cfg["calendar"]["remind_minutes"], cfg["default_language"], set()
    while True:
        try:
            now = datetime.datetime.now()
            for e in cal.events(now, now + datetime.timedelta(minutes=mins + 1)):
                left = (e.start - now).total_seconds() / 60
                key = (e.id, e.start.isoformat())
                if not e.all_day and 0 < left <= mins and key not in seen:
                    seen.add(key)
                    line = t("cal_soon", lang, n=max(1, round(left)), title=e.title)
                    system.notify(cfg["name"], line)
                    agent.say(line, lang)
        except Exception as e:
            print("[calendar watcher]", e)
        time.sleep(30)


def main():
    load_dotenv()
    cfg = load()
    data_dir = pathlib.Path(cfg["data_dir"]).expanduser()
    gemini = None
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        from linda.gemini import Gemini
        gemini = Gemini(cfg["gemini"])
    else:
        print("[!] GEMINI_API_KEY not set: running local-only.")

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

    store = Store(cfg["db_path"])
    n = store.import_songs(data_dir / "songs.yaml")
    print(f"[songs] {n} imported from data/songs.yaml" if n else "[songs] none yet: edit data/songs.yaml")
    docs = Docs(store, cfg["docs"], data_dir)
    threading.Thread(target=lambda: print(f"[docs] indexed {docs.index()} new/changed files"), daemon=True).start()

    speaker = Speaker(cfg["tts"])
    listener = Listener(cfg["audio"], speaker.is_speaking)
    stt = STT(cfg["stt"])
    router = Router(cfg, gemini)
    agent = Agent(cfg, store, speaker, gemini, router, cal=cal, tg=tg, docs=docs,
                  recorder=lambda secs: record_audio(secs, SR, listener))
    threading.Thread(target=router.warm, daemon=True).start()
    threading.Thread(target=enter_to_stop, args=(speaker,), daemon=True).start()
    threading.Thread(target=run_loop, args=(cfg, store, agent), daemon=True).start()
    if cal:
        threading.Thread(target=calendar_watcher, args=(cfg, agent, cal), daemon=True).start()

    wake = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|эй|привет)[,\s]+)?{cfg['audio']['wake_pattern']}\b[,.!?:]?\s*(.*)",
                      re.I | re.S)

    def strip_wake(text):
        m = wake.match(text)
        return (True, m.group(2).strip()) if m else (False, text.strip())

    window, lang0 = cfg["audio"]["conversation_window_s"], cfg["default_language"]
    speaker.say(t("ready", lang0), lang0)
    print(f"{cfg['name']} is listening. Say '{cfg['name']}, ...'  (Enter = interrupt, Ctrl-C = quit)")

    while True:
        audio = listener.utterance()
        text, lang = stt.transcribe(audio)
        if not text:
            continue
        print(f"[heard:{lang}] {text}")
        woke, command = strip_wake(text)
        if not (woke or time.time() < agent.active_until):
            continue
        if cfg["stt"]["refine_with_gemini"] and gemini and online():
            try:
                t2, l2 = gemini.transcribe(audio)
                if t2:
                    woke2, c2 = strip_wake(t2)
                    command = c2 if woke2 else t2
                    lang = l2 if l2 in LANGS else lang
                    print(f"[gemini:{lang}] {t2}")
            except Exception as e:
                print("[gemini stt failed]", e)
        if not command:
            speaker.say(t("listening", lang), lang)
            agent.active_until = time.time() + window
            continue
        if command.lower().strip(" .!?") in STOP and not (agent.chess or agent.quiz):
            speaker.stop()
            continue
        agent.run(command, lang)
        agent.active_until = time.time() + window


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")