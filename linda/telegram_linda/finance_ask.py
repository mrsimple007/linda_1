"""Read-only natural-language questions over linda.db. LLM writes SQL, code validates and runs it, LLM words the answer."""
from __future__ import annotations
import datetime as dt, json, re, sqlite3, time
from .finance import FinanceError, fmt_amount

DOMAINS = {
  "finance": {"hint": "amounts are whole UZS; type income/expense; sheet my_finance=personal, company=business",
    "views": {"q_finance": "CREATE VIEW q_finance AS SELECT tx_date, sheet, type, amount, description "
                           "FROM finance WHERE status='active'"}},
  "tasks": {"hint": "status is 'open' or 'done'; done_date is when it was finished",
    "views": {"q_tasks": "CREATE VIEW q_tasks AS SELECT id, title, status, due, project, "
                         "date(done_ts,'unixepoch','localtime') AS done_date FROM tasks"}},
  "practice": {"hint": "one row per practice session; day is the date",
    "views": {"q_practice": "CREATE VIEW q_practice AS SELECT p.day, p.instrument, p.minutes, p.focus, p.rating, "
                            "s.title AS song, s.artist FROM practice_logs p LEFT JOIN songs s ON s.id = p.song_id",
              "q_songs": "CREATE VIEW q_songs AS SELECT title, artist, instrument, status, priority FROM songs"}},
}

SQL_RULES = """You turn a question into ONE read-only SQLite query. Today is {today}.
Use ONLY these views:
{schema}
Hints: {hint}
Rules: one SELECT (or WITH ... SELECT), no semicolon, at most LIMIT 50. Dates are 'YYYY-MM-DD' text:
filter months with col >= 'YYYY-MM-01' AND col < 'first day of next month'. Match text with
lower(col) LIKE '%word%' and try several likely words (Uzbek, Russian, English are mixed).
Return ONLY JSON: {{"sql": "SELECT ..."}} or {{"sql": null, "reply": "two short sentences"}} if the question
is not about this data."""

ANSWER = """You are Linda, answering in a Telegram chat. Today is {today}. You get the QUESTION and the ROWS
the database returned. Use ONLY the values in ROWS exactly as written, no arithmetic, never invent entries.
Answer in the language of the question, 1-6 short lines, plain text. If ROWS is empty, say nothing is stored for that."""

_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
_REC = getattr(sqlite3, "SQLITE_RECURSIVE", 33)
_PLAIN = {"id", "n", "count", "cnt", "minutes", "score", "rating", "year"}   # numbers that are not money


def _json(t):
    m = re.search(r"\{.*\}", t or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


class DataAsk:
    MAX_ROWS = 50

    def __init__(self, db_path, llm, domains, money_domains=("finance",), llm_answer=True):
        """domains = {"tasks": {"views": {"q_tasks": "CREATE VIEW IF NOT EXISTS q_tasks AS SELECT ..."}, "hint": "..."}}"""
        self.db_path, self.llm, self.domains = str(db_path), llm, domains
        self.money, self.llm_answer = set(money_domains), llm_answer
        con = sqlite3.connect(self.db_path, timeout=10)
        try:
            for name, d in domains.items():
                for view, ddl in d["views"].items():
                    try:
                        con.execute(f"DROP VIEW IF EXISTS {view}")     # so edits to the definition apply
                        con.execute(ddl)
                    except sqlite3.Error as e:
                        print(f"[data ask] view {view} skipped:", e)
            con.commit()
        finally:
            con.close()

    def schema(self, domain):
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            out = []
            for v in self.domains[domain]["views"]:
                cols = [f"{r[1]} {r[2]}" for r in con.execute(f"PRAGMA table_info({v})")]
                if cols:
                    out.append(f"  {v}({', '.join(cols)})")
            return "\n".join(out)
        finally:
            con.close()

    def _run(self, sql, domain):
        sql = (sql or "").strip().rstrip(";").strip()
        if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql:
            raise ValueError("only one SELECT is allowed")
        allowed = set(self.domains[domain]["views"])
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        deadline = time.time() + 3

        def auth(action, a1, a2, db, src):
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, _REC):
                return _OK
            if action == sqlite3.SQLITE_READ and (a1 in allowed or src in allowed):
                return _OK
            return _DENY

        con.set_authorizer(auth)
        con.set_progress_handler(lambda: time.time() > deadline, 10000)
        try:
            cur = con.execute(sql)
            return [d[0] for d in (cur.description or [])], cur.fetchmany(self.MAX_ROWS)
        finally:
            con.close()

    def _pretty(self, cols, rows, domain):
        money = domain in self.money
        def f(c, v):
            if money and isinstance(v, (int, float)) and not isinstance(v, bool) and c.lower() not in _PLAIN:
                return fmt_amount(int(round(v)))
            return v
        return [{c: f(c, v) for c, v in zip(cols, r)} for r in rows]

    def ask(self, question, domain):
        if domain not in self.domains:
            raise FinanceError(f"I don't know the data area '{domain}'. Known: {', '.join(self.domains)}.")
        today = dt.date.today().strftime("%A %Y-%m-%d")
        system = SQL_RULES.format(today=today, schema=self.schema(domain), hint=self.domains[domain].get("hint", ""))
        err, rows, cols = "", None, []
        for _ in range(2):
            raw = self.llm(system, question if not err else f"{question}\n\nYour previous query failed: {err}\nFix it.")
            if not raw:
                raise FinanceError("The AI services are busy right now. Try again in a minute.")
            obj = _json(raw)
            if not isinstance(obj, dict):
                err = "reply was not JSON"
                continue
            if not obj.get("sql"):
                return str(obj.get("reply") or "I can't answer that from the stored data.")
            print(f"[data ask] {domain}:", str(obj["sql"])[:300])
            try:
                cols, rows = self._run(obj["sql"], domain)
                break
            except Exception as e:
                err = str(e)[:200]
        if rows is None:
            raise FinanceError("I couldn't turn that into a query. Try rephrasing it.")
        pretty = self._pretty(cols, rows, domain)
        if self.llm_answer:
            ans = re.sub(r"\*\*|`", "", self.llm(ANSWER.format(today=today),
                  f"QUESTION: {question}\nROWS: {json.dumps(pretty, ensure_ascii=False)}") or "").strip()
            if ans:
                return ans
        return "\n".join(" · ".join(str(v) for v in r.values()) for r in pretty[:30]) or "Nothing stored for that."