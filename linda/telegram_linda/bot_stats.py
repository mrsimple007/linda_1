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
(column AT TIME ZONE '{tz}')::date for timestamptz columns.
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
- give context in the SAME query as extra columns: the all-time total next to a period number (e.g. new users in the
  period AND total users), and the previous period of the same length when the user asks about a period.
- one statement, SELECT or WITH ... SELECT, no semicolon, LIMIT 200.
Return ONLY JSON: {{"sql": "SELECT ...", "meaning": "one line: what the query counts"}}
or {{"sql": null, "reply": "short answer"}} if the question cannot be answered from these tables."""

ANSWER = """You are Linda, answering in a Telegram chat about the user's own bot. ROWS came from its database, MEANING
says what was counted. Use ONLY the numbers in ROWS, exactly as given; do not compute new numbers.
Write it like a short report in the language of the question: start with the bot name and the main answer, then one
line per extra number (overall total, previous period), with the dates of the period. Example:
"Simple Quizzer: 312 new users in the last 3 days (19–21 Nov).
Overall: 48 120 users.
Previous 3 days: 287."
Plain text, no SQL, no column names, numbers with spaces as thousands separators, at most 8 lines.
If ROWS is empty, say there was nothing in that period."""


class StatsError(Exception):
    pass


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
            if hasattr(v, "is_integer"):                    # Decimal / float
                return int(v) if float(v).is_integer() else round(float(v), 2)
            return v
        out = [{c: val(v) for c, v in zip(cols, r)} for r in rows]
        for c in cols:
            if re.search(r"(^|_)(id|user_id|chat_id|telegram_id|tg_id)$", c, re.I) and len({r[c] for r in out}) > 1:
                for r in out:
                    r[c] = "•"
        return out

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
        for _ in range(3):
            raw = self.llm(system, question if not err else f"{question}\n\nYour previous query failed: {err}\nFix it.")
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
        ans = self.llm(ANSWER, f"BOT: {b.get('name', bot)} (use exactly this name)\nQUESTION: {question}\nMEANING: {meaning}\nROWS: {json.dumps(data[:60], ensure_ascii=False, default=str)}")
        ans = re.sub(r"\*\*|`", "", ans or "").strip() or "\n".join(" · ".join(str(v) for v in r.values()) for r in data[:20])
        return {"bot": b.get("name", bot), "answer": ans, "sql": sql}


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
