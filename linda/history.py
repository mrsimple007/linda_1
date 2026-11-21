"""Conversation memory: every voice and Telegram turn is kept in linda.db (table `history`) and in
data/journal/activity-YYYY-MM-DD.md (indexed by docs.py, so past conversations are searchable by meaning)."""
import datetime, json, pathlib, re, sqlite3, threading

from .config import ROOT

_lock = threading.Lock()


def _db(cfg):
    p = pathlib.Path(cfg["db_path"])
    con = sqlite3.connect(p if p.is_absolute() else ROOT / p, timeout=10)
    con.execute("create table if not exists history(id integer primary key, ts text, lang text, "
                "user_text text, tools text, reply text)")
    return con


def _data(cfg):
    p = pathlib.Path(cfg["data_dir"])
    return p if p.is_absolute() else ROOT / p


def _one_line(s, limit=1500):
    s = re.sub(r"<[^>]+>", "", str(s or ""))                  # Telegram HTML
    return " ".join(s.split())[:limit]


def log_turn(cfg, text, lang, tools_used, reply, channel=""):
    """Remember what was asked and done. Never raises."""
    now = datetime.datetime.now()
    text, reply = _one_line(text), _one_line(reply)
    if not text:
        return
    try:
        with _lock:
            con = _db(cfg)
            try:
                con.execute("insert into history(ts, lang, user_text, tools, reply) values (?,?,?,?,?)",
                            (now.isoformat(timespec="seconds"), lang, text, json.dumps(list(tools_used)), reply))
                con.commit()
            finally:
                con.close()
            folder = _data(cfg) / "journal"
            folder.mkdir(parents=True, exist_ok=True)
            f = folder / f"activity-{now:%Y-%m-%d}.md"
            if not f.exists():
                f.write_text(f"# Activity {now:%Y-%m-%d}\n\n", encoding="utf-8")
            via = f" ({channel})" if channel else ""
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"- {now:%H:%M}{via} **You:** {text}\n  - tools: {', '.join(tools_used) or '-'}\n"
                         f"  - **Linda:** {reply}\n\n")
    except Exception as e:
        print("[history] could not save:", repr(e))


STOP = set("the a an and or to of in on for with about did i you me my we what when where who how is was were "
           "said say tell told ask asked talk talked mention mentioned linda last time".split())


def search(agent, query, k=5):
    """Past conversation turns about `query`: by meaning (activity files) or, if that is unavailable, by words."""
    out = []
    docs = getattr(agent, "docs", None)
    if docs is not None:
        try:
            for h in docs.search(query, 25, None, 0.0):
                name = pathlib.Path(h.path).name
                if name.startswith("activity-") and len(out) < k:
                    out.append({"day": name[9:19], "excerpt": h.text[:500], "score": round(h.score, 2)})
        except Exception as e:
            print("[history search]", repr(e)[:120])
    if out:
        return out
    words = [w for w in re.findall(r"\w{3,}", query.lower()) if w not in STOP][:5]
    if not words:
        return []
    con = _db(agent.cfg)
    try:
        where = " or ".join("lower(user_text || ' ' || reply) like ?" for _ in words)
        rows = con.execute(f"select ts, user_text, reply from history where {where} order by id desc limit ?",
                           [f"%{w}%" for w in words] + [k]).fetchall()
    finally:
        con.close()
    return [{"day": r[0][:10], "you": r[1][:300], "linda": (r[2] or "")[:300]} for r in rows]


def reindex(agent):
    """Pick up new/changed files in the background (incremental, cheap)."""
    docs = getattr(agent, "docs", None)
    if docs is not None:
        threading.Thread(target=lambda: _safe(docs), daemon=True).start()


def _safe(docs):
    try:
        docs.index()
    except Exception as e:
        print("[docs] reindex skipped:", repr(e)[:120])
