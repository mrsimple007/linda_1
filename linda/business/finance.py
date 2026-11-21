"""Finance: text/voice -> Google Sheets (my_finance + company) and linda.db.

Numbers come from Python and SQL, never from an LLM.

  parse_message()   "-40 000 medicine"  ->  Tx
  FlatSheet         my_finance: one row per category per month. A new amount is ADDED to the matching
                    row of that month (exact match in code, else an LLM picks by number); no match = new row
  BlockSheet        company: income block on top, expense block below, pre-formatted empty rows;
                    one row per payment, written into the first free row, totals never touched
  FinanceService    add / undo_last / summary / search_total, with the DB log and an audit hook

CLI (no Telegram needed):
  python -m linda.finance parse "c: +670 000 Javohir application fee"
  python -m linda.finance check        # dry run: shows which row each sheet would use next
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]   # project root: business -> linda -> project
EPOCH = dt.date(1899, 12, 30)  # Google Sheets / Excel date serial 0

# Same text as finance_schema.sql (kept there for running by hand). Built in, so no file is needed at runtime.
SCHEMA_SQL = """
-- Finance log for Linda (Google Sheets <-> linda.db).
-- Safe to run repeatedly. Amounts are whole UZS.

CREATE TABLE IF NOT EXISTS finance (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    tx_date     TEXT    NOT NULL,                              -- ISO date YYYY-MM-DD
    sheet       TEXT    NOT NULL CHECK (sheet IN ('my_finance', 'company')),
    type        TEXT    NOT NULL CHECK (type  IN ('income', 'expense')),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    description TEXT    NOT NULL,
    category    TEXT,                                          -- reserved for auto-categorising later
    sheet_row   INTEGER,                                       -- row Linda wrote to (needed for undo)
    source      TEXT    NOT NULL DEFAULT 'telegram',           -- telegram | voice
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'undone', 'failed')),
    confirmed   INTEGER NOT NULL DEFAULT 0,                    -- 1 = user said yes (company sheet)
    error       TEXT,
    prev_amount INTEGER,                                       -- merged rows: amount before Linda added to it
    target_desc TEXT,                                          -- merged rows: the existing row's description (NULL = new row)
    undone_at   TEXT,
    raw_text    TEXT
);

CREATE INDEX IF NOT EXISTS idx_finance_date   ON finance (tx_date);
CREATE INDEX IF NOT EXISTS idx_finance_lookup ON finance (sheet, type, tx_date);
CREATE INDEX IF NOT EXISTS idx_finance_status ON finance (status, id);

-- rows copied in from the sheets' history (see finance_import.sql): one per sheet row, never undone by Linda
CREATE UNIQUE INDEX IF NOT EXISTS ux_finance_import ON finance (sheet, sheet_row) WHERE source = 'import';

-- small key/value store for the Telegram listener (last processed update id)
CREATE TABLE IF NOT EXISTS finance_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE VIEW IF NOT EXISTS finance_monthly AS
SELECT substr(tx_date, 1, 7) AS month,
       sheet,
       SUM(CASE WHEN type = 'income'  THEN amount ELSE 0 END) AS income,
       SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END) AS expense,
       SUM(CASE WHEN type = 'income'  THEN amount ELSE -amount END) AS balance
FROM finance
WHERE status = 'active'
GROUP BY month, sheet;
"""


class FinanceError(Exception):
    """Anything the user should be told about (shown as-is in Telegram)."""


class ParseError(FinanceError):
    pass


def to_serial(d: dt.date) -> int:
    return (d - EPOCH).days


def fmt_amount(n: int) -> str:
    return f"{n:,}".replace(",", " ")


@dataclass
class Tx:
    sheet: str                 # 'my_finance' | 'company'
    type: str                  # 'income' | 'expense'
    amount: int                # whole UZS
    description: str
    date: dt.date
    raw: str = ""
    db_id: Optional[int] = None
    row: Optional[int] = None
    merged: bool = False                 # True = amount was added to an existing row
    prev_amount: Optional[int] = None    # that row's amount before the addition


# --------------------------------------------------------------------------- parsing

_GROUP = r"\d{1,3}(?:[ ,.\u00a0\u202f]\d{3})+"
_PREFIX = re.compile(
    r"^\s*(?:(?P<co>c|co|biz|company|kompaniya|компания)|(?P<my>m|my|personal|shaxsiy|личное))\s*[:,]\s*",
    re.I,
)
_AMOUNT = re.compile(
    r"^\s*(?P<sign>[+\-\u2212\u2013\u2014])\s*"
    rf"(?P<num>{_GROUP}(?!\d)|\d+(?:[.,]\d+)?)"
    r"\s*(?P<mult>(?:mlrd|mln|ming|млн|тыс|k|m)(?![a-zа-яё]))?"
    r"\s*(?P<cur>(?:uzs|sum|so'?m|сум|сўм|usd|eur|\$|€)(?![a-zа-яё]))?"
    r"\s*(?P<rest>.*)$",
    re.I | re.S,
)
# "... finance sheet", "to the company", "(finance)" -> company sheet; "my finance" -> personal sheet
_CO_MARK = re.compile(
    r"(?<!\w)(?:"
    r"(?:in|to|into|на|в)\s+(?:the\s+)?(?:finance|company|kompaniya|компания)(?:\s+(?:sheet|table|list|tab))?"
    r"|(?:finance|company)\s+(?:sheet|table|list|tab)"
    r"|\(\s*(?:finance|company)\s*\)"
    r")(?!\w)", re.I)
_MY_MARK = re.compile(
    r"(?<!\w)(?:(?:in|to|into)\s+)?(?:my[\s_-]?finance(?:\s+(?:sheet|table|list|tab))?"
    r"|personal\s+(?:sheet|table|list|tab))(?!\w)", re.I)
_MULT = {"k": 10**3, "ming": 10**3, "тыс": 10**3, "m": 10**6, "mln": 10**6, "млн": 10**6, "mlrd": 10**9}
_REL = re.compile(r"(?<!\w)(yesterday|вчера|kecha|today|сегодня|bugun)(?!\w)", re.I)
_REL_DAYS = {"yesterday": -1, "вчера": -1, "kecha": -1, "today": 0, "сегодня": 0, "bugun": 0}
_DMY = re.compile(r"(?<![\d.,/])(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?(?!\d)")


def _extract_date(rest: str, today: dt.date) -> tuple[dt.date, str]:
    m = _REL.search(rest)
    if m:
        when = today + dt.timedelta(days=_REL_DAYS[m.group(1).lower()])
        return when, rest[: m.start()] + " " + rest[m.end():]
    m = _DMY.search(rest)
    if m:
        day, month, y = int(m[1]), int(m[2]), m[3]
        year = today.year if not y else (int(y) if len(y) == 4 else 2000 + int(y))
        try:
            when = dt.date(year, month, day)
        except ValueError:
            return today, rest  # not a real date: leave it in the description
        if not y and when > today + dt.timedelta(days=31):
            when = when.replace(year=year - 1)  # "30.12" typed in January means last year
        return when, rest[: m.start()] + " " + rest[m.end():]
    return today, rest


def parse_message(text: str, default_sheet: str = "my_finance", today: Optional[dt.date] = None) -> Tx:
    """'-40 000 medicine', '+9 000 000 salary', 'c: +670 000 fee 08.11', '-40k lunch yesterday'."""
    today = today or dt.date.today()
    body, sheet = text.strip(), default_sheet
    pm = _PREFIX.match(body)
    if pm:
        sheet = "company" if pm.group("co") else "my_finance"
        body = body[pm.end():]

    m = _AMOUNT.match(body)
    if not m:
        raise ParseError("Start with + or − and an amount, e.g. −40 000 medicine (company: c: +670 000 fee)")

    cur = (m.group("cur") or "").lower()
    if cur in ("usd", "eur", "$", "€"):
        raise ParseError("Only UZS amounts are supported. Send the UZS amount and put 500$ in the description.")

    num = m.group("num")
    if re.fullmatch(_GROUP, num):
        value = Decimal(re.sub(r"\D", "", num))
    else:
        value = Decimal(num.replace(",", "."))
    if m.group("mult"):
        value *= _MULT[m.group("mult").lower()]
    amount = int(value.to_integral_value())
    if amount <= 0:
        raise ParseError("The amount must be greater than zero.")

    rest = m.group("rest")
    mine = _MY_MARK.search(rest)          # check "my finance" first: it contains the word "finance"
    if mine:
        rest = rest[: mine.start()] + " " + rest[mine.end():]
    comp = _CO_MARK.search(rest)
    if comp:
        rest = rest[: comp.start()] + " " + rest[comp.end():]
    if mine and comp:
        raise ParseError("You named both sheets. Say only one: 'c:' / 'finance sheet' or 'my finance'.")
    if not pm:                            # an explicit "c:" / "m:" prefix always wins
        sheet = "company" if comp else "my_finance" if mine else default_sheet

    when, rest = _extract_date(rest, today)
    rest = re.sub(r"\s+", " ", rest).strip(" \t-–—:,.;")
    rest = re.sub(r"^(?:for|за|для)\s+", "", rest, flags=re.I)
    rest = re.sub(r"\s+uchun$", "", rest, flags=re.I).strip()
    if not rest:
        raise ParseError("Add a short description, e.g. −40 000 medicine")

    return Tx(
        sheet=sheet,
        type="income" if m.group("sign") == "+" else "expense",
        amount=amount,
        description=rest[0].upper() + rest[1:],
        date=when,
        raw=text,
    )


# --------------------------------------------------------------------------- Google Sheets

def a1(tab: str, rng: str) -> str:
    return "'" + tab.replace("'", "''") + "'!" + rng


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _copy_row(gid: int, src: int, dst: int, c0: int, c1: int) -> list:
    def rng(r):
        return {"sheetId": gid, "startRowIndex": r - 1, "endRowIndex": r,
                "startColumnIndex": c0, "endColumnIndex": c1}
    return [{"copyPaste": {"source": rng(src), "destination": rng(dst), "pasteType": t}}
            for t in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION")]


class SheetsAPI:
    """Thin wrapper over the Sheets v4 API with a service account."""

    def __init__(self, credentials_file: str):
        if not credentials_file:
            raise FinanceError("finance.credentials_file is not set (service account JSON).")
        import warnings
        with warnings.catch_warnings():   # Google's "Python 3.9 is past end of life" notice is noise here
            warnings.simplefilter("ignore", FutureWarning)
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            os.path.expanduser(credentials_file),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.email = getattr(creds, "service_account_email", "the service account")
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    def tab_names(self, sid) -> list:
        try:
            meta = self._svc.get(spreadsheetId=sid, fields="sheets.properties.title").execute(num_retries=2)
            return [s["properties"]["title"] for s in meta.get("sheets", [])]
        except Exception:
            return []

    def _exec(self, req, sid, rng=""):
        """Run a request; turn Google's raw errors into messages that say what to fix."""
        try:
            return req.execute(num_retries=3)
        except FinanceError:
            raise
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            msg = str(e)
            if "Unable to parse range" in msg:
                tab = rng.split("!")[0].strip("'")
                raise FinanceError(f"Tab '{tab}' not found in this spreadsheet. Tabs it has: {self.tab_names(sid)}. "
                                   f"Put the exact name in config.yaml (finance.sheets.*.tab).") from e
            if status == 403:
                raise FinanceError(f"No access to the spreadsheet. Share it with {self.email} as Editor "
                                   f"and make sure the Sheets API is enabled.") from e
            if status == 404:
                raise FinanceError("Spreadsheet not found. Check the id in config.yaml (finance.sheets.*.id).") from e
            raise FinanceError(f"Google Sheets error ({status}): {msg[:200]}") from e

    def get(self, sid, rng, render="UNFORMATTED_VALUE"):
        r = self._exec(self._svc.values().get(spreadsheetId=sid, range=rng, valueRenderOption=render), sid, rng)
        return r.get("values", [])

    def update(self, sid, data):
        # RAW: text stays text (so a description can never turn into a formula or a surprise date),
        # numbers stay numbers, and the cell's own number format decides how it looks.
        self._exec(self._svc.values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": "RAW", "data": data}), sid,
            data[0]["range"] if data else "")

    def clear(self, sid, ranges):
        self._exec(self._svc.values().batchClear(spreadsheetId=sid, body={"ranges": ranges}), sid, ranges[0])

    def batch(self, sid, requests):
        self._exec(self._svc.batchUpdate(spreadsheetId=sid, body={"requests": requests}), sid)

    def tab_info(self, sid, tab):
        meta = self._exec(self._svc.get(
            spreadsheetId=sid, fields="sheets.properties(sheetId,title,gridProperties(rowCount))"), sid)
        for s in meta["sheets"]:
            p = s["properties"]
            if p["title"] == tab:
                return p["sheetId"], p["gridProperties"]["rowCount"]
        raise FinanceError(f"Tab '{tab}' not found in this spreadsheet. Tabs it has: "
                           f"{[s['properties']['title'] for s in meta['sheets']]}. "
                           f"Put the exact name in config.yaml (finance.sheets.*.tab).")


@dataclass
class Slot:
    row: int
    write_label: bool = False        # blocks: the Tur cell is empty, so write the label
    copy_from: Optional[int] = None  # flat: row whose format + dropdown to copy
    merge: bool = False              # flat: ADD to this existing row instead of writing a new one
    prev: Optional[int] = None       # merge: the row's amount before the addition (None = was blank)
    name: str = ""                   # merge: the row's own description; new row: canonical name to use


# ---- matching a message to the category rows of the month ---------------------------------------
_STOP = {"for", "paid", "pay", "overall", "total", "monthly", "month", "buy", "bought", "to", "on", "the",
         "a", "of", "uchun", "oylik", "umumiy", "za", "за", "для", "оплата"}


def _different(a: str, b: str) -> bool:
    """Same kind of thing but different specifics: they share a word and EACH has its own extra word
    ('Aziz to'y' vs 'Nodira xola to'y': same event type, different people). Translations share nothing and pass."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    same = lambda w, v: w == v or (min(len(w), len(v)) >= 3 and (w.startswith(v) or v.startswith(w)))  # to'y ~ to'yona
    shared = any(same(w, v) for w in ta for v in tb)
    extra = lambda x, y: any(len(w) >= 3 and not any(same(w, v) for v in y) for w in x)
    return shared and extra(ta, tb) and extra(tb, ta)


def _tokens(text: str) -> list:
    t = str(text).lower().replace("’", "'").replace("ʻ", "'").replace("ʼ", "'")
    return [w for w in re.sub(r"[^\w']+", " ", t).split() if w not in _STOP]


def _row_date(v) -> Optional[dt.date]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return EPOCH + dt.timedelta(days=int(v))
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(\d{1,2})[./,](\d{1,2})[./,](\d{4})\s*", v)
        if m:
            try:
                return dt.date(int(m[3]), int(m[2]), int(m[1]))
            except ValueError:
                return None
    return None


MATCH_SYSTEM = (
    "You match a NEW ENTRY to rows of the user's monthly personal budget sheet (English, Russian, Uzbek, often mixed). "
    "The sheet keeps ONE row per recurring category per month (for example 'Obed' = lunch, 'Bozorlik' = groceries, "
    "'Medicine overall'), and new spending is ADDED to the existing row. THIS MONTH rows are numbered. "
    "EARLIER lists category names used in previous months that have no row this month yet.\n"
    'Answer with "merge": the number of the THIS MONTH row the entry belongs to (same category, even if worded '
    'differently or in another language), otherwise null. Only if merge is null: "name": copy one EARLIER name '
    "exactly if the entry clearly belongs to it, otherwise null. Be strict: different things must not be merged; "
    "entries about DIFFERENT people or events are different rows ('Aziz to'y' is not 'Nodira xola to'y'); "
    'when unsure use null. Return ONLY JSON like {"merge": 3, "name": null}.'
)


class FlatSheet:
    """my_finance: A=Date, B=Type (dropdown), C=Sum, D=For what. One row per category per month:
    a new amount is added to the month's matching row; otherwise a new row goes below the data."""

    def __init__(self, api, cfg):
        self.api, self.id, self.tab = api, cfg["id"], cfg["tab"]
        self.labels = cfg["labels"]
        self.header_rows = cfg.get("header_rows", 1)
        self.merge = cfg.get("merge", True)
        self._type_of = {str(v).strip().lower(): k for k, v in self.labels.items()}

    # -- reading
    def _last_row(self, vals) -> int:
        last = self.header_rows
        for i, row in enumerate(vals, start=1):
            if any(str(c).strip() for c in row):
                last = i
        return last

    def _entries(self, vals) -> list:
        out = []
        for i, row in enumerate(vals, start=1):
            if i <= self.header_rows:
                continue
            row = list(row) + [""] * 4
            typ, d, desc = self._type_of.get(str(row[1]).strip().lower()), _row_date(row[0]), str(row[3]).strip()
            if typ and d and desc:
                out.append({"row": i, "type": typ, "date": d, "amount": _num(row[2]), "desc": desc})
        return out

    def peek(self, tx_type: str) -> Slot:
        last = self._last_row(self.api.get(self.id, a1(self.tab, "A:D")))
        return Slot(row=last + 1, copy_from=last if last > self.header_rows else None)

    # -- matching
    def _match(self, tx: Tx, entries: list, chooser):
        want = set(_tokens(tx.description))
        if not want:
            return None
        key = (tx.date.year, tx.date.month)
        same = [e for e in entries if e["type"] == tx.type]
        month = [e for e in same if (e["date"].year, e["date"].month) == key]
        exact = [e for e in month if set(_tokens(e["desc"])) == want]
        if len(exact) == 1:
            return ("merge", exact[0])                    # same words: no LLM needed

        in_month = [frozenset(_tokens(e["desc"])) for e in month]
        cutoff = tx.date.replace(day=1) - dt.timedelta(days=100)
        earlier, seen = [], set()
        for e in sorted(same, key=lambda e: e["date"], reverse=True):
            k = frozenset(_tokens(e["desc"]))
            if (e["date"].year, e["date"].month) >= key or e["date"] < cutoff or not k or k in seen or k in in_month:
                continue
            seen.add(k)
            earlier.append(e["desc"])
            if len(earlier) >= 40:
                break

        if chooser and (month or earlier):
            pick = self._ask(chooser, tx, month[:80], earlier)
            if pick:
                return pick
        if not exact:
            for name in earlier:                           # same words as a category of an earlier month
                if set(_tokens(name)) == want:
                    return ("name", name)
        return None

    @staticmethod
    def _ask(chooser, tx, month, earlier):
        rows = "\n".join(f"{i}. {e['desc']} ({int(e['amount'] or 0)})" for i, e in enumerate(month, 1)) or "(none)"
        names = "\n".join(f"- {n}" for n in earlier) or "(none)"
        user = (f'NEW ENTRY: "{tx.description}" ({tx.type}, {tx.amount} UZS)\n'
                f"THIS MONTH:\n{rows}\nEARLIER:\n{names}")
        try:
            raw = chooser(MATCH_SYSTEM, user) or ""
            m = re.search(r"\{.*\}", raw, re.S)
            obj = json.loads(m.group(0)) if m else None
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        idx = obj.get("merge")
        if isinstance(idx, int) and not isinstance(idx, bool) and 1 <= idx <= len(month):
            if _different(tx.description, month[idx - 1]["desc"]):
                return None                                # e.g. another person's wedding: its own row
            return ("merge", month[idx - 1])               # the model only picks a NUMBER; code does the rest
        name = obj.get("name")
        if isinstance(name, str) and name in earlier and not _different(tx.description, name):
            return ("name", name)
        return None

    # -- locate / write / undo
    def locate(self, tx: Tx, chooser=None) -> Slot:
        vals = self.api.get(self.id, a1(self.tab, "A:D"))
        last = self._last_row(vals)
        slot = Slot(row=last + 1, copy_from=last if last > self.header_rows else None)
        if self.merge:
            hit = self._match(tx, self._entries(vals), chooser)
            if hit and hit[0] == "merge":
                e = hit[1]
                return Slot(row=e["row"], merge=True, name=e["desc"],
                            prev=None if e["amount"] is None else int(e["amount"]))
            if hit:
                slot.name = hit[1]
        return slot

    def write(self, tx: Tx, slot: Slot) -> None:
        r = slot.row
        if slot.merge:                                     # only the Sum cell changes
            self.api.update(self.id, [{"range": a1(self.tab, f"C{r}"), "values": [[(slot.prev or 0) + tx.amount]]}])
            return
        gid, grid_rows = self.api.tab_info(self.id, self.tab)
        reqs = []
        if r > grid_rows:
            reqs.append({"appendDimension": {"sheetId": gid, "dimension": "ROWS", "length": 100}})
        if slot.copy_from:
            reqs += _copy_row(gid, slot.copy_from, r, 0, 4)
        if reqs:
            self.api.batch(self.id, reqs)
        self.api.update(self.id, [{
            "range": a1(self.tab, f"A{r}:D{r}"),
            "values": [[to_serial(tx.date), self.labels[tx.type], tx.amount, tx.description]],
        }])

    def undo(self, tx: Tx, row: int, prev=None, target=None) -> None:
        v = self.api.get(self.id, a1(self.tab, f"A{row}:D{row}"))
        r = (list(v[0]) if v else []) + [""] * 4
        if target:                                         # merged: take the amount back out
            if str(r[3]).strip() != target.strip() or (_num(r[2]) or 0) != (prev or 0) + tx.amount:
                raise FinanceError(f"Row {row} no longer matches what I wrote (you edited it?). "
                                   f"I left it alone, please fix it by hand.")
            if prev is None:
                self.api.clear(self.id, [a1(self.tab, f"C{row}")])
            else:
                self.api.update(self.id, [{"range": a1(self.tab, f"C{row}"), "values": [[prev]]}])
            return
        if not (_num(r[0]) == to_serial(tx.date) and str(r[1]).strip() == self.labels[tx.type]
                and _num(r[2]) == tx.amount and str(r[3]).strip() == tx.description):
            raise FinanceError(f"Row {row} no longer matches what I wrote (you edited it?). "
                               f"I left it alone, please fix it by hand.")
        self.api.clear(self.id, [a1(self.tab, f"A{row}:D{row}")])


class BlockSheet:
    """company: B=Sana, C=Tavsif, D=Kirim, E=Chiqim, F=Tur. Two pre-formatted blocks, one row per payment.

    Dates there are TEXT ('08.11.2026'), so they are written as text. F (Tur) is written only if it is
    completely empty, never if it holds a formula. Totals (=SUM(D3:D134) ...) are never touched.
    """
    AMOUNT_COL = {"income": "D", "expense": "E"}

    def __init__(self, api, cfg):
        self.api, self.id, self.tab = api, cfg["id"], cfg["tab"]
        self.labels = cfg["labels"]
        self.rows = {"income": cfg["income_rows"], "expense": cfg["expense_rows"]}
        self.date_fmt = cfg.get("date_format", "%d.%m.%Y")

    def peek(self, tx_type: str) -> Slot:
        first, last = self.rows[tx_type]
        block = self.api.get(self.id, a1(self.tab, f"B{first}:F{last}"), render="FORMULA")
        for i in range(last - first + 1):
            row = (list(block[i]) if i < len(block) else []) + [""] * 5
            if not any(str(c).strip() for c in row[:4]):
                return Slot(row=first + i, write_label=not str(row[4]).strip())
        raise FinanceError(
            f"The {tx_type} block (rows {first}-{last}) is full. Insert a row INSIDE the block in the sheet "
            f"so the SUM ranges grow, then update finance.sheets.company.{tx_type}_rows in config.yaml "
            f"(and shift the other block if it moved)."
        )

    def locate(self, tx: Tx, chooser=None) -> Slot:
        return self.peek(tx.type)

    def write(self, tx: Tx, slot: Slot) -> None:
        r, col = slot.row, self.AMOUNT_COL[tx.type]
        data = [
            {"range": a1(self.tab, f"B{r}:C{r}"), "values": [[tx.date.strftime(self.date_fmt), tx.description]]},
            {"range": a1(self.tab, f"{col}{r}"), "values": [[tx.amount]]},
        ]
        if slot.write_label:
            data.append({"range": a1(self.tab, f"F{r}"), "values": [[self.labels[tx.type]]]})
        self.api.update(self.id, data)

    def undo(self, tx: Tx, row: int, prev=None, target=None) -> None:
        v = self.api.get(self.id, a1(self.tab, f"B{row}:E{row}"))
        r = (list(v[0]) if v else []) + [""] * 4
        amount = r[2] if tx.type == "income" else r[3]
        if not (str(r[0]).strip() == tx.date.strftime(self.date_fmt) and str(r[1]).strip() == tx.description
                and _num(amount) == tx.amount):
            raise FinanceError(f"Row {row} no longer matches what I wrote (you edited it?). "
                               f"I left it alone, please fix it by hand.")
        self.api.clear(self.id, [a1(self.tab, f"B{row}:E{row}")])


# --------------------------------------------------------------------------- service

class FinanceService:
    def __init__(self, cfg: dict, db_path: Optional[str] = None, api=None, adapters: Optional[dict] = None,
                 audit: Optional[Callable[[str, dict, str, bool], None]] = None,
                 chooser: Optional[Callable[[str, str], str]] = None):
        self.cfg = cfg
        self.chooser = chooser   # chooser(system, user) -> str ; an LLM call that matches entries to rows
        self.db_path = str(db_path or ROOT / cfg.get("db_path", "linda.db"))
        self._api = api
        self._adapters = dict(adapters or {})
        self.audit = audit or (lambda tool, args, result, confirmed: None)
        self._lock = threading.RLock()
        con = sqlite3.connect(self.db_path, timeout=10)
        try:
            con.executescript(SCHEMA_SQL)
            have = {r[1] for r in con.execute("PRAGMA table_info(finance)")}
            for col, ddl in (("prev_amount", "INTEGER"), ("target_desc", "TEXT")):   # DBs created by v1
                if col not in have:
                    con.execute(f"ALTER TABLE finance ADD COLUMN {col} {ddl}")
            con.commit()
        finally:
            con.close()

    @classmethod
    def from_config(cls, path=None, **kw) -> "FinanceService":
        import yaml
        p = Path(path or ROOT / "config.yaml")
        cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("finance")
        if not cfg:
            raise FinanceError("config.yaml has no 'finance:' section.")
        cfg.setdefault("credentials_file", os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
        return cls(cfg, **kw)

    # -- db helpers
    def _run(self, sql, params=(), fetch=False):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                cur = con.execute(sql, params)
                return cur.fetchall() if fetch else cur.lastrowid
        finally:
            con.close()


    def balance(self, sheet: str) -> dict:
        r = self._run(
            "SELECT COALESCE(SUM(CASE WHEN type='income' THEN amount END),0) AS inc, "
            "COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) AS exp, "
            "COUNT(*) AS n, MIN(tx_date) AS since FROM finance WHERE status='active' AND sheet=?",
            (sheet,), fetch=True)[0]
        return dict(r)

    def get_state(self, key):
        rows = self._run("SELECT value FROM finance_state WHERE key=?", (key,), fetch=True)
        return rows[0]["value"] if rows else None

    def set_state(self, key, value):
        self._run("INSERT INTO finance_state(key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # -- sheets
    def _adapter(self, sheet: str):
        if sheet not in self._adapters:
            scfg = (self.cfg.get("sheets") or {}).get(sheet)
            if not scfg:
                raise FinanceError(f"Sheet '{sheet}' is not configured in finance.sheets.")
            if self._api is None:
                self._api = SheetsAPI(self.cfg.get("credentials_file", ""))
            cls = {"flat": FlatSheet, "blocks": BlockSheet}[scfg["layout"]]
            self._adapters[sheet] = cls(self._api, scfg)
        return self._adapters[sheet]

    @staticmethod
    def _wrap(e: Exception) -> FinanceError:
        if isinstance(e, FinanceError):
            return e
        return FinanceError(f"Google Sheets error: {e}")

    # -- public API (Telegram and tools.py both use these)
    def parse(self, text: str) -> Tx:
        return parse_message(text, default_sheet=self.cfg.get("default_sheet", "my_finance"))

    def needs_confirmation(self, tx: Tx) -> bool:
        return tx.sheet in self.cfg.get("confirm_sheets", ["company"])

    def describe(self, tx: Tx) -> str:
        sign = "➕ Income" if tx.type == "income" else "➖ Expense"
        return f"{tx.sheet} · {sign} {fmt_amount(tx.amount)} UZS · {tx.date:%d.%m.%Y} · {tx.description}"

    def add(self, tx: Tx, confirmed: bool = False, source: str = "telegram") -> Tx:
        if self.needs_confirmation(tx) and not confirmed:
            raise FinanceError("This sheet needs confirmation first.")
        args = {"sheet": tx.sheet, "type": tx.type, "amount": tx.amount,
                "description": tx.description, "date": tx.date.isoformat()}
        with self._lock:
            tx.db_id = self._run(
                "INSERT INTO finance(tx_date, sheet, type, amount, description, source, raw_text, confirmed, status) "
                "VALUES (?,?,?,?,?,?,?,?, 'pending')",
                (tx.date.isoformat(), tx.sheet, tx.type, tx.amount, tx.description, source, tx.raw, int(confirmed)),
            )
            try:
                ad = self._adapter(tx.sheet)
                slot = ad.locate(tx, self.chooser)
                if slot.name:                      # merge: the row's own name; new row: name used in earlier months
                    tx.description = slot.name
                ad.write(tx, slot)
            except Exception as e:
                err = self._wrap(e)
                self._run("UPDATE finance SET status='failed', error=? WHERE id=?", (str(err), tx.db_id))
                self.audit("finance.add", args, f"failed: {err}", confirmed)
                raise err from e
            tx.row, tx.merged, tx.prev_amount = slot.row, slot.merge, slot.prev
            self._run("UPDATE finance SET status='active', sheet_row=?, description=?, prev_amount=?, target_desc=? "
                      "WHERE id=?", (tx.row, tx.description, slot.prev if slot.merge else None,
                                     slot.name if slot.merge else None, tx.db_id))
            self.audit("finance.add", args, f"ok row {tx.row}" + (" (merged)" if slot.merge else ""), confirmed)
        return tx

    def undo_last(self, sheet: Optional[str] = None) -> Tx:
        with self._lock:
            q = "SELECT * FROM finance WHERE status='active' AND source <> 'import'" + \
                (" AND sheet=?" if sheet else "") + " ORDER BY id DESC LIMIT 1"
            rows = self._run(q, (sheet,) if sheet else (), fetch=True)
            if not rows:
                raise FinanceError("Nothing to undo.")
            r = rows[0]
            tx = Tx(r["sheet"], r["type"], r["amount"], r["description"],
                    dt.date.fromisoformat(r["tx_date"]), row=r["sheet_row"], db_id=r["id"],
                    merged=bool(r["target_desc"]), prev_amount=r["prev_amount"])
            try:
                self._adapter(tx.sheet).undo(tx, tx.row, prev=r["prev_amount"], target=r["target_desc"])
            except Exception as e:
                raise self._wrap(e) from e
            self._run("UPDATE finance SET status='undone', undone_at=datetime('now') WHERE id=?", (tx.db_id,))
            self.audit("finance.undo", {"id": tx.db_id, "sheet": tx.sheet, "row": tx.row}, "ok", True)
            return tx

    # -- analysis (SQL only; only covers entries made through Linda)
    @staticmethod
    def _month_range(month: Optional[str]):
        month = month or dt.date.today().strftime("%Y-%m")
        try:
            y, m = map(int, month.split("-"))
            start = dt.date(y, m, 1)
        except ValueError:
            raise FinanceError("Month format: YYYY-MM, e.g. 2026-09")
        end = dt.date(y + (m == 12), m % 12 + 1, 1)
        return month, start.isoformat(), end.isoformat()

    def recent(self, sheet: str, tx_type: str = "expense", n: int = 5) -> list:
        return [dict(r) for r in self._run(
            "SELECT tx_date, amount, description FROM finance WHERE status='active' AND sheet=? AND type=? "
            "ORDER BY tx_date DESC, id DESC LIMIT ?", (sheet, tx_type, n), fetch=True)]

    def summary(self, month: Optional[str] = None, top: int = 5) -> dict:
        month, a, b = self._month_range(month)
        out: dict = {"month": month, "sheets": {}}
        for r in self._run("SELECT sheet, type, SUM(amount) AS total FROM finance WHERE status='active' "
                           "AND tx_date>=? AND tx_date<? GROUP BY sheet, type", (a, b), fetch=True):
            out["sheets"].setdefault(r["sheet"], {"income": 0, "expense": 0, "top": []})[r["type"]] = r["total"]
        for sh, d in out["sheets"].items():
            d["balance"] = d["income"] - d["expense"]
            d["top"] = [(r["d"], r["total"]) for r in self._run(
                "SELECT MIN(description) AS d, SUM(amount) AS total FROM finance WHERE status='active' "
                "AND type='expense' AND sheet=? AND tx_date>=? AND tx_date<? GROUP BY lower(description) "
                "ORDER BY total DESC LIMIT ?", (sh, a, b, top), fetch=True)]
        return out



    def format_summary(self, month: Optional[str] = None) -> str:
        s = self.summary(month)
        if not s["sheets"]:
            return f"📊 {s['month']}: no entries made through Linda yet."
        lines = [f"📊 {s['month']} (entries made through Linda)"]
        for sh, d in s["sheets"].items():
            lines.append(f"{sh}: +{fmt_amount(d['income'])} · −{fmt_amount(d['expense'])} · "
                         f"balance {fmt_amount(d['balance'])} UZS")
            if d["top"]:
                lines.append("  top expenses: " + ", ".join(f"{n} {fmt_amount(t)}" for n, t in d["top"]))
        return "\n".join(lines)

    def search_total(self, keyword: str, month: Optional[str] = None, sheet: Optional[str] = None,
                     type: str = "expense") -> tuple[int, int]:
        """'How much did I spend on ads in September?' -> (total, count)."""
        sql = "SELECT COALESCE(SUM(amount),0) AS t, COUNT(*) AS n FROM finance WHERE status='active' " \
              "AND type=? AND lower(description) LIKE ? ESCAPE '\\'"
        like = "%" + keyword.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params: list = [type, like]
        if month:
            _, a, b = self._month_range(month)
            sql += " AND tx_date>=? AND tx_date<?"
            params += [a, b]
        if sheet:
            sql += " AND sheet=?"
            params.append(sheet)
        r = self._run(sql, tuple(params), fetch=True)[0]
        return r["t"], r["n"]


# --------------------------------------------------------------------------- CLI

def _main(argv: list) -> int:
    if not argv or argv[0] not in ("parse", "check"):
        print(__doc__)
        return 1
    try:
        if argv[0] == "parse":
            print(parse_message(" ".join(argv[1:])))
        else:
            svc = FinanceService.from_config()
            for name, scfg in svc.cfg["sheets"].items():
                ad = svc._adapter(name)
                kinds = ("income", "expense") if scfg["layout"] == "blocks" else ("income",)
                for kind in kinds:
                    print(f"{name:11} {kind:8} next new row -> {ad.peek(kind)}")
    except FinanceError as e:
        print("ERROR:", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))