"""Clients base of the agency (study / work / language courses in Germany): linda.db + the Google Sheet.

TABLES   clients          one row per client (contact details, certificate, course, city, visa, payment, comment)
         client_steps     progress per client and step (the TRUE/FALSE columns of the sheet)
         client_accounts  logins per service (Proton, TLS, Expatrio, ...). Stored in plain text, by the owner's choice.
         client_log       every change, so the last one can be undone
SHEET    columns are found by their header text (row 1 names, row 2 step names under numbered columns), so moving
         a column in the sheet does not break anything. Rows are found by the client's name in the name column.
Writes go to the database first, then to the sheet; a sheet error is reported but the database keeps the data.
"""
import datetime
import difflib
import json
import re
import sqlite3
import time

from linda.business.finance import FinanceError, SheetsAPI

SCHEMA = """
create table if not exists clients (
  id integer primary key, name text not null, phone text default '', email text default '', dob text default '',
  address text default '', certificate text default '', course text default '', city text default '',
  visa_applied text default '', visa_answer text default '', payment text default '', comment text default '',
  info_raw text default '', created_ts real, updated_ts real);
create table if not exists client_steps (
  client_id integer not null, step_no integer not null, step_name text default '', done integer default 0,
  done_date text default '', primary key (client_id, step_no));
create table if not exists client_accounts (
  id integer primary key, client_id integer not null, service text not null, login text default '',
  password text default '', unique (client_id, service));
create table if not exists client_log (id integer primary key, ts real, action text, client_id integer, data text);
"""

# today's header of the sheet; replaced by what the sheet itself says when it can be read
DEFAULT_STEPS = {1: "Konsultatsiya", 3: "Proton mail", 4: "TLS akkaunt", 5: "TLS terminni olib berish",
                 6: "Til markazi to'lov + hujjatlari", 7: "Expatrio hisob ochish", 8: "Expatrio hujjatlar",
                 9: "CV / Motivatsion xat", 10: "Xujjat Tarjimalari into German", 11: "Videx forma",
                 12: "Viza kutilmoqda"}
STEP_HELP = ", ".join(f"{n} {s}" for n, s in DEFAULT_STEPS.items())
# header words -> field (row 1, lower case, first match wins)
HEADERS = [("name", ("klient ismi", "client name", "ism")), ("info", ("client info", "info")),
           ("certificate", ("sertifikat", "certificate")), ("course", ("kurs", "o'qish", "course")),
           ("city", ("shahar", "univer", "city")), ("visa_applied", ("viza ariza",)),
           ("visa_answer", ("viza javobi",)), ("payment", ("to'lov", "tolov", "payment")),
           ("comment", ("comment", "izoh"))]
FIELDS = ("phone", "email", "dob", "address", "passport", "passport_valid", "certificate", "course", "city",
          "visa_applied", "visa_answer", "payment", "comment", "other")
INFO_FIELDS = ("phone", "email", "dob", "address", "passport", "passport_valid", "other", "account")   # live in column B
NEW_COLUMNS = ("passport", "passport_valid", "other")
# an account implies the steps before it: a Proton mailbox means the consultation happened and Proton is open
IMPLIED_STEPS = [(r"proton", {1, 3}), (r"tls", {1, 4}), (r"expa\w{2,3}io|ekspatrio", {1, 7})]
PHONE = re.compile(r"\+?\s?998[\d\s\-()]{9,14}\d|\b\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
DOB = re.compile(r"\b(\d{1,2})\s?[./-]\s?(\d{1,2})\s?[./-]\s?(\d{4})\b")
PASSPORT = re.compile(r"\b([A-Z]{2})\s?(\d{7})\b")
VALID = re.compile(DOB.pattern + r"\s*[-–—]\s*" + DOB.pattern)
PROTON = re.compile(r"[\w.+-]+@(?:protonmail\.com|proton\.me|pm\.me)", re.I)

PARSE = """Extract ONE client of an agency that sends people to Germany (study, work, language courses).
Return ONLY JSON:
{{"name": "", "phone": "", "email": "", "dob": "", "address": "", "passport": "", "passport_valid": "",
 "certificate": "", "course": "", "city": "", "accounts": [{{"service": "", "login": "", "password": ""}}],
 "steps_done": [], "visa_applied": "", "visa_answer": "", "payment": "", "comment": "", "other": ""}}
Rules: copy every value exactly as written, never invent, "" when missing. Partial messages are normal
(only an email and a password, only a passport): fill what is there and leave the rest "".
- name: the client's full name ONLY if written, else "".  dob: date of birth as YYYY-MM-DD.
- passport: passport number (two letters + 7 digits).  passport_valid: its validity, e.g. "15.07.2022 - 14.07.2032".
- other: any other code or line you cannot place (e.g. issuing authority), copied as written.
- a login like name@protonmail.com with a password next to it is a "Proton mail" account.
- certificate: language certificate (B1, B2, IELTS 6.5, "Yo'q").  course: what/how long (4 oy, Magistratura, Ausbildung).
- city: German city or university.  payment: payment status.  comment: anything else worth keeping.
- accounts: logins (and passwords) for Proton mail, TLS, Expatrio or other services.
- steps_done: step numbers the message says are ALREADY done. Steps: {steps}."""


def col_letter(i):                      # 0 -> A, 26 -> AA
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _norm(s):
    return " ".join(re.findall(r"\w+", str(s or "").lower().replace("'", "")))


def _dob(s):
    """'17.04.2008' / '2008-04-17' -> '2008-04-17' ('' if not a date)."""
    s = str(s or "").strip()
    try:
        return datetime.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        m = DOB.search(s)
        if m:
            try:
                return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            except ValueError:
                pass
    return ""


def _bool(v, default=True):
    if v is None or v == "":
        return default
    return str(v).strip().lower() not in ("false", "0", "no", "yo'q", "нет", "undone", "not done")


class ClientsService:
    def __init__(self, db_path, sheet_id="", tab="Sheet1", header_rows=2, credentials_file="", llm=None):
        self.db_path, self.sid, self.tab, self.hdr = str(db_path), sheet_id, tab, header_rows
        self.llm = llm
        self.api = SheetsAPI(credentials_file) if (sheet_id and credentials_file) else None
        self.cols, self.steps, self.step_cols = {}, dict(DEFAULT_STEPS), {}
        con = self._con()
        try:
            con.executescript(SCHEMA)
            have = {r[1] for r in con.execute("pragma table_info(clients)")}
            for col in NEW_COLUMNS:
                if col not in have:
                    con.execute(f"alter table clients add column {col} text default ''")
            con.commit()
        finally:
            con.close()

    @classmethod
    def from_config(cls, cfg, db_path, llm=None):
        c = cfg.get("clients") or {}
        cred = c.get("credentials_file") or (cfg.get("finance") or {}).get("credentials_file", "")
        return cls(db_path, c.get("spreadsheet_id", ""), c.get("tab", "Sheet1"), int(c.get("header_rows", 2)), cred, llm)

    def _con(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    # ---------------- sheet layout ----------------
    def _layout(self):
        """Read the two header rows once: which column holds what, and which columns are steps."""
        if self.cols or not self.api:
            return
        rows = self.api.get(self.sid, f"'{self.tab}'!A1:AZ{self.hdr}")
        top = rows[0] if rows else []
        sub = rows[1] if len(rows) > 1 else []
        steps, step_cols = {}, {}
        for i, h in enumerate(top):
            h = str(h).strip()
            if re.fullmatch(r"\d{1,2}", h):
                n = int(h)
                steps[n] = (str(sub[i]).strip() if i < len(sub) else "") or DEFAULT_STEPS.get(n, f"Step {n}")
                step_cols[n] = i
                continue
            low = h.lower()
            for field, words in HEADERS:
                if field not in self.cols and any(w in low for w in words):
                    self.cols[field] = i
                    break
        if "name" not in self.cols:
            raise FinanceError(f"The clients sheet has no 'Klient ismi' column in row 1 of tab '{self.tab}'.")
        if steps:
            self.steps, self.step_cols = steps, step_cols

    def _find_row(self, name):
        """Sheet row number of this client (by the name column), or None."""
        self._layout()
        c = col_letter(self.cols["name"])
        vals = self.api.get(self.sid, f"'{self.tab}'!{c}{self.hdr + 1}:{c}")
        want = _norm(name)
        for i, r in enumerate(vals):
            if r and _norm(r[0]) == want:
                return self.hdr + 1 + i
        return None

    def _next_row(self):
        self._layout()
        c = col_letter(self.cols["name"])
        vals = self.api.get(self.sid, f"'{self.tab}'!{c}{self.hdr + 1}:{c}")
        last = max((i for i, r in enumerate(vals) if r and str(r[0]).strip()), default=-1)
        return self.hdr + 2 + last

    def _info_cell(self, c, accounts):
        """Column B. A client imported from the sheet keeps its original text; only new values are added below it."""
        lines = []
        if c["dob"]:
            d = datetime.date.fromisoformat(c["dob"])
            lines.append(f"DOB: {d:%d.%m.%Y}")
        lines += [x for x in (c["phone"], c["email"], c["address"]) if x]
        if c.get("passport"):
            lines.append(f"Passport: {c['passport']}" + (f" ({c['passport_valid']})" if c.get("passport_valid") else ""))
        if c.get("other"):
            lines.append(c["other"])
        lines += [f"{a['service']}: {a['login']}" + (f" / {a['password']}" if a["password"] else "") for a in accounts]
        raw = (c["info_raw"] or "").strip()
        if not raw:
            return "\n".join(lines)
        flat = _norm(raw).replace(" ", "")
        new = [l for l in lines if _norm(l).replace(" ", "") not in flat
               and not (l.startswith("DOB: ") and c["dob"] and _dob(raw) == c["dob"])]
        return "\n".join([raw] + new)

    def _sheet_write_client(self, cid, row=None):
        """Write the whole row of one client (used for new clients and after field changes)."""
        if not self.api:
            return "no sheet configured"
        self._layout()
        con = self._con()
        try:
            c = dict(con.execute("select * from clients where id=?", (cid,)).fetchone())
            accounts = [dict(r) for r in con.execute("select * from client_accounts where client_id=? order by id", (cid,))]
            steps = {r["step_no"]: r["done"] for r in con.execute("select * from client_steps where client_id=?", (cid,))}
        finally:
            con.close()
        row = row or self._find_row(c["name"]) or self._next_row()
        data = []
        put = lambda col, v: data.append({"range": f"'{self.tab}'!{col_letter(col)}{row}", "values": [[v]]})
        put(self.cols["name"], c["name"])
        if "info" in self.cols:
            put(self.cols["info"], self._info_cell(c, accounts))
        for f in ("certificate", "course", "city", "visa_applied", "visa_answer", "payment", "comment"):
            if f in self.cols and c[f]:
                put(self.cols[f], c[f])
        for n, col in self.step_cols.items():
            if n in steps:
                put(col, "TRUE" if steps[n] else "FALSE")
        self.api.update(self.sid, data)
        return row

    def _sheet_write_fields(self, cid, fields):
        """Write only these fields' cells (hand edits in other cells of the row stay untouched)."""
        self._layout()
        con = self._con()
        try:
            c = dict(con.execute("select * from clients where id=?", (cid,)).fetchone())
            accounts = [dict(r) for r in con.execute("select * from client_accounts where client_id=? order by id", (cid,))]
        finally:
            con.close()
        row = self._find_row(c["name"])
        if row is None:
            return self._sheet_write_client(cid)
        data = []
        for f in set(fields):
            if f in INFO_FIELDS and "info" in self.cols:
                col, v = self.cols["info"], self._info_cell(c, accounts)
            elif f in self.cols:
                col, v = self.cols[f], c[f]
            else:
                continue
            data.append({"range": f"'{self.tab}'!{col_letter(col)}{row}", "values": [[v]]})
        if data:
            self.api.update(self.sid, data)
        return row

    # ---------------- parsing ----------------
    def parse(self, text):
        """Whatever the message holds; nothing is required. Code fills what the model missed."""
        obj = None
        if self.llm:
            raw = self.llm(PARSE.format(steps=", ".join(f"{n} {s}" for n, s in self.steps.items())), text) or ""
            m = re.search(r"\{.*\}", raw, re.S)
            try:
                obj = json.loads(m.group(0)) if m else None
            except ValueError:
                obj = None
        obj = obj if isinstance(obj, dict) else {}
        out = {k: str(obj.get(k) or "").strip()[:500] for k in ("name",) + FIELDS}
        valid = VALID.search(text)
        rest = VALID.sub(" ", text)                             # validity dates are not the birth date
        out["dob"] = _dob(out["dob"]) or _dob(rest)
        if not out["passport_valid"] and valid:
            out["passport_valid"] = re.sub(r"\s+", "", valid.group(0)).replace("-", " - ")
        if not out["passport"]:
            m = PASSPORT.search(text)
            out["passport"] = m.group(1) + m.group(2) if m else ""
        if not out["email"]:
            m = EMAIL.search(text)
            out["email"] = m.group(0) if m else ""
        if not out["phone"]:
            m = PHONE.search(rest)
            out["phone"] = m.group(0).strip() if m else ""
        accounts = [{"service": str(a.get("service") or "").strip()[:40], "login": str(a.get("login") or "").strip()[:200],
                     "password": str(a.get("password") or "").strip()[:200]}
                    for a in (obj.get("accounts") or []) if isinstance(a, dict) and a.get("service")]
        pm = PROTON.search(text)
        if pm and not any("proton" in a["service"].lower() for a in accounts):
            after = text[pm.end():].split()                   # the password is the next word after the address
            pw = after[0] if after and not EMAIL.match(after[0]) and not DOB.match(after[0]) else ""
            accounts.append({"service": "Proton mail", "login": pm.group(0), "password": pw})
        out["accounts"] = [x for x in accounts if x["login"] or x["password"]]
        if not out["name"]:
            out["name"] = self._name_from(text, pm.group(0) if pm else out["email"])
        steps = {int(n) for n in (obj.get("steps_done") or []) if str(n).isdigit() and int(n) in self.steps}
        for a in accounts:
            for rx, implied in IMPLIED_STEPS:
                if re.search(rx, a["service"], re.I):
                    steps |= {n for n in implied if n in self.steps}
        if DONE.search(text) and not UNDONE.search(text):     # 'Expatrio account done' in the text itself
            steps |= {n for n in _steps_in(text) if n in self.steps}
        out["steps_done"] = sorted(steps)
        return out

    @staticmethod
    def _name_from(text, email):
        """A line that is just two-four capitalised words, else the mailbox name: 'ali.valiyev@...' -> 'Ali Valiyev'."""
        for line in text.splitlines():
            l = line.strip()
            if re.fullmatch(r"[A-ZА-ЯЁ][\w'ʻ-]+(?:\s+[A-ZА-ЯЁ][\w'ʻ-]+){1,3}", l) and not PASSPORT.search(l):
                return l
        if email:
            local = re.sub(r"\d+", "", email.split("@")[0])
            parts = [w for w in re.split(r"[._-]+", local) if len(w) > 1]
            if parts:
                return " ".join(w.capitalize() for w in parts)
        return ""

    def match(self, p):
        """The existing client these details belong to: same email/login, same passport, or the same name
        allowing small spelling differences ('Karimov' = 'Karimova', 'Abdullayev' = 'Abdullaev'). -> client id or None."""
        con = self._con()
        try:
            rows = [dict(r) for r in con.execute("select * from clients")]
            logins = {}
            for r in con.execute("select client_id, login from client_accounts"):
                logins.setdefault(r["login"].lower(), r["client_id"])
        finally:
            con.close()
        mails = {a["login"].lower() for a in p.get("accounts", []) if a.get("login")} | ({p["email"].lower()} if p.get("email") else set())
        for m in mails:
            if m in logins:
                return logins[m]
            for r in rows:
                if r["email"] and r["email"].lower() == m or m and m in (r["info_raw"] or "").lower():
                    return r["id"]
        if p.get("passport"):
            for r in rows:
                if p["passport"] in (r.get("passport") or "") or p["passport"] in (r["info_raw"] or "").replace(" ", ""):
                    return r["id"]
        want = _norm(p.get("name")).split()
        if len(want) == 1 and len(want[0]) >= 5:               # 'Jasurbek' alone: only if exactly one client fits
            fits = [r["id"] for r in rows if any(difflib.SequenceMatcher(None, want[0], h).ratio() >= 0.85
                                                 for h in _norm(r["name"]).split())]
            if len(fits) == 1:
                return fits[0]
        if len(want) >= 2:
            def close(a, b):
                return a == b or (len(a) > 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)
            for r in rows:
                have = _norm(r["name"]).split()
                if len(have) >= 2 and all(any(close(w, h) for h in have) for w in want) \
                        and all(any(close(h, w) for w in want) for h in have):
                    return r["id"]
        return None

    # ---------------- lookups ----------------
    def find(self, query):
        """-> (client row dict, None) or (None, message). Matches every word of the query against the name."""
        q = _norm(query).split()
        if not q:
            return None, "Which client?"
        con = self._con()
        try:
            rows = [dict(r) for r in con.execute("select * from clients order by id")]
        finally:
            con.close()
        def fits(name):
            words = _norm(name).split()
            return all(any(w.startswith(t) or (len(t) > 4 and difflib.SequenceMatcher(None, w, t).ratio() > 0.8)
                           for w in words) for t in q)
        hits = [r for r in rows if fits(r["name"])]
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, "Several clients match: " + "; ".join(r["name"] for r in hits) + ". Which one?"
        return None, f"No client matches '{query}'."

    def named_in(self, text):
        """Clients whose name (a word of 4+ letters) starts a word of the text: 'Jasurning telefoni' -> Jasur."""
        words = _norm(text).split()
        con = self._con()
        try:
            rows = [dict(r) for r in con.execute("select id, name from clients")]
        finally:
            con.close()
        hits = []
        for r in rows:
            parts = [p for p in _norm(r["name"]).split() if len(p) >= 4]
            if any(w.startswith(p) for p in parts for w in words):
                hits.append(r)
        return hits

    def card(self, cid):
        con = self._con()
        try:
            c = dict(con.execute("select * from clients where id=?", (cid,)).fetchone())
            accs = [dict(r) for r in con.execute("select * from client_accounts where client_id=? order by id", (cid,))]
            st = {r["step_no"]: dict(r) for r in con.execute("select * from client_steps where client_id=?", (cid,))}
        finally:
            con.close()
        info = [f"{k}: {c[k]}" for k in ("phone", "email", "dob", "address", "certificate", "course", "city") if c[k]]
        if not info and c["info_raw"]:
            info = c["info_raw"].splitlines()
        steps = [("✅ " if st.get(n, {}).get("done") else "⬜ ") + f"{n}. {s}"
                 + (f" ({st[n]['done_date']})" if st.get(n, {}).get("done_date") else "") for n, s in self.steps.items()]
        extra = [f"{k.replace('_', ' ')}: {c[k]}" for k in ("visa_applied", "visa_answer", "payment", "comment") if c[k]]
        done = sum(1 for n in self.steps if st.get(n, {}).get("done"))
        return {"name": c["name"], "info": info, "accounts": [f"{a['service']}: {a['login']}"
                + (f" / {a['password']}" if a["password"] else "") for a in accs],
                "steps": steps, "extra": extra, "done": done, "total": len(self.steps)}

    def listing(self, step=None, done=False):
        con = self._con()
        try:
            rows = [dict(r) for r in con.execute("select id, name from clients order by id")]
            st = {(r["client_id"], r["step_no"]): r["done"] for r in con.execute("select * from client_steps")}
        finally:
            con.close()
        out = []
        for r in rows:
            if step is not None and bool(st.get((r["id"], step))) != done:
                continue
            n_done = sum(1 for n in self.steps if st.get((r["id"], n)))
            nxt = next((f"{n}. {s}" for n, s in self.steps.items() if not st.get((r["id"], n))), "all done")
            out.append({"name": r["name"], "done": n_done, "total": len(self.steps), "next": nxt})
        return out

    # ---------------- writes ----------------
    def _log(self, con, action, cid, data):
        con.execute("insert into client_log (ts, action, client_id, data) values (?,?,?,?)",
                    (time.time(), action, cid, json.dumps(data, ensure_ascii=False, default=str)))

    def add(self, p):
        """p: parse() output. -> (client id, sheet row or error text)."""
        if not p.get("name"):
            raise FinanceError("I need at least the client's name.")
        dup, _ = self.find(p["name"])
        if dup and _norm(dup["name"]) == _norm(p["name"]):
            raise FinanceError(f"{dup['name']} is already in the clients base. Say what to change instead.")
        now = time.time()
        con = self._con()
        try:
            cur = con.execute(f"insert into clients (name, {', '.join(FIELDS)}, created_ts, updated_ts) "
                              f"values ({', '.join('?' * (len(FIELDS) + 3))})",
                              (p["name"],) + tuple(p.get(f, "") for f in FIELDS) + (now, now))
            cid = cur.lastrowid
            for n, s in self.steps.items():
                done = n in p.get("steps_done", [])
                con.execute("insert into client_steps values (?,?,?,?,?)",
                            (cid, n, s, int(done), str(datetime.date.today()) if done else ""))
            for a in p.get("accounts", []):
                con.execute("insert or replace into client_accounts (client_id, service, login, password) values (?,?,?,?)",
                            (cid, a["service"], a["login"], a["password"]))
            self._log(con, "add", cid, {})
            con.commit()
        finally:
            con.close()
        return cid, self._sheet(lambda: self._sheet_write_client(cid))

    def upsert(self, p):
        """New client, or new info for an existing one. -> (client id, created?, what changed, sheet note)."""
        cid = self.match(p)
        if cid is None:
            cid, res = self.add(p)
            return cid, True, [], res
        changed, res = self.update(cid, p)
        return cid, False, changed, res

    def update(self, cid, p):
        """Fill in / replace the fields the message has, add accounts, tick steps. Never clears anything."""
        con = self._con()
        try:
            old = dict(con.execute("select * from clients where id=?", (cid,)).fetchone())
            accs = {r["service"].lower(): dict(r) for r in con.execute("select * from client_accounts where client_id=?", (cid,))}
            steps = {r["step_no"]: dict(r) for r in con.execute("select * from client_steps where client_id=?", (cid,))}
            snapshot = {"client": old, "accounts": list(accs.values()), "steps": list(steps.values())}
            changed, fields = [], []
            for f in FIELDS:
                v = str(p.get(f) or "").strip()
                if v and v != (old.get(f) or ""):
                    con.execute(f"update clients set {f}=? where id=?", (v, cid))
                    changed.append(f"{f.replace('_', ' ')}{' (was ' + old[f] + ')' if old.get(f) else ''}")
                    fields.append(f)
            for a in p.get("accounts", []):
                cur = accs.get(a["service"].lower())
                if cur and cur["login"] == a["login"] and (cur["password"] == a["password"] or not a["password"]):
                    continue
                con.execute("insert or replace into client_accounts (client_id, service, login, password) values (?,?,?,?)",
                            (cid, a["service"], a["login"] or (cur or {}).get("login", ""),
                             a["password"] or (cur or {}).get("password", "")))
                changed.append(f"{a['service']} account")
                fields.append("account")
            new_steps = [n for n in p.get("steps_done", []) if not steps.get(n, {}).get("done")]
            for n in new_steps:
                con.execute("insert or replace into client_steps values (?,?,?,?,?)",
                            (cid, n, self.steps.get(n, ""), 1, str(datetime.date.today())))
            if new_steps:
                changed.append("steps " + ", ".join(map(str, new_steps)) + " done")
            if changed:
                con.execute("update clients set updated_ts=? where id=?", (time.time(), cid))
                self._log(con, "update", cid, snapshot)
            con.commit()
        finally:
            con.close()
        if not changed:
            return [], None
        res = self._sheet(lambda: self._sheet_write_fields(cid, fields) if fields else None)
        if new_steps:
            r2 = self.set_steps_sheet(cid, {n: True for n in new_steps})
            res = res if isinstance(r2, int) or res is None else res
        return changed, res

    def set_steps(self, cid, steps, done=True):
        bad = [n for n in steps if n not in self.steps]
        if bad:
            raise FinanceError(f"Unknown step {bad}. Steps: " + ", ".join(f"{n} {s}" for n, s in self.steps.items()))
        con = self._con()
        try:
            prev = {r["step_no"]: [r["done"], r["done_date"]] for r in
                    con.execute("select * from client_steps where client_id=?", (cid,))}
            for n in steps:
                con.execute("insert or replace into client_steps values (?,?,?,?,?)",
                            (cid, n, self.steps[n], int(done), str(datetime.date.today()) if done else ""))
            con.execute("update clients set updated_ts=? where id=?", (time.time(), cid))
            self._log(con, "steps", cid, {str(n): prev.get(n, [0, ""]) for n in steps})
            con.commit()
        finally:
            con.close()
        return self.set_steps_sheet(cid, {n: done for n in steps})

    def set_steps_sheet(self, cid, states):
        """states: {step_no: done}. Writes only those checkbox cells."""
        def write():
            self._layout()
            con = self._con()
            try:
                name = con.execute("select name from clients where id=?", (cid,)).fetchone()["name"]
            finally:
                con.close()
            row = self._find_row(name)
            if row is None:
                return self._sheet_write_client(cid)
            data = [{"range": f"'{self.tab}'!{col_letter(self.step_cols[n])}{row}", "values": [["TRUE" if d else "FALSE"]]}
                    for n, d in states.items() if n in self.step_cols]
            if data:
                self.api.update(self.sid, data)
            return row
        return self._sheet(write)

    def set_field(self, cid, field, value):
        field = field.strip().lower().replace(" ", "_")
        con = self._con()
        try:
            if field in ("account", "accounts", "login", "password"):
                m = re.match(r"\s*([^:]+):\s*(\S+)(?:\s*/\s*(.+))?", value or "")
                if not m:
                    raise FinanceError("Write the account as 'Service: login / password'.")
                svc, login, pw = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
                old = con.execute("select login, password from client_accounts where client_id=? and service=?",
                                  (cid, svc)).fetchone()
                con.execute("insert or replace into client_accounts (client_id, service, login, password) values (?,?,?,?)",
                            (cid, svc, login, pw))
                self._log(con, "account", cid, {"service": svc, "old": dict(old) if old else None})
                field = "account"
            elif field in FIELDS:
                value = _dob(value) if field == "dob" else str(value or "").strip()[:500]
                old = con.execute(f"select {field} from clients where id=?", (cid,)).fetchone()[0]
                con.execute(f"update clients set {field}=?, updated_ts=? where id=?", (value, time.time(), cid))
                self._log(con, "field", cid, {"field": field, "old": old})
            else:
                raise FinanceError(f"Unknown field '{field}'. Fields: {', '.join(FIELDS)}, account.")
            con.commit()
        finally:
            con.close()
        return self._sheet(lambda: self._sheet_write_fields(cid, [field]))

    def undo_last(self):
        con = self._con()
        try:
            r = con.execute("select * from client_log order by id desc limit 1").fetchone()
            if not r:
                raise FinanceError("Nothing to undo in the clients base.")
            cid, data = r["client_id"], json.loads(r["data"] or "{}")
            c = con.execute("select name from clients where id=?", (cid,)).fetchone()
            name = c["name"] if c else "?"
            if r["action"] == "add":
                row = self._sheet(lambda: self._find_row(name)) if self.api else None
                for t in ("client_steps", "client_accounts"):
                    con.execute(f"delete from {t} where client_id=?", (cid,))
                con.execute("delete from clients where id=?", (cid,))
                what = f"Removed the new client {name}"
            elif r["action"] == "update":
                old = data["client"]
                cols = [k for k in old if k != "id"]
                con.execute(f"update clients set {', '.join(f'{k}=?' for k in cols)} where id=?",
                            [old[k] for k in cols] + [cid])
                con.execute("delete from client_accounts where client_id=?", (cid,))
                for a in data["accounts"]:
                    con.execute("insert into client_accounts (client_id, service, login, password) values (?,?,?,?)",
                                (cid, a["service"], a["login"], a["password"]))
                for st in data["steps"]:
                    con.execute("insert or replace into client_steps values (?,?,?,?,?)",
                                (cid, st["step_no"], st["step_name"], st["done"], st["done_date"]))
                what = f"{name}: the last update was undone"
            elif r["action"] == "steps":
                for n, (d, date) in data.items():
                    con.execute("update client_steps set done=?, done_date=? where client_id=? and step_no=?",
                                (d, date, cid, int(n)))
                what = f"{name}: steps {', '.join(data)} put back"
            elif r["action"] == "field":
                con.execute(f"update clients set {data['field']}=? where id=?", (data["old"], cid))
                what = f"{name}: {data['field']} put back"
            else:
                if data.get("old"):
                    con.execute("update client_accounts set login=?, password=? where client_id=? and service=?",
                                (data["old"]["login"], data["old"]["password"], cid, data["service"]))
                else:
                    con.execute("delete from client_accounts where client_id=? and service=?", (cid, data["service"]))
                what = f"{name}: {data['service']} account put back"
            con.execute("delete from client_log where id=?", (r["id"],))
            con.commit()
        finally:
            con.close()
        if r["action"] == "add":
            if isinstance(row, int):
                self._layout()
                last = col_letter(max(list(self.cols.values()) + list(self.step_cols.values())))
                note = self._sheet(lambda: self.api.clear(self.sid, [f"'{self.tab}'!A{row}:{last}{row}"]))
                what += f" (sheet row {row} cleared)" if note == row or note is None else f" ({note})"
        else:
            if r["action"] == "update":
                note = self._sheet(lambda: self._sheet_write_fields(cid, list(FIELDS) + ["account"]))
                self.set_steps_sheet(cid, {st["step_no"]: st["done"] for st in data["steps"]})
            elif r["action"] == "steps":
                note = self.set_steps_sheet(cid, {int(n): d for n, (d, _) in data.items()})
            else:
                note = self._sheet(lambda: self._sheet_write_fields(
                    cid, [data["field"]] if r["action"] == "field" else ["account"]))
            if isinstance(note, str):
                what += f" ({note})"
        return what

    def _sheet(self, fn):
        """Run a sheet write; the database already has the data, so an error only becomes a note."""
        if not self.api:
            return "no sheet configured: saved in linda.db only"
        try:
            return fn()
        except FinanceError as e:
            return f"sheet not updated: {e}"
        except Exception as e:
            return f"sheet not updated: {repr(e)[:150]}"

    # ---------------- import ----------------
    def import_sheet(self):
        """Copy clients that are in the sheet but not in linda.db (name, details, steps). Returns how many."""
        if not self.api:
            return 0
        self._layout()
        last = col_letter(max(list(self.cols.values()) + list(self.step_cols.values())))
        rows = self.api.get(self.sid, f"'{self.tab}'!A{self.hdr + 1}:{last}")
        con = self._con()
        added = 0
        try:
            have = {_norm(r["name"]) for r in con.execute("select name from clients")}
            for row in rows:
                cell = lambda f: str(row[self.cols[f]]).strip() if f in self.cols and self.cols[f] < len(row) else ""
                name = cell("name")
                if not name or "(example)" in name.lower() or _norm(name) in have:
                    continue
                info = cell("info")
                m_e, m_p = EMAIL.search(info), PHONE.search(info)
                now = time.time()
                cid = con.execute(
                    "insert into clients (name, phone, email, dob, certificate, course, city, visa_applied, visa_answer, "
                    "payment, comment, info_raw, created_ts, updated_ts) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (name, m_p.group(0).strip() if m_p else "", m_e.group(0) if m_e else "", _dob(info),
                     cell("certificate"), cell("course"), cell("city"), cell("visa_applied"), cell("visa_answer"),
                     cell("payment"), cell("comment"), info, now, now)).lastrowid
                for n, s in self.steps.items():
                    i = self.step_cols.get(n)
                    done = i is not None and i < len(row) and str(row[i]).strip().upper() == "TRUE"
                    con.execute("insert into client_steps values (?,?,?,?,?)", (cid, n, s, int(done), ""))
                have.add(_norm(name))
                added += 1
            con.commit()
        finally:
            con.close()
        return added


# ============================ Linda tools ============================================
_SVC = {}


def service(agent):
    """One ClientsService per process (created on first use; reads the clients section of config.yaml)."""
    if "svc" not in _SVC:
        from linda import tools
        from linda.channels.telegram.briefing import llm_text
        db = tools.ROOT / agent.cfg.get("db_path", "linda.db")
        svc = ClientsService.from_config(agent.cfg, db, llm=lambda s, u: llm_text(agent, s, u, temp=0))
        try:
            n = svc.import_sheet()
            if n:
                print(f"[clients] imported {n} clients from the sheet")
        except Exception as e:
            print("[clients] sheet import skipped:", repr(e)[:150])
        _SVC["svc"] = svc
    return _SVC["svc"]


def _tg(agent):
    """The Telegram bot, only while answering a Telegram message (voice gets a spoken line instead)."""
    brain = getattr(agent, "brain", None)
    in_tg = getattr(agent, "in_telegram", False) or getattr(brain, "extra_rules", "")
    return getattr(agent, "tg", None) if in_tg else None


def _send_card(agent, c, title_icon="👤", intro=""):
    from linda.channels.telegram.telegram import esc
    tg = _tg(agent)
    if not tg:
        return
    sections = [("Details", [esc(x) for x in c["info"]]), ("Accounts", [esc(x) for x in c["accounts"]]),
                (f"Progress {c['done']}/{c['total']}", [esc(x) for x in c["steps"]]),
                ("Visa, payment, notes", [esc(x) for x in c["extra"]])]
    tg.send_styled(c["name"], sections, icon=title_icon, intro=intro)
    agent.card_sent = True


def _note(res):
    return "" if isinstance(res, int) else f" ({res})"


def client_add(agent, a, text):
    """Save whatever the message holds. A known client (same email, passport or name) is updated, not duplicated."""
    svc = service(agent)
    try:
        p = svc.parse(str(a.get("details") or text))
        data = any(p.get(f) for f in ("phone", "email", "dob", "address", "passport")) or \
            any(x.get("login") for x in p["accounts"])
        if svc.match(p) is None and not data:              # 'X: Expatrio done' is an update, never a new client
            hits = svc.named_in(text)
            if len(hits) == 1:
                p["name"] = hits[0]["name"]
            else:
                return agent.say("error: no client matches " + (repr(p["name"]) if p["name"] else "this message")
                                 + ". To add a new client, send their details (name and email, phone or passport).")
        if not p["name"] and svc.match(p) is None:
            return agent.say("error: I couldn't tell whose details these are (no name, no known email or passport). "
                             "Add the client's name on the first line.")
        cid, created, changed, res = svc.upsert(p)
    except FinanceError as e:
        return agent.say(f"error: {e}")
    c = svc.card(cid)
    if created:
        where = "linda.db" + (f" and the clients sheet (row {res})" if isinstance(res, int) else f" ({res})")
        intro = f"New client: saved {c['name']} to {where}."
    elif changed:
        intro = f"{c['name']} is already a client, so I updated: {', '.join(changed)}{_note(res)}."
    else:
        intro = f"{c['name']} is already a client and already has all of this."
    _send_card(agent, c, "🆕" if created else "✏️", intro=intro + " Wrong? Say 'undo client'.")
    agent.say(intro + " The card was sent; reply with one short line.")


def client_update(agent, a, text):
    svc = service(agent)
    c, err = svc.find(str(a.get("name") or ""))
    if err:
        return agent.say(f"error: {err}")
    notes = []
    try:
        steps = [int(x) for x in re.findall(r"\d+", str(a.get("steps") or ""))]
        if steps:
            notes.append(_note(svc.set_steps(c["id"], steps, _bool(a.get("done"), True))))
        if a.get("field"):
            notes.append(_note(svc.set_field(c["id"], str(a["field"]), str(a.get("value") or ""))))
    except FinanceError as e:
        return agent.say(f"error: {e}")
    if not notes:
        return agent.say("error: say which step (number) or which field to change.")
    card = svc.card(c["id"])
    _send_card(agent, card, "✏️", intro="Updated. Wrong? Say 'undo client'.")
    agent.say(f"Updated {card['name']}{''.join(notes)}. Progress {card['done']}/{card['total']}. "
              "The card was sent; reply with one short line.")


def client_info(agent, a, text):
    svc = service(agent)
    c, err = svc.find(str(a.get("name") or ""))
    if err:
        return agent.say(f"error: {err}")
    card = svc.card(c["id"])
    _send_card(agent, card)
    if _tg(agent):
        return agent.say(f"The card of {card['name']} was sent to Telegram; reply with one short line.")
    nxt = next((s[2:] for s in card["steps"] if s.startswith("⬜")), "all steps done")
    agent.say(f"{card['name']}: {card['done']} of {card['total']} steps done. Next: {nxt}.")


def client_list(agent, a, text):
    from linda.channels.telegram.telegram import esc
    svc = service(agent)
    step = int(a["step"]) if str(a.get("step") or "").isdigit() else None
    if step is not None and step not in svc.steps:
        return agent.say(f"error: unknown step {step}. Steps: " + ", ".join(f"{n} {s}" for n, s in svc.steps.items()))
    done = _bool(a.get("done"), False)
    rows = svc.listing(step, done)
    title = "Clients" if step is None else f"Clients {'with' if done else 'without'} {step}. {svc.steps[step]}"
    lines = [f"• <b>{esc(r['name'])}</b> · {r['done']}/{r['total']}\n   next: {esc(r['next'])}" for r in rows]
    tg = _tg(agent)
    if tg:
        tg.send_styled(f"{title} ({len(rows)})", [("", lines or ["none"])], icon="👥")
        return agent.say(f"The list ({len(rows)} clients) was sent; reply with one short line.")
    agent.say(f"{title}: " + (", ".join(r["name"] for r in rows) or "none") + ".")


def client_undo(agent, a, text):
    try:
        agent.say(service(agent).undo_last() + ".")
    except FinanceError as e:
        agent.say(f"error: {e}")


# ============================ Telegram entry points (no LLM routing) ===================
NEW_CLIENT = re.compile(r"^\s*(new client|yangi (?:klient|mijoz)|новый клиент|klient|client)\b[\s:,-]*", re.I)
UNDO_CLIENT = re.compile(r"^\s*(undo client|client undo|klientni bekor|отмени клиента)\s*$", re.I)


def looks_like_client(text):
    """Pasted client details: several lines with an email or an Uzbek phone, plus a birth date or a full name."""
    if text.lstrip().startswith("/"):
        return False
    if PROTON.search(text) or PASSPORT.search(text):
        return True                       # a client's Proton mailbox or passport: always client data, even one line
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return False
    contact = bool(EMAIL.search(text) or re.search(r"\+?\s?998[\s\-]?\d{2}", text))
    name = any(re.fullmatch(r"[A-ZА-ЯЁʻ'][\w'ʻ-]+(?:\s+[A-ZА-ЯЁʻ'][\w'ʻ-]+){1,3}", l.strip()) for l in lines)
    return contact and (bool(DOB.search(text)) or name)


def _run(agent, tg, fn, a, text, reply=False, title=("Clients", "👥")):
    """Run a client tool for a Telegram message. Cards go out from the tool; errors (and, with reply=True,
    the tool's own text) are sent as a short message."""
    from linda.channels.telegram.briefing import muted
    agent.in_telegram = True
    try:
        with muted(agent, text) as mute:
            fn(agent, a, text)
        said = " ".join(mute.lines).strip()
    except Exception as e:                   # handled here: the message must not fall through and be saved twice
        print("[clients]", repr(e)[:200])
        said = f"error: something went wrong ({type(e).__name__}). Nothing else was changed; try again or say 'undo client'."
    finally:
        agent.in_telegram = False
    if said.startswith("error:"):
        tg.send_styled(title[0], icon=title[1], intro=said[6:].strip())
    elif reply and said:
        tg.send_styled("Clients", icon="↩️", intro=said)


ASK_FIELD = [("phone", r"phone|telefon|raqam|nomer|номер|телефон"), ("email", r"e-?mail|pochta|почт|gmail"),
             ("address", r"address|manzil|адрес|yashash"), ("dob", r"birth|tug'?il|дата рожд|dob|yosh"),
             ("accounts", r"password|parol|пароль|login|логин|akkaunt|account"),
             ("progress", r"progress|holat|status|step|qadam|bosqich|этап|tls|expatrio|viza|visa|videx|cv"),
             ("info", r"\binfo|ma'?lumot|информац|details|haqida|кто|who|show|ko'?rsat|all\b|full")]
QUESTION = re.compile(r"\?|^\s*(what|who|show|give|tell|how|qanday|qaysi|nima|kim|ber|ko'?rsat|что|кто|какой|дай|покажи)\b", re.I)
EXPATRIO = r"(?:expa\w{2,3}io|ekspatrio|експатрио)"          # expatrio, expartio, expatrioo ...
DONE = re.compile(r"\b(done|completed?|finished|did|ready|bajarildi|bajardik|bajardim|qildik|qildim|qilindi|tugadi|"
                  r"tugatdik|o'?tdi|o'?tkazdik|ochdik|ochildi|tayyor|yubordik|topshirdik|готово?|сделали?|завершил\w*|"
                  r"прошл\w*|открыли)\b", re.I)
UNDONE = re.compile(r"\b(not|undo|bekor|emas|qilinmadi|не)\b", re.I)
SAVING = re.compile(r"\b(add|save|saqla|set|change|o'zgartir)\b|^\s*\S+\s*:\s*\S", re.I)
# step words -> step number (default step names; 'TLS' or 'Expatrio' alone is ambiguous and goes to the AI)
STEP_WORDS = [(1, r"konsult|consult|konsalt|консульт"), (3, r"proton"),
              (4, r"tls\s*(akk|acc|akaunt|hisob|kabinet)"), (5, r"termin|appointment|tls\s*(sana|vaqt|date)"),
              (6, r"til markaz|language (cent|school)|kurs to'?lov"), (7, EXPATRIO + r"\s*(hisob|acc|akk|ochish|ochdik|ochildi)"),
              (8, EXPATRIO + r"\s*(hujjat|doc)"), (9, r"\bcv\b|motivat|rezyume|resume|мотивац"),
              (10, r"tarjima|translat|перевод"), (11, r"videx"), (12, r"viza kutil|visa (wait|pending)|viza topshir")]


def _steps_in(text):
    return sorted({n for n, rx in STEP_WORDS if re.search(rx, text, re.I)})


def _mark_steps(agent, tg, t):
    """'For Jasur we already completed the konsultatsiya, proton mail' -> steps 1 and 3 done, no AI needed."""
    if not DONE.search(t) or UNDONE.search(t):
        return False
    steps = _steps_in(t)
    if not steps:
        return False
    svc = service(agent)
    hits = svc.named_in(t)
    if len(hits) != 1:
        return False                      # no client or several: the AI asks
    try:
        res = svc.set_steps(hits[0]["id"], steps, True)
    except FinanceError as e:
        tg.send_styled("Clients", icon="👥", intro=str(e))
        return True
    c = svc.card(hits[0]["id"])
    names = ", ".join(f"{n}. {svc.steps.get(n, '')}" for n in steps)
    print(f"[route] client steps done -> {steps}")
    _send_card(agent, c, "✅", intro=f"Marked done for {c['name']}: {names}{_note(res)}. Wrong? Say 'undo client'.")
    return True


def _answer_question(agent, tg, t):
    """'Jasurning telefoni?', 'Timur info', 'email of Aziz': the full card, with the asked field on top."""
    svc = service(agent)
    asked = [f for f, rx in ASK_FIELD if re.search(rx, t, re.I)]
    if not asked or ((SAVING.search(t) or DONE.search(t)) and not QUESTION.search(t)):
        return False
    hits = svc.named_in(t)
    if not hits:
        return False
    if len(hits) > 1:
        tg.send_styled("Clients", icon="👥", intro="Which one: " + "; ".join(h["name"] for h in hits) + "?")
        return True
    c = svc.card(hits[0]["id"])
    con = svc._con()
    try:
        row = dict(con.execute("select * from clients where id=?", (hits[0]["id"],)).fetchone())
    finally:
        con.close()
    top = []
    for f in asked:
        if f in ("phone", "email", "address", "dob"):
            top.append(f"{f}: {row[f] or 'not saved (see details below)'}")
        elif f == "accounts":
            top.append("accounts: " + ("; ".join(c["accounts"]) or "none saved"))
    print(f"[route] client question -> card ({', '.join(asked)})")
    _send_card(agent, c, "👤", intro=" · ".join(top))
    return True


def telegram_text(agent, tg, text):
    """True when the message was a client message and was handled here."""
    t = text.strip()
    if UNDO_CLIENT.match(t):
        _run(agent, tg, client_undo, {}, t, reply=True)
        return True
    m = NEW_CLIENT.match(t)
    if (m and len(t) > m.end() + 3 and ("\n" in t or EMAIL.search(t) or PHONE.search(t))) or looks_like_client(t):
        details = t[m.end():] if m else t
        print("[route] client details -> client_add")
        _run(agent, tg, client_add, {"details": details}, details)
        return True
    if len(t) < 300:
        agent.in_telegram = True
        try:
            return _mark_steps(agent, tg, t) or _answer_question(agent, tg, t)
        finally:
            agent.in_telegram = False
    return False


def telegram_command(agent, tg, cmd, rest):
    """/client <details> adds, /client <name> shows one, /client alone or /clients lists."""
    if cmd == "clients" or (cmd == "client" and not rest):
        _run(agent, tg, client_list, {}, rest)
        if cmd == "client":
            tg.send_styled("Clients", icon="👥", intro="Add one: /client followed by the details (name, phone, email, "
                           "date of birth, address, certificate, course, city). Show one: /client <name>.")
        return
    if cmd == "client_undo":
        return _run(agent, tg, client_undo, {}, rest, reply=True)
    if "\n" in rest or EMAIL.search(rest) or PHONE.search(rest) or DOB.search(rest):
        return _run(agent, tg, client_add, {"details": rest}, rest)
    return _run(agent, tg, client_info, {"name": rest}, rest)
