"""Run once from the project root:   python apply_linda_patches.py
Edits linda/tools.py, linda/brain.py, server.py and linda/stt.py. Every file is backed up as <name>.bak first.
Safe to run twice: a patch that is already in place is skipped. A patch whose anchor text is not found is reported
and NOT applied (nothing is guessed)."""
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if len(sys.argv) > 1:
    ROOT = pathlib.Path(sys.argv[1]).resolve()
report = []


def patch(path, steps):
    p = ROOT / path
    if not p.exists():
        report.append(f"[skip]  {path}: file not found")
        return
    src = p.read_text(encoding="utf-8")
    out = src
    for label, done_marker, fn in steps:
        if done_marker in out:
            report.append(f"[have]  {path}: {label} (already applied)")
            continue
        new = fn(out)
        if new is None or new == out:
            report.append(f"[FAIL]  {path}: {label}: anchor not found, apply by hand")
        else:
            out = new
            report.append(f"[ok]    {path}: {label}")
    if out != src:
        shutil.copy2(p, str(p) + ".bak")
        p.write_text(out, encoding="utf-8")


def sub_once(pattern, repl, flags=0):
    def run(text):
        new, n = re.subn(pattern, repl, text, count=1, flags=flags)
        return new if n else None
    return run


def replace_once(old, new):
    return lambda text: text.replace(old, new, 1) if old in text else None


# ----------------------------------------------------------------------------- tools.py
TOOLS_BLOCK = '''

# ---- news / birthday list: built in code from RSS and the database (see briefing.py) ----
def _news(agent, a, text):
    from . import briefing
    briefing.run_news(agent, a, text)


def _birthday_list(agent, a, text):
    from . import briefing
    briefing.run_birthday_list(agent, a, text)


SPECIAL.update({"news": _news, "list_birthdays": _birthday_list})
'''
patch("linda/tools.py", [
    ("list_birthdays declaration", "never write it yourself",
     replace_once('("list_birthdays", "List upcoming birthdays.", {}, [], {}),',
                  '("list_birthdays", "List the saved birthdays. For \'send me the birthday list on Telegram\' set '
                  'telegram=true: the full list is built from the database, never write it yourself.", {**TG}, [], {}),')),
    ("telegram_send description", "Never use it for lists of stored data",
     replace_once('"Send something to the user\'s own Telegram."',
                  '"Send something to the user\'s own Telegram. Never use it for lists of stored data (birthdays, '
                  'tasks, news): call that tool with telegram=true instead."')),
    ("news + birthday_list handlers", "briefing.run_news",
     sub_once(r'(    "task_done": _task_done, "song_add": _song_add,\n\})', lambda m: m.group(1) + TOOLS_BLOCK)),
])

# ----------------------------------------------------------------------------- brain.py
BRAIN_CONST = '''NEWS_ASK = re.compile(r"\\b(?:news|headlines)\\b|новост|yangilik", re.I)
BDAY_ASK = re.compile(r"\\bbirthdays?\\b.*\\blist\\b|\\blist\\b.*\\bbirthdays?\\b|список.*дн\\w+ рожд|дни рождения.*список", re.I)
'''
BRAIN_BRANCH = '''
            elif NEWS_ASK.search(text) and len(text.split()) <= 14:      # RSS + LLM: never waits on Gemini's quota
                timer.route = "news"
                tools.execute(ag, "news", {}, text)
                self._spoke = True
            elif BDAY_ASK.search(text) and len(text.split()) <= 14:      # full list straight from the database
                timer.route = "birthdays"
                tools.execute(ag, "list_birthdays", {}, text)
                self._spoke = True
'''
patch("linda/brain.py", [
    ("NEWS_ASK / BDAY_ASK constants", "NEWS_ASK = re.compile",
     sub_once(r"(SENT = re\.compile\()", lambda m: BRAIN_CONST + m.group(1))),
    ("direct news + birthday-list routes", 'timer.route = "news"',
     sub_once(r'(            elif fastpath\.try_fast\(ag, text, self\.set_lang\):\n                timer\.route = "fast"\n)',
              lambda m: m.group(1).rstrip("\n") + BRAIN_BRANCH)),
])

# ----------------------------------------------------------------------------- server.py
TG_BLOCK = '''            if deps.tg:                                          # Telegram: /start /news ... and filing what you write
                from linda import briefing
                briefing.start_telegram(agent, deps.tg)
'''
OLD_LISTEN = re.compile(r"\n[ ]*if deps\.tg:\n[ ]*from linda import briefing\n[ ]*deps\.tg\.listen\([^\n]*\)\n")


def server_listen(text):
    if OLD_LISTEN.search(text):                                       # the earlier hand-pasted version
        return OLD_LISTEN.sub("\n" + TG_BLOCK, text, count=1)
    return sub_once(r"(            tasks\.append\(asyncio\.create_task\(refresher\(\)\)\)\n)",
                    lambda m: TG_BLOCK + m.group(1))(text)


patch("server.py", [
    ("Telegram.from_env(cfg)", "Telegram.from_env(cfg)", replace_once("Telegram.from_env()", "Telegram.from_env(cfg)")),
    ("start_telegram at startup", "briefing.start_telegram", server_listen),
])

# ----------------------------------------------------------------------------- stt.py
patch("linda/stt.py", [
    ("more Whisper hallucination phrases", "смотрите под видео",
     replace_once('if low in HALLUCINATIONS or "субтитр" in low or',
                  'if low in HALLUCINATIONS or any(x in low for x in ("субтитр", "смотрите под видео", '
                  '"подписывайтесь", "подписывайся на канал")) or')),
])

print("\n".join(report))
print("\nDone. Restart Linda. Backups: *.bak next to each changed file.")