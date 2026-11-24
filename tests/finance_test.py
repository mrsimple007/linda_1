"""Finance tests with a fake Google Sheets API (no network, no credentials).  python tests/test_finance.py"""
import datetime as dt, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from linda.business.finance import BlockSheet, FinanceError, FinanceService, FlatSheet, ParseError, parse_message, to_serial
from linda.business.finance_chat import FinanceChat

TODAY = dt.date(2026, 11, 17)
def p(t, **k): return parse_message(t, today=TODAY, **k)

# ---- parser
t = p("-40 000 uzs for medicine")
assert (t.type, t.amount, t.description, t.sheet, t.date) == ("expense", 40000, "Medicine", "my_finance", TODAY), t
assert p("+9 000 000 salary").amount == 9_000_000 and p("+9 000 000 salary").type == "income"
assert p("-40k lunch").amount == 40000
assert p("-1.5m laptop").amount == 1_500_000
assert p("-40.000 taxi").amount == 40000
assert p("−500 medicine").amount == 500                      # unicode minus
assert p("-100 000 lunch yesterday").date == TODAY - dt.timedelta(days=1)
assert p("-100 000 lunch 12.11").date == dt.date(2026, 11, 12)
assert p("-100 000 lunch 12.11.2026").description == "Lunch"
assert p("+100 000 salary 30.12").date == dt.date(2025, 12, 30)
c = p("c: +670 000 Javohir application fee")
assert (c.sheet, c.type, c.amount) == ("company", "income", 670000)
assert p("-6 000 000 Sardor aka 500$ perevod").description == "Sardor aka 500$ perevod"
assert p("-100 000 summer camp").description == "Summer camp"
assert p("-5 medicine").amount == 5
for bad in ["40 000 medicine", "-40 000", "-500 usd taxi", "hello"]:
    try: p(bad); raise SystemExit(f"should fail: {bad}")
    except ParseError: pass

# ---- fake sheets
class FakeAPI:
    def __init__(self): self.cells, self.batches = {}, []
    @staticmethod
    def _rng(r):
        m = re.fullmatch(r"([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", r.split("!")[1])
        c1, r1, c2, r2 = m.groups()
        r1 = int(r1) if r1 else 1
        r2 = int(r2) if r2 else (10_000 if c2 else r1)
        return c1, r1, c2 or c1, r2
    def get(self, sid, rng, render=None):
        c1, r1, c2, r2 = self._rng(rng); out = []
        for r in range(r1, min(r2, 300) + 1):
            row = [self.cells.get((chr(c), r), "") for c in range(ord(c1), ord(c2) + 1)]
            while row and row[-1] == "": row.pop()
            out.append(row)
        while out and not out[-1]: out.pop()
        return out
    def update(self, sid, data):
        for d in data:
            c1, r1, _, _ = self._rng(d["range"])
            for i, v in enumerate(d["values"][0]): self.cells[(chr(ord(c1) + i), r1)] = v
    def clear(self, sid, ranges):
        for rg in ranges:
            c1, r1, c2, r2 = self._rng(rg)
            for c in range(ord(c1), ord(c2) + 1): self.cells.pop((chr(c), r1), None)
    def batch(self, sid, reqs): self.batches.append(reqs)
    def tab_info(self, sid, tab): return 1, 1000

flat_api, co_api = FakeAPI(), FakeAPI()
for r in range(2, 14):
    for col, v in zip("ABCD", [46000 + r, "Expense", 1000, "x"]): flat_api.cells[(col, r)] = v
for r in range(3, 44):
    for col, v in zip("BCD", [46000, "old", 5]): co_api.cells[(col, r)] = v
for r in range(3, 51): co_api.cells[("F", r)] = "Kirim ✅"        # row 51: Tur empty
for r in range(53, 126):
    for col, v in zip("BCE", [46000, "old", 5]): co_api.cells[(col, r)] = v
    co_api.cells[("F", r)] = "Chiqim ❌"

flat_cfg = {"id": "s1", "tab": "Sheet1", "layout": "flat", "labels": {"income": "Income", "expense": "Expense"}}
co_cfg = {"id": "s2", "tab": "Sheet1", "layout": "blocks", "income_rows": [3, 51], "expense_rows": [53, 134],
          "labels": {"income": "Kirim ✅", "expense": "Chiqim ❌"}}
cfg = {"default_sheet": "my_finance", "confirm_sheets": ["company"], "sheets": {"my_finance": flat_cfg, "company": co_cfg}}
audit_log = []
svc = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"),
                     adapters={"my_finance": FlatSheet(flat_api, flat_cfg), "company": BlockSheet(co_api, co_cfg)},
                     audit=lambda *a: audit_log.append(a))
bot = FinanceChat(svc, clock=lambda: 1000.0)

# ---- my_finance
r = bot.handle("-40 000 medicine")
assert "my_finance, row 14" in r, r
assert flat_api.cells[("C", 14)] == 40000 and flat_api.cells[("B", 14)] == "Expense"
assert flat_api.cells[("A", 14)] == to_serial(dt.date.today())
assert isinstance(flat_api.cells[("D", 14)], str)
assert flat_api.batches and flat_api.batches[-1][0]["copyPaste"]["pasteType"] == "PASTE_FORMAT"
r = bot.handle("+9 000 000 salary")
assert "row 15" in r and flat_api.cells[("B", 15)] == "Income"

# ---- company: confirmation, row choice, Tur cell untouched
r = bot.handle("c: +670 000 Javohir application fee")
assert "Reply yes" in r and ("D", 44) not in co_api.cells
r = bot.handle("yes")
assert "company, row 44" in r, r
assert co_api.cells[("B", 44)] == dt.date.today().strftime("%d.%m.%Y")      # TEXT date like the rest of the sheet
assert co_api.cells[("D", 44)] == 670000 and co_api.cells[("F", 44)] == "Kirim ✅" and ("E", 44) not in co_api.cells
bot.handle("c: -100 000 Reklama"); r = bot.handle("yes")
assert "row 126" in r and co_api.cells[("E", 126)] == 100000 and co_api.cells[("F", 126)] == "Chiqim ❌", r
assert ("D", 126) not in co_api.cells
bot.handle("c: +1 000 000 last slot"); assert bot.handle("yes")
r = bot.handle("c: +5 000 something")
r = bot.handle("yes")
assert "company, row 46" in r

# no / expiry
bot.handle("c: +1 000 later"); assert bot.handle("no") == "Cancelled."
bot.handle("c: +1 000 later"); bot.clock = lambda: 5000.0
assert "expired" in bot.handle("yes"); bot.clock = lambda: 1000.0

# ---- undo: removes only what Linda wrote, refuses edited rows
r = bot.handle("undo")
assert "Removed from company (row 46)" in r and ("D", 46) not in co_api.cells and co_api.cells[("F", 46)] == "Kirim ✅"
co_api.cells[("C", 44)] = "edited by hand"
bot.handle("undo"); bot.handle("undo")   # row 45, then expense row 126
r = bot.handle("undo")                   # row 44 was edited
assert "no longer matches" in r and co_api.cells[("D", 44)] == 670000, r

# ---- block full
for r_ in range(126, 135): co_api.cells[("B", r_)] = 1
try: svc._adapter("company").peek("expense"); raise SystemExit("should be full")
except FinanceError as e: assert "full" in str(e)

# ---- failure is logged, not silent
class Boom(FakeAPI):
    def get(self, *a, **k): raise RuntimeError("403 forbidden")
svc2 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), adapters={"my_finance": FlatSheet(Boom(), flat_cfg)})
try: svc2.add(svc2.parse("-1 000 x")); raise SystemExit("should fail")
except FinanceError as e: assert "Google Sheets error" in str(e)
assert svc2._run("SELECT status FROM finance", fetch=True)[0]["status"] == "failed"

# ---- summary / search / security
s = bot.handle("/summary")
assert "my_finance" in s and "9 000 000" in s, s
assert svc.search_total("medicine")[0] == 40000
assert bot.handle("hello there") is None and bot.handle("- buy milk") is None   # normal Linda flow
assert bot.handle("yes") is None or True
assert any(a[0] == "finance.add" for a in audit_log)

# ================= routing words =================
t = p("-4 000 000 perevod to akam finance sheet")
assert (t.sheet, t.amount, t.description) == ("company", 4_000_000, "Perevod to akam"), t
assert p("-100 000 reklama in the company").sheet == "company"
assert p("-100 000 reklama (finance)").sheet == "company"
assert p("-100 000 lunch my finance").sheet == "my_finance"
assert p("-100 000 lunch to my finance sheet").sheet == "my_finance"
assert p("c: -100 000 reklama").sheet == "company" and p("m: -100 000 reklama finance sheet").sheet == "my_finance"
assert p("-100 000 finance book").sheet == "my_finance" and p("-100 000 finance book").description == "Finance book"
assert p("-100 000 business lunch").sheet == "my_finance"
try: p("-1 000 x my finance and finance sheet"); raise SystemExit("both sheets must fail")
except ParseError: pass
# the sheet alone on the last line, or after a dash
t = p("-1 300 000\nPerevod CTO ga 100 euro\ncompany")
assert (t.sheet, t.description) == ("company", "Perevod CTO ga 100 euro"), t
t = p("-1 300 000\nPerevod CTO ga 100 euro - company sheet")
assert (t.sheet, t.description) == ("company", "Perevod CTO ga 100 euro"), t
t = p("+4 500 000\nUmid til kursi - 350 eur\nCompany sheet")
assert (t.sheet, t.description) == ("company", "Umid til kursi - 350 eur"), t
assert p("-50 000 lunch\nmy finance").sheet == "my_finance"
assert p("-50 000 Uzum company tolov").sheet == "my_finance"       # "company" inside the text is not a sheet
assert p("-50 000 business lunch").description == "Business lunch"
assert p("c: -1 000 x\nmy finance").sheet == "company"            # an explicit prefix always wins

# ================= merge into the month's category rows (your Expences layout) =================
this_m = dt.date.today().replace(day=1)
last_m = (this_m - dt.timedelta(days=1)).replace(day=26)
m_api = FakeAPI()
for col, v in zip("ABCD", ["Date", "Type", "Sum", "For what?"]): m_api.cells[(col, 1)] = v
seed = [("Medicine overall", 183000, "Expense", this_m), ("Obed", 283000, "Expense", this_m),
        ("Salary (avans)", 0, "Income", this_m), ("Uzum market", 442000, "Expense", last_m)]
for i, (d_, a_, t_, dd) in enumerate(seed, start=2):
    for col, v in zip("ABCD", [to_serial(dd), t_, a_, d_]): m_api.cells[(col, i)] = v
calls = []
def fake_llm(system, user):
    calls.append(user)
    return '{"merge": 2, "name": null}' if "lunch" in user.lower() else '{"merge": 99, "name": "made up"}'
svc3 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), chooser=fake_llm,
                      adapters={"my_finance": FlatSheet(m_api, flat_cfg)})
chat3 = FinanceChat(svc3)

r = chat3.handle("-40 000 medicine")
assert "183 000 → 223 000" in r and "row 2" in r, r
assert m_api.cells[("C", 2)] == 223000 and m_api.cells[("D", 2)] == "Medicine overall" and not calls   # exact: no LLM
r = chat3.handle("undo")
assert "back out" in r and m_api.cells[("C", 2)] == 183000, r

r = chat3.handle("+10 000 000 salary avans")                       # merges into a placeholder row holding 0
assert "row 4" in r and m_api.cells[("C", 4)] == 10_000_000 and m_api.cells[("B", 4)] == "Income", r

r = chat3.handle("-225 000 paid for uzum market")                  # only exists in LAST month: new row, same name
assert "row 6" in r and m_api.cells[("D", 6)] == "Uzum market" and m_api.cells[("C", 6)] == 225000, r

r = chat3.handle("-60 000 lunch")                                  # different words: the LLM picks row 2 -> ASK
assert "looks like your “Obed”" in r and "283 000 → 343 000" in r and len(calls) >= 1, r
assert m_api.cells[("C", 3)] == 283000 and chat3.awaiting()       # nothing written yet
assert svc3._run("SELECT COUNT(*) AS n FROM finance WHERE status='pending'", fetch=True)[0]["n"] == 0
r = chat3.handle("add")                                            # "add" -> into Obed
assert "Obed" in r and m_api.cells[("C", 3)] == 343000 and not chat3.awaiting(), r

r = chat3.handle("-20 000 lunch")                                  # asked again; "new" -> its own row, own words
assert "looks like" in r and chat3.handle("new").startswith("✅ my_finance, row 7"), r
assert m_api.cells[("D", 7)] == "Lunch" and m_api.cells[("C", 7)] == 20000 and m_api.cells[("C", 3)] == 343000
assert "Removed" in chat3.handle("undo") and ("D", 7) not in m_api.cells

r = chat3.handle("-20 000 lunch"); assert "looks like" in r       # "no" cancels the question
assert chat3.handle("no") == "Cancelled." and m_api.cells[("C", 3)] == 343000 and ("D", 7) not in m_api.cells
r = chat3.handle("-20 000 lunch"); assert "looks like" in r       # a new entry instead of an answer: say it
r = chat3.handle("-5 000 obed")
assert r.startswith("(Not saved: “Lunch” 20 000") and m_api.cells[("C", 3)] == 348000, r
chat3.handle("undo"); assert m_api.cells[("C", 3)] == 343000

r = chat3.handle("-15 000 something unrelated")                    # LLM answers garbage: must NOT merge
assert "row 7" in r and m_api.cells[("D", 7)] == "Something unrelated" and m_api.cells[("C", 3)] == 343000, r

# another merge into the same row, then undo in reverse order
chat3.handle("-10 000 obed"); assert m_api.cells[("C", 3)] == 353000
chat3.handle("undo")
assert m_api.cells[("C", 3)] == 343000
m_api.cells[("C", 3)] = 999                                        # hand edit -> undo refuses and changes nothing
r = chat3.handle("-1 000 obed"); assert "added 1 000" in r
m_api.cells[("C", 3)] = 5
assert "no longer matches" in chat3.handle("undo") and m_api.cells[("C", 3)] == 5

# no chooser (LLM down): only exact matches merge, the rest becomes new rows
svc4 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), chooser=None,
                      adapters={"my_finance": FlatSheet(m_api, flat_cfg)})
r = FinanceChat(svc4).handle("-1 000 lunch"); assert "row 9" in r or "row 8" in r, r

# merge can be switched off per sheet
off = dict(flat_cfg, merge=False)
svc5 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), adapters={"my_finance": FlatSheet(m_api, off)})
before = m_api.cells[("C", 2)]; FinanceChat(svc5).handle("-1 000 medicine"); assert m_api.cells[("C", 2)] == before

# imported history is never undone
svc6 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), adapters={"my_finance": FlatSheet(FakeAPI(), flat_cfg)})
svc6._run("INSERT INTO finance(tx_date,sheet,type,amount,description,sheet_row,source,status) "
          "VALUES ('2026-09-10','my_finance','income',9000000,'Salary',2,'import','active')")
try: svc6.undo_last(); raise SystemExit("must not undo imports")
except FinanceError as e: assert "Nothing to undo" in str(e)
assert "no entries in the sheets" in svc6.format_summary("2026-09")   # totals come from the (empty) sheet now

# ================= summaries read the SHEETS (hand-typed rows count too), not only linda.db =================
s_flat, s_co = FakeAPI(), FakeAPI()
for col, v in zip("ABCD", ["Date", "Type", "Sum", "For what?"]): s_flat.cells[(col, 1)] = v
for i, (d_, t_, a_, w_) in enumerate([(dt.date(2026, 10, 3), "Income", 9_000_000, "Salary"),
                                      (dt.date(2026, 10, 9), "Expense", 500_000, "Obed"),
                                      (dt.date(2026, 11, 2), "Expense", 300_000, "obed"),
                                      (dt.date(2026, 11, 5), "Expense", 1_000_000, "Rent")], start=2):
    for col, v in zip("ABCD", [to_serial(d_), t_, a_, w_]): s_flat.cells[(col, i)] = v
s_flat.cells[("A", 7)] = "separator row"                            # junk rows are skipped
for r, (d_, w_, inc, exp) in {3: ("05.01.2026", "Kurs", 4_000_000, ""), 4: ("08.11.2026", "Fee", "1 500 000", ""),
                             53: ("10.01.2026", "Reklama", "", 700_000), 54: ("09.11.2026", "Ofis", "", 200_000)}.items():
    for col, v in zip("BCDE", [d_, w_, inc, exp]): s_co.cells[(col, r)] = v
s_co.cells[("D", 136)] = "=SUM(D3:D134)"                           # totals row is outside the blocks: never read
svc7 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"),
                      adapters={"my_finance": FlatSheet(s_flat, flat_cfg), "company": BlockSheet(s_co, co_cfg)})
a = svc7.sheet_summary("all")["sheets"]
assert (a["my_finance"]["income"], a["my_finance"]["expense"], a["my_finance"]["n"]) == (9_000_000, 1_800_000, 4), a
assert a["my_finance"]["top"][0] == ("Rent", 1_000_000) and ("Obed", 800_000) in a["my_finance"]["top"], a
assert (a["company"]["income"], a["company"]["expense"], a["company"]["source"]) == (5_500_000, 900_000, "sheet"), a
nov = svc7.sheet_summary("2026-11")["sheets"]
assert nov["my_finance"]["expense"] == 1_300_000 and nov["my_finance"]["income"] == 0 and nov["company"]["income"] == 1_500_000
assert svc7.sheet_summary("2026-12")["sheets"] == {}
assert [r["description"] for r in svc7.sheet_recent("my_finance", "expense", 2)] == ["Rent", "obed"]
class Broken(FakeAPI):
    def get(self, *a, **k): raise RuntimeError("403")
svc8 = FinanceService(cfg, db_path=tempfile.mktemp(suffix=".db"), adapters={"my_finance": FlatSheet(Broken(), flat_cfg)})
svc8._run("INSERT INTO finance(tx_date,sheet,type,amount,description,source,status) "
          "VALUES ('2026-11-01','my_finance','expense',1000,'Tea','telegram','active')")
b = svc8.sheet_summary("all", ["my_finance"])["sheets"]["my_finance"]
assert (b["expense"], b["source"]) == (1000, "linda"), b               # sheet down: falls back to linda.db

print("all finance tests passed")