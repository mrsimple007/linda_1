import concurrent.futures, threading
from linda import fastpath

POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
ALICE_RULES = ("Channel: voice through a Yandex Station. Reply in 1-2 short sentences, no markdown or emoji. "
               "Use smart_home for lamps and devices.")


def _think(agent, text):
    from linda.telegram_linda.briefing import muted
    brain = agent.brain
    with muted(agent, text) as mute:
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
        return {**base, "response": {"text": "Нет доступа.", "end_session": True}}
    text = (req.get("original_utterance") or "").strip()
    if sess.get("new") or not text:
        return {**base, "response": {"text": "Линда на связи. Слушаю.", "end_session": False}}
    if text.lower() in ("хватит", "выход", "стоп"):
        return {**base, "response": {"text": "Пока!", "end_session": True}}
    fut = POOL.submit(_think, agent, text)
    try:
        answer = fut.result(timeout=2.5)
    except concurrent.futures.TimeoutError:
        def later(f):
            try:
                agent.tg.send(f.result())
            except Exception as e:
                print("[alice late]", repr(e)[:100])
        fut.add_done_callback(later)
        answer = "Думаю долго, ответ пришлю в Телеграм."
    return {**base, "response": {"text": answer[:1000] or "Готово.", "end_session": False}}