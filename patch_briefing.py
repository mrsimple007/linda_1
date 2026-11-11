"""Run from the project root:   python patch_briefing.py [project_dir] [--dry]

Fixes linda/briefing.py: a Telegram message that is a question or chat (not something to file) used to get only the
classifier's one-line stub ("Sure, I can help you figure that out!"). Now it is answered by the LLM from your saved
data (goals, ideas, profile, tasks, birthdays, notes, progress, journal), the same retrieval the voice mode uses.

* Replaces only the function _free_text and adds the new helpers right before it.
* Writes a backup first (.bak, or a timestamped copy if .bak exists). Safe to run twice. --dry changes nothing.
"""
import datetime
import pathlib
import re
import shutil
import sys

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
DRY = "--dry" in sys.argv
ROOT = pathlib.Path(ARGS[0]).resolve() if ARGS else pathlib.Path(__file__).resolve().parent
P = ROOT / "linda" / "briefing.py"

NEW = r'''# ============================ answering questions =================================
# A question or chat is not filed: it is answered from the user's own saved data (same retrieval as voice mode).
QUESTION = re.compile(r"\?\s*$|^\s*(?:tell me|can you|can u|could you|could u|write (?:me|a|the)|скажи|напиши|покажи|"
                      r"можешь|ayt|yozib ber)\b", re.I)
BDAY_WORDS = re.compile(r"birthday|wish|greeting|congrat|поздрав|день рожд|tug'?ilgan|tabrik", re.I)
GREET_WORDS = re.compile(r"\bwish|greeting|congrat|поздрав|tabrik|\bmessage\b|\bletter\b", re.I)
ANSWER = """You are Linda, {user}'s personal assistant, chatting with him in Telegram. Today is {today}.
The DATA block is {user}'s own saved information (goals, ideas, profile, tasks, birthdays, notes, progress, journal).
Rules:
- Answer from DATA and RECENT CHAT. If the answer is not in DATA, say plainly that you do not have it saved. Never invent dates, names or facts.
- Birthdays in DATA are sorted by date: the first one is the NEXT birthday. "in_days" counts days from today.
- If asked to write a wish, greeting or message, write it IN FULL (warm, natural, personal, 3 to 5 sentences) for the right person, using the relation and "greeting_lang" from DATA (en, ru or uz). If no person is named, use the next birthday. Output only the message, ready to forward.
- Otherwise answer briefly (1 to 4 sentences) in the language of {user}'s message. No markdown, no pep-talk closers such as "good luck".
- A task due date is a deadline, not a scheduled appointment."""


def _hist(agent):
    h = getattr(agent, "tg_hist", None)
    if h is None:
        h = agent.tg_hist = []
    return h


def _context(agent, text):
    """Saved data relevant to the message, as JSON text from the same retrieval the voice mode uses."""
    parts = []

    def add(label, fn):
        try:
            v = fn()
        except Exception as e:
            print(f"[telegram context {label}]", repr(e)[:120])
            return
        if v:
            parts.append(f"{label}: {v}")
    add("matches", lambda: tools.prefetch(agent, text, budget=1500))                       # words in the message
    add("overview", lambda: tools.prefetch(agent, text, budget=2400, think=True))          # goals, ideas, tasks, profile...
    if BDAY_WORDS.search(text):
        add("birthdays", lambda: tools.recall_data(agent, {"kind": "birthdays", "query": text}, text, budget=2000))
    return "\n".join(parts)


def _answer(agent, tg, text, fallback=""):
    t = S[_lang(agent)]
    user = agent.cfg.get("user_name") or "the user"
    hist = _hist(agent)
    chat = "\n".join(f"{who}: {m}" for who, m in hist[-6:])
    prompt = (f"DATA:\n{_context(agent, text) or '(nothing relevant found)'}\n\n"
              + (f"RECENT CHAT:\n{chat}\n\n" if chat else "") + f"MESSAGE from {user}:\n{text}")
    system = ANSWER.format(user=user, today=datetime.date.today().strftime("%A %Y-%m-%d"))
    ans = llm_text(agent, system, prompt, temp=0.4)
    ans = re.sub(r"\*\*|`|^#+\s*", "", ans or "", flags=re.M).strip()
    if not ans:
        return tg.send_styled(t["hint_title"], icon="💬", intro=fallback or t["no_ai"])
    hist += [("User", text[:300]), ("Linda", ans[:300])]
    del hist[:-8]
    if GREET_WORDS.search(text):
        return tg.send(ans)                                  # plain text: easy to forward
    return tg.send_styled(t["hint_title"], icon="💬", intro=ans)


def _free_text(agent, tg, text):
    t = S[_lang(agent)]
    in_window = time.time() < getattr(agent, "plans_until", 0)
    tg.typing()
    if "\n" not in text and QUESTION.search(text):           # clearly a question: answer it, do not file it
        return _answer(agent, tg, text)
    items, reply = classify(agent, text, in_window)
    if items is None:                                        # no LLM answered
        if in_window:
            return _plans_reply(agent, tg, parse_plans(text)) if parse_plans(text) else \
                tg.send_styled(t["hint_title"], icon="💬", intro=t["empty"])
        return tg.send_styled(t["hint_title"], icon="💬", intro=t["no_ai"])
    if in_window:
        for it in items:
            if it["type"] == "task" and not it["due"]:
                it["due"] = str(datetime.date.today())
        agent.plans_until = time.time() + PLANS_WINDOW
    if not items:                                            # chat / question the shortcut missed
        return _answer(agent, tg, text, fallback=reply or t["nothing"])
    return _save_items(agent, tg, items, text)'''

src = P.read_text(encoding="utf-8") if P.exists() else None
if src is None:
    sys.exit(f"[FAIL] {P} not found")
if "def _answer(" in src:
    print("[have] linda/briefing.py: already patched")
    sys.exit(0)
pat = re.compile(r"def _free_text\(agent, tg, text\):\n.*?(?=\n\n\n# =+ commands)", re.S)
if not pat.search(src):
    sys.exit("[FAIL] could not find def _free_text(...) followed by the '# ==== commands' header; nothing changed")
out = pat.sub(lambda m: NEW, src, count=1)
if DRY:
    print("[ok]   dry run: _free_text would be replaced, nothing written")
    sys.exit(0)
bak = P.with_name(P.name + ".bak")
if bak.exists():
    bak = P.with_name(P.name + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
shutil.copy2(P, bak)
P.write_text(out, encoding="utf-8")
print(f"[ok]   linda/briefing.py patched (backup: {bak.name}). Restart Linda.")