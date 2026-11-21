"""Questions about the user's Telegram bots (Simple Quizzer, Simple Slides, ...) answered from their Supabase Postgres.

HOW      the model writes ONE read-only SELECT from the bot's real schema -> code checks it -> it runs in a READ ONLY
         transaction with a time limit -> the model words the answer from the rows.
SAFETY   - connect with a read-only Postgres role (see config: bot_stats.db_env); every query also runs read-only
         - personal columns (names, usernames, phones, emails, tokens, ...) are not shown to the model and any SQL that
           names them is refused, so answers are counts, sums and trends, never one person's data
         - one statement, SELECT/WITH only, 8 s timeout, at most 200 rows
CONFIG   config.yaml:
           bot_stats:
             db_env: SUPABASE_STATS_DB_URL      # name of the .env variable holding the read-only connection string
             timezone: Asia/Tashkent
             bots:
               quizzer: {name: "Simple Quizzer", tables: [simplequizzer_users, ...], hint: "..."}
"""
import datetime
import decimal
import json
import os
import re
import time

HIDDEN = re.compile(r"(^|_)(phone|email|e_mail|username|user_name|first_name|last_name|full_name|fullname|name|"
                    r"token|password|passwd|secret|api_key|key|card|pan|address|passport|photo|avatar|bio|"
                    r"ip|ip_address|hash|otp)($|_)", re.I)
ALLOWED_NAME_COLS = re.compile(r"^(file_name|filename|table_name|plan_name|package_name|bot_name|quiz_name|"
                               r"group_name|title|topic|subject|theme|template_name|status_code)$", re.I)
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|copy|call|do|execute|"
                       r"vacuum|analyze|refresh|lock|set|reset|listen|notify|pg_sleep|pg_read|dblink|lo_\w+)\b", re.I)
MAX_ROWS = 200

SQL_RULES = """You write ONE read-only PostgreSQL query that answers a question about the user's Telegram bot(s).
Now in {tz}: {now}. "today", "yesterday", "last N days", "this/last month", "last year" are calendar periods in {tz}:
today = (now() AT TIME ZONE '{tz}')::date (never CURRENT_DATE: the server runs in UTC); compare
(column AT TIME ZONE '{tz}')::date for timestamptz columns; a "timestamp without time zone" column holds UTC:
(column AT TIME ZONE 'UTC' AT TIME ZONE '{tz}')::date.
TABLES (only these, only these columns):
{schema}
{hints}
Rules:
- answer with AGGREGATES (count, count(distinct ...), sum, avg, grouped by day/month/plan/...). Never list
  individual users; no user ids in the output.
- "new users" = rows created in the period (the users table's created/joined column); "active users" = distinct users
  with activity in the period (activity/last-active columns, attempts, files, edits). Say which definition you used.
- periods: work with local DATES and integers, never date - interval. Pattern (last 3 days, the 3 days before, total):
  WITH d AS (SELECT (now() AT TIME ZONE '{tz}')::date AS today)
  SELECT count(*) FILTER (WHERE (u.created_at AT TIME ZONE '{tz}')::date BETWEEN d.today - 2 AND d.today) AS last_3_days,
         count(*) FILTER (WHERE (u.created_at AT TIME ZONE '{tz}')::date BETWEEN d.today - 5 AND d.today - 3) AS previous_3_days,
         count(*) AS total_users
  FROM simplequizzer_users u, d
  Months: date_trunc('month', d.today)::date; last month = BETWEEN (date_trunc('month', d.today) - interval '1 month')::date
  AND date_trunc('month', d.today)::date - 1.
- a period of up to 31 days: return ONE ROW PER DAY (every day, also days with 0) plus the totals as extra columns:
  WITH d AS (SELECT (now() AT TIME ZONE '{tz}')::date AS today),
       days AS (SELECT generate_series(d.today - 6, d.today, interval '1 day')::date AS day FROM d)
  SELECT days.day, count(u.created_at) AS new_users, sum(count(u.created_at)) OVER () AS period_total,
         (SELECT count(*) FROM simplequizzer_users) AS total_users
  FROM days LEFT JOIN simplequizzer_users u ON (u.created_at AT TIME ZONE '{tz}')::date = days.day
  GROUP BY days.day ORDER BY days.day
- give context in the SAME query as extra columns: the all-time total next to a period number (e.g. new users in the
  period AND total users), and the previous period of the same length when the user asks about a period.
- one statement, SELECT or WITH ... SELECT, no semicolon, LIMIT 200.
Return ONLY JSON: {{"sql": "SELECT ...", "meaning": "one line: what the query counts"}}
or {{"sql": null, "reply": "short answer"}} if the question cannot be answered from these tables."""

ANSWER = """You are Linda, answering in a Telegram chat about the user's own bot. ROWS came from its database, MEANING
says what was counted. Use ONLY the numbers in ROWS, exactly as given; do not compute new numbers.
Write it like a short report in the language of the question: start with the bot name and the main answer with the
dates of the period; if ROWS has one row per day, list every day on its own line; then the overall total / previous
period if present. "Overall" ALWAYS means the all-time total of the bot (e.g. all users ever), never the period total
again. Never write the same number twice. Examples:
"Simple Slides: 1 345 new users in the last 7 days (15–21 Nov).
15 Nov — 210
16 Nov — 188
...
21 Nov — 67
Overall: 46 556 users."
"Simple Quizzer: 56 982 users in total."
Plain text, no SQL, no column names, numbers with spaces as thousands separators, at most 40 lines.
If ROWS is empty, say there was nothing in that period."""


class StatsError(Exception):
    pass


MONTHS = [("jan", "yanv", "январ"), ("feb", "fevr", "феврал"), ("mar", "mart", "март"), ("apr", "aprel", "апрел"),
          ("may", "may", "ма"), ("jun", "iyun", "июн"), ("jul", "iyul", "июл"), ("aug", "avgust", "август"),
          ("sep", "sentabr", "сентябр"), ("oct", "oktabr", "октябр"), ("nov", "noyabr", "ноябр"),
          ("dec", "dekabr", "декабр")]


def period_of(text, today):
    """The calendar period a question means, worked out in code: -> (first day, last day, label) or None.
    The model gets these exact dates, so it cannot pick the wrong month or year."""
    t = text.lower()
    d = datetime.timedelta
    m = re.search(r"(?:last|past|oxirgi|so'?nggi|последни\w*)\s+(\d{1,3})\s*(?:days?|kun|дн|день)", t)
    if m:
        n = int(m.group(1))
        return today - d(days=n - 1), today, f"last {n} days"
    if re.search(r"\btoday\b|bugun|сегодня", t):
        return today, today, "today"
    if re.search(r"yesterday|kecha|вчера", t):
        return today - d(days=1), today - d(days=1), "yesterday"
    if re.search(r"(last|previous|o'?tgan|прошл\w*)\s+(week|hafta|недел)", t):
        start = today - d(days=today.weekday() + 7)
        return start, start + d(days=6), "last week"
    if re.search(r"this week|bu hafta|эт\w+ недел", t):
        return today - d(days=today.weekday()), today, "this week"
    first = today.replace(day=1)
    if re.search(r"(last|previous|o'?tgan|прошл\w*)\s+(month|oy|месяц)", t):
        end = first - d(days=1)
        return end.replace(day=1), end, "last month"
    if re.search(r"this month|bu oy|эт\w+ месяц", t):
        return first, today, "this month"
    for i, names in enumerate(MONTHS, 1):
        if any(re.search(rf"\b{n}\w*", t) for n in names if not (n == "ma" and "март" in t)):
            if names[0] == "may" and not re.search(r"\b(in|of|for|during|since)\s+may\b|\bmay\s+(20\d\d|month|oy\w*)\b|"
                                                   r"\bмая?\b|\bмае\b", t):
                continue
            y = int(re.search(r"\b(20\d\d)\b", t).group(1)) if re.search(r"\b(20\d\d)\b", t) else \
                (today.year if i <= today.month else today.year - 1)
            start = datetime.date(y, i, 1)
            end = (datetime.date(y + (i == 12), i % 12 + 1, 1) - d(days=1))
            return start, min(end, today) if (y, i) == (today.year, today.month) else end, start.strftime("%B %Y")
    if re.search(r"(last|previous|o'?tgan|прошл\w*)\s+(year|yil|год)", t):
        return datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31), str(today.year - 1)
    if re.search(r"this year|bu yil|эт\w+ год", t):
        return datetime.date(today.year, 1, 1), today, f"{today.year} so far"
    m = re.search(r"\b(20\d\d)\b", t)
    if m:
        y = int(m.group(1))
        return datetime.date(y, 1, 1), min(datetime.date(y, 12, 31), today), str(y)
    return None


class BotStats:
    def __init__(self, cfg, llm):
        c = cfg.get("bot_stats") or {}
        self.tz = c.get("timezone", "Asia/Tashkent")
        self.bots = c.get("bots") or {}
        self.db_env = c.get("db_env", "SUPABASE_STATS_DB_URL")
        self.llm = llm
        self._schema = {}                   # bot key -> (text, visible columns per table, hidden column names)
        self._schema_ts = {}

    # ---------------- connection ----------------
    def _url(self, bot):
        env = (self.bots.get(bot) or {}).get("db_env") or self.db_env
        url = os.getenv(env, "")
        if not url:
            raise StatsError(f"No database connection for the bot stats: put the read-only connection string in .env "
                             f"as {env}. Setup: README, section 'Bot stats'.")
        return url

    def _connect(self, bot):
        import psycopg
        try:
            con = psycopg.connect(self._url(bot), connect_timeout=8, autocommit=False)
        except Exception as e:
            raise StatsError(f"Couldn't connect to the bot database: {type(e).__name__}. Check {self.db_env} in .env.") from e
        return con

    # ---------------- schema ----------------
    def schema(self, bot):
        if bot in self._schema and time.time() - self._schema_ts[bot] < 3600:
            return self._schema[bot]
        tables = (self.bots.get(bot) or {}).get("tables") or []
        con = self._connect(bot)
        try:
            with con.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("select table_name, column_name, data_type from information_schema.columns "
                            "where table_schema = 'public' and (%s::text[] is null or table_name = any(%s::text[])) "
                            "order by table_name, ordinal_position", (tables or None, tables or None))
                rows = cur.fetchall()
        finally:
            con.rollback()
            con.close()
        visible, hidden = {}, set()
        for t, col, typ in rows:
            if HIDDEN.search(col) and not ALLOWED_NAME_COLS.match(col):
                hidden.add(col.lower())
                continue
            visible.setdefault(t, []).append(f"{col} {typ}")
        if not visible:
            raise StatsError("The read-only user can't see any of the bot tables. Grant it SELECT on them.")
        text = "\n".join(f"  {t}({', '.join(cols)})" for t, cols in visible.items())
        self._schema[bot], self._schema_ts[bot] = (text, visible, hidden), time.time()
        return self._schema[bot]

    # ---------------- checking and running ----------------
    @staticmethod
    def check(sql, hidden, allowed_tables):
        sql = (sql or "").strip().rstrip(";").strip()
        bare = re.sub(r"'(?:[^']|'')*'", "''", sql)                    # ignore words inside string literals
        if not re.match(r"(?is)^(select|with)\b", bare) or ";" in bare:
            raise StatsError("only one SELECT is allowed")
        bad = FORBIDDEN.search(bare)
        if bad:
            raise StatsError(f"'{bad.group(0)}' is not allowed in a read-only question")
        words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", bare)}
        if words & hidden:
            raise StatsError(f"personal columns are not available: {', '.join(sorted(words & hidden))}")
        ctes = {n.lower() for n in re.findall(r"(?i)\b([A-Za-z_]\w*)\s+as\s*\(", bare)}       # WITH x AS (...)
        for t, call in re.findall(r"(?i)\b(?:from|join)\s+([A-Za-z_][\w.]*)\s*(\()?", bare):
            name = t.split(".")[-1].lower()
            if call or name in ctes:                     # generate_series(...), a CTE
                continue
            if (t.lower().startswith(("pg_", "information_schema")) or "." in t and not t.lower().startswith("public.")
                    or allowed_tables and name not in allowed_tables):
                raise StatsError(f"table '{t}' is not one of this bot's tables")
        return sql

    def _run(self, bot, sql):
        con = self._connect(bot)
        try:
            with con.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL statement_timeout = '8s'")
                cur.execute(f"select * from ({sql}) as q limit {MAX_ROWS}")
                cols = [d.name for d in cur.description or []]
                return cols, cur.fetchall()
        finally:
            con.rollback()
            con.close()

    @staticmethod
    def _clean(cols, rows):
        """JSON-friendly rows; id-looking columns with many different values are masked (no per-user output)."""
        def val(v):
            if isinstance(v, (datetime.date, datetime.datetime)):
                return v.isoformat()
            if isinstance(v, (float, decimal.Decimal)):        # Decimal from sum()/avg(), float
                return int(v) if float(v).is_integer() else round(float(v), 2)
            return v
        out = [{c: val(v) for c, v in zip(cols, r)} for r in rows]
        for c in cols:
            if re.search(r"(^|_)(id|user_id|chat_id|telegram_id|tg_id)$", c, re.I) and len({r[c] for r in out}) > 1:
                for r in out:
                    r[c] = "•"
        return out

    # ---------------- formatting in code (exact, no AI call) ----------------
    @staticmethod
    def _num(col, v):
        if isinstance(v, bool) or v is None:
            return str(v if v is not None else 0)
        if isinstance(v, (int, float)):
            n = round(v) if float(v).is_integer() or abs(v) >= 100 else round(v, 2)
            txt = f"{n:,}".replace(",", " ")
            return txt + (" UZS" if re.search(r"uzs|sum\b|amount|revenue|so'?m", col, re.I) else "")
        return str(v)

    @staticmethod
    def _label(col):
        words = re.sub(r"_+", " ", col).strip()
        if re.search(r"\bperiod\b", words, re.I) and not re.search(r"previous|prev", words, re.I):
            words = "in this period" + (" (UZS)" if re.search(r"uzs|amount|sum", words, re.I) else "")
        words = re.sub(r"\buzs\b", "", words, flags=re.I).strip() or col
        return words[:1].upper() + words[1:]

    def format(self, name, per, cols, data):
        """One summary row, or one row per day: written in code. Anything else -> None (the AI words it)."""
        if not data:
            return f"{name}: nothing in that period." if per else f"{name}: nothing found."
        head = f"{name}" + (f" · {per[2]} ({per[0]:%d %b}–{per[1]:%d %b})" if per and per[0] != per[1]
                            else f" · {per[2]}" if per else "")
        if len(data) == 1:
            return "\n".join([head] + [f"{self._label(c)}: {self._num(c, v)}" for c, v in data[0].items()])
        first = cols[0]
        if len(data) <= 40 and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", str(r[first])) for r in data):
            same = [c for c in cols[1:] if len({str(r[c]) for r in data}) == 1]          # totals repeated on every row
            vary = [c for c in cols[1:] if c not in same]
            lines = [head] + [f"{self._label(c)}: {self._num(c, data[0][c])}" for c in same]
            for r in data:
                d = datetime.date.fromisoformat(str(r[first])[:10])
                lines.append(f"{d:%d %b} — " + ", ".join(self._num(c, r[c]) for c in vary))
            return "\n".join(lines)
        return None

    # ---------------- the question ----------------
    def ask(self, question, bot=""):
        keys = [bot] if bot in self.bots else ([k for k in self.bots if re.search(k, question, re.I)
                                                or re.search(re.escape(self.bots[k].get("name", k).split()[-1]), question, re.I)]
                                               or list(self.bots))
        if not self.bots:
            raise StatsError("No bots configured: add a bot_stats section to config.yaml.")
        answers = []
        for k in keys:
            answers.append(self._ask_one(question, k))
        return answers

    def _ask_one(self, question, bot):
        b = self.bots[bot]
        text, visible, hidden = self.schema(bot)
        now = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%A %Y-%m-%d %H:%M")
        system = SQL_RULES.format(tz=self.tz, now=now, schema=text,
                                  hints=f"About this bot ({b.get('name', bot)}): {b['hint']}" if b.get("hint") else "")
        err, sql, meaning, rows, cols = "", "", "", None, []
        today = datetime.datetime.now().date()
        per = period_of(question.split("\nChange:")[-1], today) or period_of(question, today)
        fixed = (f"\nPERIOD (already worked out, use EXACTLY these local dates): {per[0]} to {per[1]} ({per[2]}). "
                 f"Filter with (local date) BETWEEN '{per[0]}' AND '{per[1]}'." if per else "")
        own = (f"{question}{fixed}\n\n[Answer ONLY for {b.get('name', bot)}, from its own tables above. Each bot has its own "
               f"tables; other bots are answered separately, so never say you can't tell the bots apart.]")
        for _ in range(3):
            raw = self.llm(system, own if not err else f"{own}\n\nYour previous query failed: {err}\nFix it.")
            m = re.search(r"\{.*\}", raw or "", re.S)
            try:
                obj = json.loads(m.group(0)) if m else None
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                err = "the reply was not JSON"
                continue
            if not obj.get("sql"):
                return {"bot": b.get("name", bot), "answer": str(obj.get("reply") or "That isn't in the bot's data."),
                        "sql": ""}
            try:
                sql = self.check(obj["sql"], hidden, {t.lower() for t in visible})
                meaning = str(obj.get("meaning") or "")
                print(f"[bot stats] {bot}: {sql[:300]}")
                cols, rows = self._run(bot, sql)
                break
            except StatsError as e:
                err = str(e)
            except Exception as e:                                  # SQL error from Postgres: let the model fix it
                err = str(e).splitlines()[0][:250]
        if rows is None:
            raise StatsError(f"I couldn't build a working query for {b.get('name', bot)} ({err}). Try rephrasing.")
        data = self._clean(cols, rows)
        fixed_text = self.format(b.get("name", bot), per, cols, data)
        if fixed_text:
            return {"bot": b.get("name", bot), "answer": fixed_text, "sql": sql}
        ans = self.llm(ANSWER, f"BOT: {b.get('name', bot)} (use exactly this name)"
                       + (f"\nPERIOD: {per[0]} to {per[1]} ({per[2]})" if per else "") + f"\nQUESTION: {question}\nMEANING: {meaning}\nROWS: {json.dumps(data[:60], ensure_ascii=False, default=str)}")
        ans = re.sub(r"\*\*|`", "", ans or "").strip() or "\n".join(" · ".join(str(v) for v in r.values()) for r in data[:20])
        return {"bot": b.get("name", bot), "answer": ans, "sql": sql}


# ============================ routing (no AI needed to decide) =========================
ALIASES = {"quizzer": r"quizzer|simple ?quiz|quiz bot|kvizer|квиз", "slides": r"slides?|slayd|presentation|prezentats|"
           r"referat|презентац|слайд"}
STATS_Q = re.compile(r"\busers?\b|foydalanuvchi|пользоват|юзер|subscriber|\bnew\b|active|joined|signed up|payments?|"
                     r"to'?lov|оплат|premium|quizzes|presentations|files|docs?\b|how many|how much|qancha|nechta|"
                     r"сколько|statisti|\bstats?\b|growth|o'?sish|рост", re.I)
EXPLAIN = re.compile(r"what is (it|this)|nima (u|bu)|что это|tell me about|haqida", re.I)
FOLLOW_UP = re.compile(r"\b(this|that|it|there|same|bu|shu|unda|этом|там|тоже)\b|^\s*(and|va|а|и)\b", re.I)
PERIOD = re.compile(r"today|yesterday|week|month|year|days?\b|bugun|kecha|hafta|oy\b|yil|kun|сегодня|вчера|недел|"
                    r"месяц|год|дн", re.I)


REFINE = re.compile(r"^\s*(and |va |а |и )?(only |faqat |только )?((for|in|uchun|за|в) )?"
                    r"(today|yesterday|this|last|previous|january|february|march|april|may|june|july|august|september|"
                    r"october|november|december|yanvar|fevral|mart|aprel|iyun|iyul|avgust|sentabr|oktabr|noyabr|"
                    r"dekabr|январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр|week|month|year|"
                    r"\d{4}|\d{1,2}\s?days?)", re.I)


def _bot_named(agent, text):
    """'quizzer' / 'slides' / 'both' / None. 'both bots', 'my bots', 'botlar' mean both."""
    bots = (agent.cfg.get("bot_stats") or {}).get("bots") or {}
    hits = [k for k in bots if re.search(ALIASES.get(k, re.escape(k)), text, re.I)]
    if len(hits) > 1 or re.search(r"\bboth bots|\bbots\b|\bikkala bot|\bbotlar|оба бота|\bботах|\bботов", text, re.I):
        return "both"
    return hits[0] if hits else None


def wants_stats(agent, text):
    """-> 'quizzer' / 'slides' / 'both' / '' (both bots) when the message is a numbers question about the bots, else None."""
    t = text.strip()
    if t.startswith("/") or len(t) > 300 or not (agent.cfg.get("bot_stats") or {}).get("bots"):
        return None
    bot = _bot_named(agent, t)
    if bot and bot != "both":
        agent._last_bot = (bot, time.time())              # remembered even for 'Simple Slides, what is it?'
    if not STATS_Q.search(t) or EXPLAIN.search(t):
        return None
    if bot:
        return "" if bot == "both" else bot
    last = getattr(agent, "_last_bot", None)
    if last and time.time() - last[1] < 1800 and FOLLOW_UP.search(t):
        return last[0]                                     # 'how many users do we have on this?'
    if re.search(r"\busers?\b|foydalanuvchi|пользоват|юзер|subscriber", t, re.I) and PERIOD.search(t):
        return ""                                          # 'new users last week' with no bot: both bots
    return None


def _refinement(agent, text):
    """'November', 'and last month?', 'only Slides' right after a stats answer: the same question, changed."""
    last = getattr(agent, "_last_stats", None)
    t = text.strip()
    if not last or time.time() - last[2] > 600 or len(t.split()) > 6 or STATS_Q.search(t):
        return None
    bot = _bot_named(agent, t)
    if not (REFINE.search(t) or bot):
        return None
    return (f"{last[0]}\nChange: {t}", "" if bot == "both" else (bot or last[1]))


def telegram_text(agent, tg, text):
    ref = _refinement(agent, text)
    if ref:
        question, bot = ref
    else:
        bot = wants_stats(agent, text)
        if bot is None:
            return False
        question = text
    print(f"[route] bot stats -> {bot or 'both bots'}{' (refining the last question)' if ref else ''}")
    agent._last_stats = (question, bot, time.time())
    from .clients import _run
    _run(agent, tg, bot_stats, {"question": question, "bot": bot}, question)
    return True


# ============================ Linda tool ============================================
_SVC = {}


def service(agent):
    if "svc" not in _SVC:
        from .briefing import llm_text
        _SVC["svc"] = BotStats(agent.cfg, llm=lambda s, u: llm_text(agent, s, u, temp=0))
    return _SVC["svc"]


def bot_stats(agent, a, text):
    from .telegram import esc
    q = str(a.get("question") or text)
    try:
        answers = service(agent).ask(q, str(a.get("bot") or "").lower())
    except StatsError as e:
        return agent.say(f"error: {e}")
    brain = getattr(agent, "brain", None)
    tg = getattr(agent, "tg", None) if (getattr(agent, "in_telegram", False) or getattr(brain, "extra_rules", "")) else None
    if tg:
        for r in answers:
            tg.send_styled(r["bot"], [("", [esc(r["answer"])])], icon="📊")
        agent.card_sent = True
        return agent.say("The answer was sent as a card. Do not write anything else.")
    agent.say(" ".join(f"{r['bot']}: {r['answer']}" for r in answers))
