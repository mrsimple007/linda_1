"""Linda writes first: deadline reminders, agency client follow-ups and the Sunday weekly review (Telegram).

Run by the scheduler (config.yaml `schedule`: reminders, client_followups, weekly_review) or by hand with
/reminders, /followups, /review. Every number and date here is computed in code; no LLM is involved.
Settings (config.yaml `proactive`, all optional):
  reminder_days: [7, 3, 1, 0]   remind this many days before a task's due date (0 = on the day)
  overdue_days: 3               keep reminding about an overdue task for this many days
  client_idle_days: 7           a client with no progress for this long is listed in the follow-ups
  review_finance: false         add money in / out to the weekly review (finance is PIN-protected in chat)"""
import datetime, html, pathlib, re, sqlite3, time

DEFAULTS = {"reminder_days": [7, 3, 1, 0], "overdue_days": 3, "client_idle_days": 7, "review_finance": False}
WEEK_KEY = "weekly_plans_until"        # store key: until when a reply counts as next week's plans
WEEK_HINT = ("The user is answering the weekly review question 'what matters most next week'. Each plan they list "
             "is a NEW task (task_add, one call per plan; due only if they name a day). Never re-add an open task.")


def _cfg(agent):
    return {**DEFAULTS, **(agent.cfg.get("proactive") or {})}


def _esc(s):
    return html.escape(str(s), quote=False)


def _db(agent):
    p = pathlib.Path(agent.cfg["db_path"]).expanduser()
    return sqlite3.connect(p, timeout=10)


def _when(n):
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n < 0:
        return f"overdue by {-n} day" + ("s" if n < -1 else "")
    return f"in {n} days"


# ---------------------------------------------------------------- 1. reminders
def reminders(agent, today=None):
    """-> (title, sections) or None. Tasks hitting a reminder day, overdue tasks, today's / tomorrow's birthdays."""
    c, today = _cfg(agent), today or datetime.date.today()
    days, overdue = {int(x) for x in c["reminder_days"]}, int(c["overdue_days"])
    soon, late = [], []
    for r in agent.store.open_tasks():
        try:
            due = datetime.date.fromisoformat(str(r[2])[:10]) if r[2] else None
        except ValueError:
            due = None
        if not due:
            continue
        n = (due - today).days
        line = f"• {_esc(r[1])} <i>({_when(n)}, {due:%a %d %b})</i>"
        if n in days:
            soon.append((n, line))
        elif -overdue <= n < 0:
            late.append((n, line))
    sections = []
    if late:
        sections.append(("⚠️ Overdue", [l for _, l in sorted(late)]))
    if soon:
        sections.append(("⏰ Coming up", [l for _, l in sorted(soon)]))
    try:
        bdays = [b for b in agent.store.upcoming(1) if b.n in (0, 1)]
    except Exception as e:
        print("[proactive birthdays]", repr(e)[:100])
        bdays = []
    if bdays:
        lines = [f"🎂 <b>{_esc(b.name)}</b>" + (f" ({_esc(b.relation)})" if getattr(b, "relation", "") else "")
                 + f" · {'today' if b.n == 0 else 'tomorrow'}" for b in bdays]
        lines.append("<i>Write «greeting for NAME» and I'll draft one.</i>")
        sections.append(("Birthdays", lines))
    return ("Reminders", sections) if sections else None


# ---------------------------------------------------------------- 2. client follow-ups
def followups(agent, now=None):
    """-> (title, sections) or None: agency clients with steps left and no progress for client_idle_days."""
    now = now or time.time()
    idle_days = int(_cfg(agent)["client_idle_days"])
    con = _db(agent)
    try:
        try:
            clients = con.execute("select id, name, created_ts, updated_ts from clients").fetchall()
            steps = con.execute("select client_id, step_no, step_name, done, done_date from client_steps").fetchall()
        except sqlite3.OperationalError:              # clients base not set up
            return None
    finally:
        con.close()
    by = {}
    for cid, no, name, done, ddate in steps:
        by.setdefault(cid, []).append((no, name, done, ddate))
    rows = []
    for cid, name, created, updated in clients:
        st = sorted(by.get(cid, []))
        left = [s for s in st if not s[2]]
        if not st or not left:
            continue                                   # no steps tracked, or everything done
        last = max(x for x in (created or 0, updated or 0) if x is not None)
        for _, _, done, ddate in st:
            if done and ddate:
                try:
                    last = max(last, datetime.datetime.fromisoformat(str(ddate)[:10]).timestamp())
                except ValueError:
                    pass
        idle = int((now - last) // 86400) if last else 999
        if idle >= idle_days:
            nxt = left[0]
            rows.append((idle, f"• <b>{_esc(name)}</b>: next {nxt[0]} {_esc(nxt[1])} "
                               f"<i>({'no progress yet' if idle >= 999 else f'{idle} days idle'})</i>"))
    if not rows:
        return None
    rows.sort(key=lambda x: -x[0])
    lines = [l for _, l in rows[:15]] + ([f"<i>…and {len(rows) - 15} more</i>"] if len(rows) > 15 else [])
    return (f"Clients waiting ({len(rows)})", [("Need action", lines + ["<i>Update one: «Name: step 4 done».</i>"])])


# ---------------------------------------------------------------- 3. weekly review
def _week(today):
    start = today - datetime.timedelta(days=6)        # the last 7 days, today included
    return start, today


def _done_tasks(agent, start, end):
    con = _db(agent)
    try:
        rows = con.execute("select title, done_ts from tasks where status='done' and done_ts is not null").fetchall()
    finally:
        con.close()
    out = []
    for title, ts in rows:
        try:
            d = datetime.date.fromtimestamp(float(ts))
        except (TypeError, ValueError):
            continue
        if start <= d <= end:
            out.append(title.strip().rstrip(".!"))
    return out


def _progress(agent, start, end):
    """{project: [lines]} from data/projects/*.md lines '- YYYY-MM-DD · text' inside the week."""
    out = {}
    pdir = pathlib.Path(agent.data_dir) / "projects"
    for p in sorted(pdir.glob("*.md")) if pdir.exists() else []:
        if p.name.lower() == "readme.md":
            continue
        head = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        title = head[0].lstrip("# ").split(":")[0].strip() if head and head[0].startswith("#") else p.stem
        for l in head:
            m = re.match(r"\s*-\s*(\d{4}-\d{2}-\d{2})\s*·\s*(.+)", l)
            if m and start <= datetime.date.fromisoformat(m.group(1)) <= end:
                out.setdefault(title, []).append(m.group(2).strip())
    return out


def _bots(agent):
    """[(bot name, new users this week, last week, paid payments, UZS)] for bots with a `weekly` config."""
    bots = ((agent.cfg.get("bot_stats") or {}).get("bots") or {})
    if not any(b.get("weekly") for b in bots.values()):
        return []
    from linda.business import bot_stats
    svc = bot_stats.service(agent)
    out = []
    for key, b in bots.items():
        w = b.get("weekly") or {}
        if not w.get("users"):
            continue
        u, p, paid = w["users"], w.get("payments"), w.get("paid_filter") or "true"
        for t in (u, p):                               # table names come from config: allow plain identifiers only
            if t and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
                raise ValueError(f"bad table name in bot_stats.{key}.weekly")
        sql = (f"select (select count(*) from {u} where created_at >= now() - interval '7 days') as new_users, "
               f"(select count(*) from {u} where created_at >= now() - interval '14 days' "
               f"and created_at < now() - interval '7 days') as prev_users")
        if p:
            sql += (f", (select count(*) from {p} where {paid} and created_at >= now() - interval '7 days') as payments, "
                    f"(select coalesce(sum(amount), 0) / 100 from {p} where {paid} "
                    f"and created_at >= now() - interval '7 days') as uzs")
        try:
            cols, rows = svc._run(key, sql)
            r = dict(zip(cols, rows[0]))
            out.append((b.get("name") or key, int(r["new_users"]), int(r["prev_users"]),
                        int(r.get("payments") or 0), int(r.get("uzs") or 0)))
        except Exception as e:
            print(f"[proactive bots {key}]", repr(e)[:120])
    return out


def _money(agent, start, end):
    con = _db(agent)
    try:
        rows = con.execute("select sheet, type, sum(amount) from finance where status='active' and tx_date between ? "
                           "and ? group by sheet, type", (str(start), str(end))).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return rows


def _n(x):
    return f"{int(x):,}".replace(",", " ")


def weekly_review(agent, today=None):
    """-> (title, sections, question)."""
    today = today or datetime.date.today()
    start, end = _week(today)
    sections = []
    done = _done_tasks(agent, start, end)
    sections.append((f"✅ Done ({len(done)})", [f"• {_esc(t)}" for t in done[:12]] or ["<i>No tasks closed this week.</i>"]))
    prog = _progress(agent, start, end)
    if prog:
        lines = []
        for proj, items in prog.items():
            lines.append(f"<b>{_esc(proj)}</b> ({len(items)})")
            lines += [f"• {_esc(x[:140])}" for x in items[:3]]
        sections.append(("🛠 Progress", lines))
    try:
        bots = _bots(agent)
    except Exception as e:
        print("[proactive bots]", repr(e)[:120])
        bots = []
    if bots:
        lines = []
        for name, new, prev, pay, uzs in bots:
            trend = "" if not prev else f" ({'+' if new >= prev else ''}{round((new - prev) * 100 / prev)}% vs last week)"
            lines.append(f"<b>{_esc(name)}</b>: {_n(new)} new users{trend}" + (f" · {pay} payments, {_n(uzs)} UZS" if pay else ""))
        sections.append(("🤖 Bots", lines))
    if _cfg(agent)["review_finance"]:
        money = _money(agent, start, end)
        if money:
            sections.append(("💰 Money", [f"{_esc(s)} · {t}: {_n(v)}" for s, t, v in money]))
    nxt = []
    for r in agent.store.open_tasks():
        try:
            due = datetime.date.fromisoformat(str(r[2])[:10]) if r[2] else None
        except ValueError:
            due = None
        if due and today < due <= today + datetime.timedelta(days=7):
            nxt.append((due, f"• {_esc(r[1])} <i>({due:%a %d %b})</i>"))
    sections.append(("📅 Next 7 days", [l for _, l in sorted(nxt)] or ["<i>No deadlines.</i>"]))
    question = "What matters most next week? Reply with your plans and I'll add them as tasks."
    return f"Weekly review · {start:%d %b} – {end:%d %b}", sections, question


# ---------------------------------------------------------------- sending (scheduler + commands)
def _tg(agent):
    tg = getattr(agent, "tg", None)
    if tg is None:
        print("[proactive] Telegram is not configured")
    return tg


def send_reminders(agent, tg=None, quiet=True):
    tg = tg or _tg(agent)
    card = reminders(agent)
    if tg is None:
        return True
    if card:
        tg.send_styled(card[0], card[1], icon="⏰")
    elif not quiet:
        tg.send_styled("Reminders", icon="⏰", intro="Nothing due soon and nothing overdue.")
    return True


def send_followups(agent, tg=None, quiet=True):
    tg = tg or _tg(agent)
    card = followups(agent)
    if tg is None:
        return True
    if card:
        tg.send_styled(card[0], card[1], icon="👥")
    elif not quiet:
        tg.send_styled("Clients", icon="👥", intro="No client has been waiting too long.")
    return True


def send_weekly_review(agent, tg=None):
    tg = tg or _tg(agent)
    if tg is None:
        return True
    title, sections, question = weekly_review(agent)
    tg.send_styled(title, sections, icon="🗓", footer=question)
    agent.store.set(WEEK_KEY, str(time.time() + 12 * 3600))     # the bot process reads this (shared database)
    return True


def week_window(agent):
    """True while a reply should be read as next week's plans (12 h after the review, one reply)."""
    try:
        return time.time() < float(agent.store.get(WEEK_KEY) or 0)
    except (TypeError, ValueError):
        return False


def close_week_window(agent):
    agent.store.set(WEEK_KEY, "0")
