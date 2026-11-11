"""ONE central SQLite database for every module: birthdays, notes, songs, practice logs, audio analyses,
chess games, quiz answers, driving results, tasks, daily reviews, document chunks (semantic search)."""
import collections, datetime, json, pathlib, sqlite3, threading, time
import yaml

Bday = collections.namedtuple("Bday", "n name date age relation lang")
BRow = collections.namedtuple("BRow", "name day month year relation lang")
Song = collections.namedtuple("Song", "id title artist instrument status priority target_bpm melody notes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS birthdays(id INTEGER PRIMARY KEY, name TEXT COLLATE NOCASE UNIQUE,
    day INT, month INT, year INT, relation TEXT, lang TEXT);
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, text TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS songs(id INTEGER PRIMARY KEY, title TEXT COLLATE NOCASE, artist TEXT,
    instrument TEXT COLLATE NOCASE, status TEXT, priority INT, target_bpm REAL, melody TEXT, notes TEXT,
    added REAL, UNIQUE(title, instrument));
CREATE TABLE IF NOT EXISTS practice_logs(id INTEGER PRIMARY KEY, ts REAL, day TEXT, song_id INT,
    instrument TEXT, minutes INT, focus TEXT, rating INT);
CREATE TABLE IF NOT EXISTS analyses(id INTEGER PRIMARY KEY, ts REAL, song_id INT, instrument TEXT, metrics TEXT);
CREATE TABLE IF NOT EXISTS chess_games(id INTEGER PRIMARY KEY, ts REAL, user_white INT, skill INT,
    result TEXT, plies INT, pgn TEXT);
CREATE TABLE IF NOT EXISTS quiz_answers(id INTEGER PRIMARY KEY, ts REAL, bank TEXT, qhash TEXT, correct INT);
CREATE TABLE IF NOT EXISTS driving_tests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, score INT, total INT, notes TEXT);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, title TEXT, due TEXT, priority INT, project TEXT,
    status TEXT DEFAULT 'open', created REAL, done_ts REAL);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, day TEXT, text TEXT, summary TEXT, tomorrow TEXT);
CREATE TABLE IF NOT EXISTS doc_files(path TEXT PRIMARY KEY, kind TEXT, mtime REAL, size INT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, kind TEXT, idx INT, text TEXT, emb BLOB);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, tool TEXT NOT NULL,
    arguments TEXT NOT NULL, result TEXT NOT NULL, confirmed_by TEXT
);
"""
SONG_COLS = "id,title,artist,instrument,status,priority,target_bpm,melody,notes"


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)
            for col in ("relation", "lang"):             # migrate the v1 birthdays table
                try:
                    self.db.execute(f"ALTER TABLE birthdays ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            self.db.commit()

    # ---- low level (thread-safe) ----
    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def xmany(self, sql, rows):
        with self.lock:
            self.db.executemany(sql, rows)
            self.db.commit()

    # ---- kv / notes ----
    def get(self, k):
        r = self.q("SELECT v FROM kv WHERE k = ?", (k,))
        return r[0][0] if r else None

    def set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))

    def audit_action(self, tool, arguments, result, confirmed_by=None):
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        self.x("INSERT INTO audit_log(timestamp,tool,arguments,result,confirmed_by) VALUES (?,?,?,?,?)",
               (timestamp, tool, payload, result, confirmed_by))

    def add_note(self, text):
        self.x("INSERT INTO notes(text, ts) VALUES (?,?)", (text, time.time()))

    def notes(self, limit=50):
        return [r[0] for r in self.q("SELECT text FROM notes ORDER BY id DESC LIMIT ?", (limit,))]

    # ---- birthdays ----
    def add_birthday(self, name, day, month, year=None, relation="", lang=""):
        name = name.strip()
        old = self.q("SELECT year, relation, lang FROM birthdays WHERE name = ?", (name,))
        if old:
            year = year or old[0][0]
            relation = relation.strip() or old[0][1] or ""
            lang = lang or old[0][2] or ""
        self.x("INSERT OR REPLACE INTO birthdays(name,day,month,year,relation,lang) VALUES (?,?,?,?,?,?)",
            (name, day, month, year or None, relation.strip(), lang))

    
    def delete_birthday(self, name):
        return self.x("DELETE FROM birthdays WHERE name = ?", (name.strip(),)).rowcount > 0

    def find_birthday(self, name):
        name = name.strip()
        if not name:
            return None
        for pat in (name, name[:max(3, len(name) - 2)] + "%"):
            r = self.q("SELECT name,day,month,year,relation,lang FROM birthdays WHERE name LIKE ? LIMIT 1", (pat,))
            if r:
                r = r[0]
                return BRow(r[0], r[1], r[2], r[3], r[4] or "", r[5] or "")
        return None

    def upcoming(self, days=60, today=None):
        today, out = today or datetime.date.today(), []
        for name, d, m, y, rel, lang in self.q("SELECT name, day, month, year, relation, lang FROM birthdays"):
            try:
                nxt = datetime.date(today.year, m, d)
                if nxt < today:
                    nxt = datetime.date(today.year + 1, m, d)
            except ValueError:
                continue
            n = (nxt - today).days
            if n <= days:
                out.append(Bday(n, name, nxt, nxt.year - y if y else None, rel or "", lang or ""))
        return sorted(out)

    # ---- songs & practice ----
    def upsert_song(self, title, instrument, artist="", status="learning", priority=2,
                    target_bpm=None, melody="", notes=""):
        self.x("""INSERT INTO songs(title,instrument,artist,status,priority,target_bpm,melody,notes,added)
                  VALUES (?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(title,instrument) DO UPDATE SET artist=excluded.artist, status=excluded.status,
                  priority=excluded.priority, target_bpm=excluded.target_bpm, melody=excluded.melody,
                  notes=excluded.notes""",
               (title.strip(), instrument.lower().strip(), artist, status, int(priority), target_bpm,
                melody or "", notes or "", time.time()))

    def import_songs(self, path):
        p = pathlib.Path(path)
        if not p.exists():
            return 0
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        songs = data.get("songs") or []
        for s in songs:
            self.upsert_song(s["title"], s.get("instrument", "piano"), s.get("artist", ""),
                             s.get("status", "learning"), s.get("priority", 2), s.get("target_bpm"),
                             s.get("melody", ""), s.get("notes", ""))
        return len(songs)

    def songs(self, instrument=None):
        sql = f"SELECT {SONG_COLS} FROM songs WHERE status != 'archived'"
        args = ()
        if instrument:
            sql, args = sql + " AND instrument = ?", (instrument.lower(),)
        return [Song(*r) for r in self.q(sql, args)]

    def find_song(self, title, instrument=""):
        title = title.strip()
        if not title:
            return None
        for pat in (title, f"%{title}%"):
            sql, args = f"SELECT {SONG_COLS} FROM songs WHERE title LIKE ?", [pat]
            if instrument:
                sql, args = sql + " AND instrument = ?", args + [instrument.lower()]
            r = self.q(sql + " LIMIT 1", args)
            if r:
                return Song(*r[0])
        return None

    def log_practice(self, minutes, song_id=None, instrument="", focus="", rating=None, day=None):
        self.x("INSERT INTO practice_logs(ts,day,song_id,instrument,minutes,focus,rating) VALUES (?,?,?,?,?,?,?)",
               (time.time(), day or str(datetime.date.today()), song_id, instrument, int(minutes), focus, rating))

    def minutes_on(self, day):
        return self.q("SELECT COALESCE(SUM(minutes),0) FROM practice_logs WHERE day = ?", (str(day),))[0][0]

    def practice_summary(self, days=7):
        since = str(datetime.date.today() - datetime.timedelta(days=days - 1))
        by_instr = dict(self.q("SELECT instrument, SUM(minutes) FROM practice_logs WHERE day >= ? GROUP BY instrument", (since,)))
        by_song = dict(self.q("""SELECT s.title, SUM(p.minutes) FROM practice_logs p JOIN songs s ON s.id = p.song_id
                                 WHERE p.day >= ? GROUP BY s.title""", (since,)))
        return {"days": days, "total_minutes": sum(by_instr.values()), "by_instrument": by_instr, "by_song": by_song}

    def last_practiced(self, song_id):
        r = self.q("SELECT MAX(day) FROM practice_logs WHERE song_id = ?", (song_id,))[0][0]
        return datetime.date.fromisoformat(r) if r else None

    def last_rating(self, song_id):
        r = self.q("SELECT rating FROM practice_logs WHERE song_id = ? AND rating IS NOT NULL ORDER BY id DESC LIMIT 1", (song_id,))
        return r[0][0] if r else None

    def save_analysis(self, song_id, instrument, metrics):
        self.x("INSERT INTO analyses(ts,song_id,instrument,metrics) VALUES (?,?,?,?)",
               (time.time(), song_id, instrument, json.dumps(metrics)))

    def last_analysis(self, song_id):
        r = self.q("SELECT metrics FROM analyses WHERE song_id = ? ORDER BY id DESC LIMIT 1", (song_id,))
        return json.loads(r[0][0]) if r else None

    # ---- chess ----
    def log_chess(self, user_white, skill, result, plies, pgn):
        self.x("INSERT INTO chess_games(ts,user_white,skill,result,plies,pgn) VALUES (?,?,?,?,?,?)",
               (time.time(), int(user_white), skill, result, plies, pgn))

    def chess_stats(self):
        res = dict(self.q("SELECT result, COUNT(*) FROM chess_games GROUP BY result"))
        last = self.q("SELECT MAX(ts) FROM chess_games")[0][0]
        return {"wins": res.get("win", 0), "losses": res.get("loss", 0), "draws": res.get("draw", 0),
                "abandoned": res.get("abandoned", 0),
                "last_game": datetime.date.fromtimestamp(last) if last else None}

    # ---- quiz / driving ----
    def log_quiz(self, bank, qhash, correct):
        self.x("INSERT INTO quiz_answers(ts,bank,qhash,correct) VALUES (?,?,?,?)", (time.time(), bank, qhash, int(correct)))

    def quiz_last_all(self):
        return self.q("SELECT qhash, correct FROM quiz_answers WHERE id IN (SELECT MAX(id) FROM quiz_answers GROUP BY qhash)")

    def quiz_bank_stats(self):
        return {b: (n, c or 0) for b, n, c in self.q("SELECT bank, COUNT(*), SUM(correct) FROM quiz_answers GROUP BY bank")}

    def last_quiz_day(self):
        r = self.q("SELECT MAX(ts) FROM quiz_answers")[0][0]
        return datetime.date.fromtimestamp(r) if r else None

    def log_driving(self, kind, score, total, notes=""):
        self.x("INSERT INTO driving_tests(ts,kind,score,total,notes) VALUES (?,?,?,?,?)",
               (time.time(), kind, score, total, notes))

    def driving_recent(self, n=5):
        return [{"date": str(datetime.date.fromtimestamp(ts)), "kind": k, "score": s, "total": t, "notes": nt}
                for ts, k, s, t, nt in self.q("SELECT ts,kind,score,total,notes FROM driving_tests ORDER BY id DESC LIMIT ?", (n,))]

    # ---- tasks ----
    def add_task(self, title, due=None, priority=2, project=""):
        self.x("INSERT INTO tasks(title,due,priority,project,created) VALUES (?,?,?,?,?)",
               (title.strip(), str(due) if due else None, priority, project, time.time()))

    def open_tasks(self):
        return self.q("SELECT id,title,due,priority,project FROM tasks WHERE status='open' "
                      "ORDER BY due IS NULL, due, priority DESC")

    def tasks_due(self, by_day):
        return [r for r in self.open_tasks() if r[2] and r[2] <= str(by_day)]

    def complete_task(self, fragment):
        fragment = fragment.strip()
        r = self.q("SELECT id,title FROM tasks WHERE status='open' AND title LIKE ? ORDER BY id DESC LIMIT 1", (f"%{fragment}%",))
        if not r:
            return None
        self.x("UPDATE tasks SET status='done', done_ts=? WHERE id=?", (time.time(), r[0][0]))
        return r[0][1]

    # ---- reviews ----
    def save_review(self, day, text, summary="", tomorrow=""):
        self.x("INSERT INTO reviews(day,text,summary,tomorrow) VALUES (?,?,?,?)", (str(day), text, summary, tomorrow))

    def recent_reviews(self, n=3):
        return [{"day": d, "summary": s or t, "tomorrow": tm}
                for d, t, s, tm in self.q("SELECT day,text,summary,tomorrow FROM reviews ORDER BY id DESC LIMIT ?", (n,))]
