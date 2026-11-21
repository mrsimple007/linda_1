"""Entry point for every Telegram message: pointer pairing, typing status, routing, start_telegram."""
import contextlib
import re
import threading
import time
import os
from linda import tools

TX_ENTRY = re.compile(r"^\s*(?:[a-zа-я]{1,10}\s*[:,]\s*)?[+\-\u2212\u2013\u2014]\s*\d", re.I)

PAIR_WINDOW = 300                 # seconds: "I want to apply for this" waits this long for the thing it means
POINTER = re.compile(r"\b(this|that|it|these|bu|shu|buni|shuni|bunga|shunga|это|этот|эту|этого|на это)\b", re.I)


def _is_pointer(t):
    """A short message that points at something not sent yet: 'I wanna apply for this', 'save this', 'buni saqla'."""
    return (len(t) <= 80 and "\n" not in t and not t.startswith("/") and bool(POINTER.search(t))
            and bool(ACT.search(t)) and not DONE_PAST.search(t) and not t.rstrip().endswith("?")
            and not EMAILISH.search(t))


EMAILISH = re.compile(r"@|https?://|\d{3,}")
ACT = re.compile(r"\b(apply|register|sign up|enrol+|participate|save|keep|note|look at|check|read|"
                 r"topshir\w*|ariza|ro'?yxatdan|qatnash\w*|saqla\w*|ko'?rib|o'qib|"
                 r"подать|подам|запиш\w*|сохрани\w*|участв\w*|посмотри)\b", re.I)
DONE_PAST = re.compile(r"\b(did|done|finished|completed|applied|saved|qildim|topshirdim|сделал)\b", re.I)


COPIED_SENDER = re.compile(r"^\s*>?\s*(?:(?:[A-ZА-ЯЁ]\.?\s?){1,3}|[\w' ]{1,30}assistant)\s*:\s*$", re.I)


def _strip_copied_sender(t):
    """Text copied out of Telegram starts with the sender line ('M. A:', 'MrSimple's Assistant:'): drop those lines."""
    lines = t.splitlines()
    while lines and (COPIED_SENDER.match(lines[0]) or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip() or t


STATUS = {"stats": "📊 Checking your bots' numbers…", "default": "⏳ Looking into it…"}


@contextlib.contextmanager
def _typing(tg, status=None):
    """Telegram shows 'typing…' for 5 s only: repeat it every 4 s. If the answer takes longer than 2 s, also show a
    short status line, and delete it once the answer is out."""
    stop, sid = threading.Event(), {}
    def keep():
        n = 0
        while not stop.is_set():
            try:
                tg.typing()
                if n == 0 and status and not stop.wait(2) and hasattr(tg, "send_status"):
                    sid["id"] = tg.send_status(status)
            except Exception:
                pass
            n += 1
            stop.wait(4 if n > 1 else 2)
    threading.Thread(target=keep, daemon=True).start()
    try:
        yield
    finally:
        stop.set()
        if sid.get("id") and hasattr(tg, "delete_message"):
            tg.delete_message(sid["id"])


def on_telegram_text(agent, tg, text):
    t = text.strip()
    quick = t.startswith("/") and t.split()[0].lower() in ("/start", "/help", "/tasks", "/notes", "/clients", "/birthdays")
    from linda.business import bot_stats
    status = None if quick else (STATUS["stats"] if bot_stats.wants_stats(agent, t) else STATUS["default"])
    with _typing(tg, status):
        return _on_telegram_text(agent, tg, text)


def _on_telegram_text(agent, tg, text):
    t = _strip_copied_sender(text.strip())
    pend = getattr(agent, "_pointer", None)
    last, agent._last_msg = getattr(agent, "_last_msg", 0), time.time()
    if pend and time.time() - pend[1] < PAIR_WINDOW and not t.startswith("/"):
        agent._pointer = None
        t = f"{pend[0]}\n\n{t}"                              # what the user meant + the thing itself, as ONE request
        print(f"[route] paired with the previous message: {pend[0][:40]!r}")
    elif (_is_pointer(t) and not getattr(agent, "_fin_pending", None)
          and time.time() - last > 120):    # "this" right after a message means that one
        agent._pointer = (t, time.time())
        print(f"[route] pointer message, waiting for what it refers to: {t[:40]!r}")
        return tg.send_styled(S[_lang(agent)]["hint_title"], icon="📎",
                              intro="Okay, send it and I'll take care of it.")
    pend = getattr(agent, "_fin_pending", None)
    if pend and not _pin_like(t, _fin_pin(agent)):
        agent._fin_pending = None                            # not a PIN: the PIN question is dropped, route normally
    if not getattr(agent, "_fin_pending", None):          # a PIN reply must reach the finance gate first
        try:
            from linda.business import clients
            if clients.telegram_text(agent, tg, t):          # before finance: "+998 ..." is not an amount
                return
            from linda.business import bot_stats
            if bot_stats.telegram_text(agent, tg, t):        # before finance: "payments in Quizzer" needs no PIN
                return
        except Exception as e:
            print("[clients]", repr(e)[:200])
    if finance_gate(agent, tg, t):
        print(f"[route] finance gate: {t[:60]!r}")
        return
    fc = getattr(agent, "finance_chat", None)
    if fc:
        reply = fc.handle(t)
        if reply is not None:
            print(f"[route] finance_chat (entry/undo/summary): {t[:60]!r}")
            return tg.send_styled("Finance", icon="💰", intro=reply)
    m = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", t, re.S)
    if m:
        print(f"[route] command /{m.group(1)}")
        cmd, rest = m.group(1).lower(), m.group(2).strip()
        return _command(agent, tg, cmd, rest)
    print(f"[route] free text -> brain: {t[:60]!r}")
    return _free_text(agent, tg, t)


def start_telegram(agent, tg):
    if os.getenv("LINDA_TELEGRAM_STANDALONE") == "1" and not getattr(agent, "is_bot", False):
        print("[telegram] слушает отдельный бот, сервер не слушает")
        return

    # 1) finance: entries, undo, /summary (must not depend on the SQL helper)
    try:
        from linda.business.finance import FinanceService
        from linda.business.finance_chat import FinanceChat
        agent.finance = FinanceService.from_config(
            chooser=lambda system, user: llm_text(agent, system, user, temp=0))
        agent.finance_chat = FinanceChat(agent.finance)
    except Exception as e:
        print("[finance] disabled:", repr(e)[:150])

    # 2) general read-only SQL questions (finance, tasks, practice, ...)
    try:
        from linda.business.finance_ask import DataAsk, DOMAINS
        fin = getattr(agent, "finance", None)
        db_path = fin.db_path if fin else str(tools.ROOT / agent.cfg.get("db_path", "linda.db"))
        agent.data_ask = DataAsk(
            db_path,
            llm=lambda system, user: llm_text(agent, system, user, temp=0),
            domains=DOMAINS,
            llm_answer=bool(fin.cfg.get("llm_answer", True)) if fin else True)
    except Exception as e:
        print("[data ask] disabled:", repr(e)[:150])

    tg.set_commands(COMMANDS)
    tg.listen(lambda text: on_telegram_text(agent, tg, text))


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import COMMANDS, S, _lang, llm_text  # noqa: E402
from linda.channels.telegram.commands import _command  # noqa: E402
from linda.channels.telegram.chat import _free_text  # noqa: E402
from linda.channels.telegram.finance_gate import _fin_pin, _pin_like, finance_gate  # noqa: E402
