import concurrent.futures, re, threading, time
from linda import fastpath

POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
ALICE_RULES = ("Channel: voice through a Yandex Station. Reply in 1-2 short sentences, no markdown or emoji. "
               "The user's data is stored in English: search it with English words, then answer in the user's language.")


def _think(agent, text):
    from linda.telegram_linda.briefing import muted
    brain = agent.brain
    with muted(agent, text) as mute:
        if agent.chess and agent.chess_turn(text, cloud=False):           # a game in progress: moves, "подсказка", "стоп шахматы"
            return " ".join(mute.lines)
        if fastpath.try_fast(agent, text, lambda c: None):
            return " ".join(mute.lines)
        brain.cancel = threading.Event()
        brain.extra_rules = ALICE_RULES
        try:
            brain._agent_loop(text)
        finally:
            brain.extra_rules = ""
    return " ".join(mute.lines)


def handle(agent, payload, owner_id):
    sess = payload.get("session", {})
    uid = (sess.get("user") or {}).get("user_id") or sess.get("user_id")
    base = {"version": payload.get("version", "1.0")}
    req = payload.get("request", {})
    if not owner_id or uid != owner_id:                     # every request type, not only spoken ones
        print(f"[alice] refused user_id={uid} (put your own in .env as ALICE_OWNER_ID)")
        return {**base, "response": {"text": "Нет доступа.", "end_session": True}}
    text = (req.get("original_utterance") or "").strip()
    if sess.get("new") or not text:
        return {**base, "response": {"text": "Линда на связи. Слушаю.", "end_session": False}}
    if text.lower() in ("хватит", "выход", "стоп"):
        agent.alice_until = time.time() + 120                # session over: the Mac listens again in 2 minutes
        return {**base, "response": {"text": "Пока!", "end_session": True}}
    sid = sess.get("session_id") or ""
    names = [str(agent.cfg.get("user_name") or "")]
    late = LATE.get(sid)
    if late is not None and AGAIN.search(text):                         # "ну что?" -> the answer that was too slow last time
        ans = late.pop("answer", None)
        if ans:
            LATE.pop(sid, None)
            return _reply(base, ans, names)
        return _reply(base, "Ещё думаю. Спроси «ну что» через пару секунд.")
    print(f"[alice] heard: {text[:120]!r}")
    if any(not f.done() for f in BUSY):                     # still answering the last one: don't queue behind it
        return _reply(base, "Ещё отвечаю на прошлый вопрос. Скажи «ну что» через пару секунд.")
    t0 = time.time()
    fut = POOL.submit(_think, agent, text)
    BUSY[:] = [fut]
    try:
        answer = fut.result(timeout=DEADLINE)
        print(f"[alice] {time.time() - t0:.2f}s")
    except concurrent.futures.TimeoutError:
        LATE.clear()                                        # one pending answer per session is enough
        slot = LATE[sid] = {}

        def later(f):
            try:
                slot["answer"] = f.result()
                agent.alice_until = max(getattr(agent, "alice_until", 0), time.time() + 120)
                print(f"[alice late] {time.time() - t0:.2f}s")
                agent.tg.send(slot["answer"])
            except Exception as e:
                print("[alice late]", repr(e)[:100])
        fut.add_done_callback(later)
        answer = "Секунду, думаю. Скажи «ну что», и я отвечу. Ответ также придёт в Телеграм."
    return _reply(base, answer, names)


DEADLINE = 2.6                       # Alice waits ~3 s, the tunnel needs the rest
LATE = {}                            # session_id -> {"answer": ...} for answers that missed the deadline
BUSY = []                            # the answer being worked on
AGAIN = re.compile(r"^\W*(ну\s+)?(что|и что|как|ну|готово|ответ|и)\W*$|ну что|что там|ответ", re.I)
LAT = {"sh": "ш", "ch": "ч", "yo": "ё", "yu": "ю", "ya": "я", "ts": "ц", "kh": "х", "zh": "ж", "o'": "у", "g'": "г",
       "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "ж", "k": "к",
       "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
       "w": "в", "x": "х", "y": "й", "z": "з"}
_LAT = re.compile("|".join(sorted(map(re.escape, LAT), key=len, reverse=True)), re.I)


def _cyr(word):
    out = _LAT.sub(lambda m: LAT[m.group(0).lower()], word)
    return out[:1].upper() + out[1:] if word[:1].isupper() else out


def _tts(text, names=()):
    """Alice reads Latin words with an English accent: spell the user's name in Cyrillic when the reply is Russian
    (product names such as 'Simple Slides' stay as they are)."""
    names = [n for n in names if n]
    if not names or not re.search("[а-яё]", text, re.I):
        return text
    pat = r"\b(" + "|".join(map(re.escape, names)) + r")\b"
    return re.sub(pat, lambda m: _cyr(m.group(0)), text, flags=re.I)


def _reply(base, answer, names=()):
    answer = (answer or "Готово.")[:1000]
    return {**base, "response": {"text": answer, "tts": _tts(answer, names)[:1000], "end_session": False}}
