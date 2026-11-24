"""Finance in Telegram: PIN gate, summary / recent cards, questions about money."""
import datetime
import re
import time
import os
from linda.channels.telegram.telegram import esc
from linda.business.finance import FinanceError
import hmac
from linda.business.finance import fmt_amount

FIN_WORD = re.compile(
    r"\b(revenue|income|expenses?|expences?|spend|spent|spending|earned|profit|finances?|purchases?|bought|"
    r"transactions?|payments?|balance|summary)\b|доход|расход|выручк|прибыл|покупк|трат|"
    r"daromad|xarajat|xarid", re.I)
FIN_CMDS = ("/summary", "summary", "/balance", "balance", "/finance")
LAST_MONTH = re.compile(r"last month|previous month|прошл\w+ месяц|o'?tgan oy", re.I)
RECENT = re.compile(r"\b(latest|last|recent|newest)\b|последн|oxirgi", re.I)
REPORT_Q = re.compile(r"revenue|income|expenses?|expences?|summary|balance|report|profit|finances?|"
                      r"доход|расход|daromad|xarajat", re.I)
INCOME_Q = re.compile(r"income|revenue|earn|доход|daromad", re.I)
MY_WORDS = re.compile(r"my[\s_-]?finance|personal|private|личн|shaxsiy", re.I)
CO_WORDS = re.compile(r"company|business|\bbiz\b|kompaniya|компани", re.I)
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
SHEETS = {"my_finance": ("👤", "Personal"), "company": ("🏢", "Company")}
FIN_SESSION, FIN_LOCK = 300, 900


def _fin_month(t):
    m = re.search(r"\b(\d{4}-\d{2})\b", t)
    if m:
        return m.group(1)
    today = datetime.date.today()
    if LAST_MONTH.search(t):
        return (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    low = t.lower()
    for i, name in enumerate(MONTHS_EN, 1):
        if re.search(rf"\b{name.lower()}\b", low):
            return f"{today.year - (i > today.month)}-{i:02d}"
    return None                                     # None = this month


def _month_label(m):
    y, mo = map(int, (m or datetime.date.today().strftime("%Y-%m")).split("-"))
    return f"{MONTHS_EN[mo - 1]} {y}"


def _fin_sheets(q):
    my, co = bool(MY_WORDS.search(q)), bool(CO_WORDS.search(q))
    return ["my_finance"] if my and not co else ["company"] if co and not my else ["my_finance", "company"]


def _money(n, sign=False):
    s = fmt_amount(abs(int(n)))
    return ("−" if n < 0 else "+" if sign and n > 0 else "") + s


def _bar(share, n=10):
    k = max(0, min(n, round(share * n)))
    return "▰" * k + "▱" * (n - k)


def _summary_card(tg, svc, sheet, s, month):
    icon, name = SHEETS[sheet]
    title = f"{name} finance · {_month_label(month)}"
    d = s["sheets"].get(sheet)
    if not d:
        return tg.send_styled(title, icon=icon, intro="No entries for this month yet.")
    inc, exp, bal = d["income"], d["expense"], d["balance"]
    sections = [("", [f"🟢 Income: <b>{_money(inc)}</b>", f"🔴 Expenses: <b>{_money(exp)}</b>",
                      f"{'💰' if bal >= 0 else '⚠️'} Balance: <b>{_money(bal, sign=True)}</b>"])]
    if d["top"]:
        lines = []
        for i, (n, t) in enumerate(d["top"], 1):
            share = t / exp if exp else 0
            lines.append(f"{i}. {esc(n)}\n    <b>{_money(t)}</b> · {_bar(share)} {share:.0%}")
        sections.append(("Biggest expenses", lines))
    return tg.send_styled(title, sections, footer="UZS · only entries made through Linda", icon=icon)


def _recent_card(tg, svc, sheet, tx_type, n=5):
    icon, name = SHEETS[sheet]
    rows = svc.recent(sheet, tx_type, n)
    kind = "income" if tx_type == "income" else "expenses"
    title = f"{name} · latest {len(rows) if rows else n} {kind}"
    if not rows:
        return tg.send_styled(title, icon=icon, intro="Nothing recorded yet.")
    sign = "🟢 +" if tx_type == "income" else "🔴 −"
    lines = []
    for r in rows:
        d = datetime.date.fromisoformat(r["tx_date"])
        lines.append(f"{sign}{fmt_amount(r['amount'])}  <b>{esc(r['description'])}</b>\n    {d:%d.%m.%Y}")
    return tg.send_styled(title, [("", lines)], footer="UZS", icon=icon)

FIN_PLAN = """Classify one message sent to a personal assistant. Today is {today}.
Return ONLY JSON with these keys: finance, sheet, view, type, month, limit. Fill each from THE MESSAGE, not from habit.
- finance: true ONLY if the user asks to READ money data (totals, revenue, expenses, balance, latest purchases).
  Reporting finished work, tasks, or to-dos is false even if the word "finance" appears.
- sheet: "my_finance" ONLY if the message says personal/private/my own; "company" ONLY if it says company/business;
  otherwise "both".
- view: "recent" = a list of individual entries (latest/last N purchases, expenses, payments, income);
  "summary" = totals of a month; "balance" = how much is left overall; "custom" = any other money question
  (biggest purchase, spending on one thing, a date range, a comparison).
- type: "income" or "expense" (purchases, spending, payments = expense).
- month: "YYYY-MM" only for view=summary when a month is named; otherwise null.
- limit: how many entries for view=recent (the number the user asked for, default 5, at most 50).
Examples:
"latest 10 purchases" -> {{"finance": true, "sheet": "both", "view": "recent", "type": "expense", "month": null, "limit": 10}}
"company revenue in October" -> {{"finance": true, "sheet": "company", "view": "summary", "type": "income", "month": "YYYY-10", "limit": 5}}
"how much did I spend on taxi this month" -> {{"finance": true, "sheet": "both", "view": "custom", "type": "expense", "month": null, "limit": 5}}"""

COUNT = re.compile(r"\b(\d{1,2})\b")
TOTAL_Q = re.compile(r"summary|total|overall|итог|всего|jami", re.I)


def _fin_limit(text, value=None):
    """The number of entries asked for ('latest 10 purchases' -> 10), 5 if none, never above 50."""
    for v in (value, *(m.group(1) for m in COUNT.finditer(text))):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 50:
            return n
    return 5


def _fin_plan(agent, text):
    """LLM understands the question; if every LLM is down, the old regexes decide."""
    raw = llm_text(agent, FIN_PLAN.format(today=datetime.date.today()), text, temp=0)
    obj = _json_obj(raw)
    if isinstance(obj, dict) and "finance" in obj:
        sheet = obj.get("sheet")
        view = obj.get("view")
        month = obj.get("month")
        if not (isinstance(month, str) and re.fullmatch(r"\d{4}-\d{2}", month)):
            month = None
        view = view if view in ("summary", "recent", "balance", "custom") else "summary"
        if (view == "summary" and RECENT.search(LAST_MONTH.sub("", text)) and not LEFT_Q.search(text)
                and not TOTAL_Q.search(text)):
            view = "recent"                   # "latest/last N ..." is a list of entries, whatever the model said
        return {"finance": bool(obj["finance"]),
                "sheets": [sheet] if sheet in SHEETS else ["my_finance", "company"],
                "view": view,
                "type": "income" if obj.get("type") == "income" else "expense",
                "month": month, "limit": _fin_limit(text, obj.get("limit"))}
    # fallback: the regex logic
    view = ("balance" if LEFT_Q.search(text) else "recent" if RECENT.search(LAST_MONTH.sub("", text))
            else "summary" if REPORT_Q.search(text) else "custom")
    return {"finance": not TASKISH.search(text), "sheets": _fin_sheets(text), "view": view,
            "type": "income" if INCOME_Q.search(text) else "expense", "month": _fin_month(text),
            "limit": _fin_limit(text)}

def _fin_answer(agent, tg, query):
    svc = getattr(agent, "finance", None)
    if svc is None:
        return tg.send_styled("Finance", icon="💰", intro="Finance is not set up.")
    agent._fin_last = (query, time.time())               # a follow-up like "November" refines this question
    p = _fin_plan(agent, query)
    print(f"[finance] plan={p}")
    try:
        if p["view"] == "balance":
            for sh in p["sheets"]:
                _balance_card(tg, svc, sh)
        elif p["view"] == "recent":
            for sh in p["sheets"]:
                _recent_card(tg, svc, sh, p["type"], p.get("limit", 5))
        elif p["view"] == "summary":
            s = svc.summary(p["month"])
            for sh in p["sheets"]:
                _summary_card(tg, svc, sh, s, p["month"])
        else:
            da = getattr(agent, "data_ask", None)
            if da is None:
                return tg.send_styled("Finance", icon="💰", intro="I can show reports, balances and latest entries.")
            for sh in p["sheets"]:
                icon, name = SHEETS[sh]
                ans = da.ask(f"{query} (use only rows where sheet = '{sh}')", "finance")
                tg.send_styled(f"{name} finance", icon=icon, intro=ans)
    except FinanceError as e:
        tg.send_styled("Finance", icon="💰", intro=str(e))
    except Exception as e:
        print("[finance ask]", repr(e)[:150])
        tg.send_styled("Finance", icon="💰", intro=f"I couldn't read the finance data: {e}")

def _pin_like(t, pin):
    """Could this reply be a PIN attempt? One word, 3-20 characters, and digits in it if the PIN has digits
    ('November' after the PIN question is a new message, not a wrong PIN)."""
    if pin and hmac.compare_digest(t, pin):
        return True                                            # the PIN itself, even "2024"-looking digits
    if not re.fullmatch(r"\S{3,20}", t):
        return False
    if t.isdigit() and pin.isdigit():
        return True                                            # digits after the PIN question = a PIN attempt
    if any(ch.isdigit() for ch in pin) and not any(ch.isdigit() for ch in t):
        return False
    from linda.business import bot_stats                       # months, bot names, stats/finance words are never a PIN
    return not (bot_stats.REFINE.search(t) or bot_stats.STATS_Q.search(t) or FIN_WORD.search(t)
                or re.search(r"quizzer|slides?|bots?", t, re.I))


FOLLOW_UP = 120          # seconds a short follow-up still belongs to the last finance question


def fin_refine(agent, tg, t):
    """'November', 'company only', 'for my finance' sent right after a finance question refine THAT question
    (it used to drop the question and reach the brain alone, which answered with tasks)."""
    if len(t) > 40 or "\n" in t or t.startswith("/") or _pin_like(t, _fin_pin(agent)):
        return False
    from linda.business import bot_stats
    if not (bot_stats.REFINE.search(t) or MY_WORDS.search(t) or CO_WORDS.search(t)):
        return False
    now = time.time()
    pend, last = getattr(agent, "_fin_pending", None), getattr(agent, "_fin_last", None)
    stats = getattr(agent, "_last_stats", None)
    if pend and now < pend[1]:
        agent._fin_pending = (f"{pend[0]} ({t})", now + 120)
        tg.send_styled("Finance", icon="🔒", intro=f"Noted: {esc(t)}. Send your finance PIN to see this.")
        return True
    if (last and now - last[1] < FOLLOW_UP and now < getattr(agent, "_fin_until", 0)
            and not (stats and stats[2] > last[1])):           # a later bot-stats question owns the follow-up
        _fin_answer(agent, tg, f"{last[0]} ({t})")
        return True
    return False


def _fin_pin(agent):
    return str(agent.cfg.get("finance_pin") or os.getenv("LINDA_FINANCE_PIN") or "")


def finance_request(agent, tg, question):
    """The ONLY door to finance reads. Pure code: PIN, lockout, session."""
    now, pin = time.time(), _fin_pin(agent)
    if not pin:
        tg.send_styled("Finance", icon="🔒", intro="Set finance_pin in config.yaml (or LINDA_FINANCE_PIN) first.")
    elif now < getattr(agent, "_fin_lock", 0):
        tg.send_styled("Finance", icon="🔒", intro="Locked for a few minutes after wrong PINs.")
    elif now < getattr(agent, "_fin_until", 0):
        _fin_answer(agent, tg, question)
    else:
        agent._fin_pending = (question, now + 120)
        tg.send_styled("Finance", icon="🔒", intro="Send your finance PIN to see this.")

def finance_gate(agent, tg, t):
    """True when the message was handled here. Guards every finance READ (questions and /summary)."""
    now, pin = time.time(), _fin_pin(agent)
    fc = getattr(agent, "finance_chat", None)
    low = t.lower()
    pend = getattr(agent, "_fin_pending", None)
    if pend and now < pend[1] and _pin_like(t, pin) and not (fc and fc.matches(t)):
        agent._fin_pending = None
        tg.delete_later(getattr(tg, "last_message_id", None))      # the PIN must not stay in the chat
        if now < getattr(agent, "_fin_lock", 0):
            tg.send_styled("Finance", icon="🔒", intro="Locked for a few minutes after wrong PINs.")
        elif pin and hmac.compare_digest(t, pin):
            agent._fin_until, agent._fin_fails = now + FIN_SESSION, 0
            _fin_answer(agent, tg, pend[0])
        else:
            agent._fin_fails = getattr(agent, "_fin_fails", 0) + 1
            if agent._fin_fails >= 3:
                agent._fin_lock, agent._fin_fails = now + FIN_LOCK, 0
            tg.send_styled("Finance", icon="🔒", intro="Wrong PIN.")
        return True
    agent._fin_pending = None
    cmd = low.split(" ")[0] in FIN_CMDS
    is_read = cmd or (bool(FIN_WORD.search(t)) and _fin_plan(agent, t)["finance"])
    if not is_read or TX_ENTRY.match(t):
        return False                      # everything else reaches the brain, which can call finance_report
    finance_request(agent, tg, t)
    return True


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import LEFT_Q, TASKISH, _balance_card, _json_obj, llm_text  # noqa: E402
from linda.channels.telegram.routing import TX_ENTRY  # noqa: E402
