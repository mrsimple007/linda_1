"""Memory read: recall (the model's lookup tool), prefetch (search before the model answers), memory_index."""
import datetime
import json
import pathlib
import re
from linda.memory import reference

# ===================== memory read: recall + prefetch =====================

REL_GROUPS = [
    {"brother", "brothers", "брат", "братья", "aka", "uka", "akam", "ukam", "birodar"},
    {"sister", "sisters", "сестра", "сестры", "opa", "opam", "singil", "singlim"},
    {"mother", "mom", "mum", "мама", "мать", "ona", "onam"},
    {"father", "dad", "отец", "папа", "ota", "otam"},
    {"friend", "friends", "друг", "друзья", "dost", "dostim"},
    {"aunt", "тётя", "тетя", "xola", "xolam", "amma", "ammam", "yanga", "yangam"},
    {"grandmother", "grandmothers", "бабушка", "buvi", "buvim", "buvijon", "anna", "annam", "annalar", "annalarim"},
    {"grandfather", "дедушка", "bobo", "bobom"},
]
STOP = {"the", "and", "what", "when", "who", "where", "which", "how", "did", "does", "for", "you", "are", "was",
        "have", "has", "had", "with", "about", "tell", "send", "telegram", "birthday", "birthdays", "please",
        "can", "could", "next", "list", "give", "show", "know", "remember", "that", "this", "from", "your",
        "mine", "just", "some", "any", "get", "say", "said", "want", "need", "like", "yes", "not", "but", "now",
        "also", "there", "they", "his", "her", "our", "all", "one", "today", "yesterday",
        "когда", "что", "кто", "мой", "моя", "мои", "день", "рождения", "вчера", "сегодня",
        "qachon", "nima", "kim", "mening", "tug", "ilgan", "kuni", "kunim", "kecha", "bugun"}
KIND_ALIAS = {"people": "all", "family": "all", "friends": "all", "relatives": "all", "project": "progress",
              "projects": "progress", "diary": "journal", "note": "notes", "remember": "memory", "reminders": "memory",
              "yesterday": "day", "today": "day", "activity": "day", "date": "day"}
DAY_STORES = {"journal", "progress", "practice", "tasks"}
ADVICE_STORES = {"goals", "ideas", "profile", "decisions", "tasks", "progress", "journal", "practice", "songs"}
LOOKUP = re.compile(r"\b(when|what|who|where|which|how many|how old|tell me|do you know|did i|yesterday|today|"
                    r"birthdays?|tasks?|goals?|songs?|brother|sister|mother|father|friend|cousin|uncle|aunt|"
                    r"aka|uka|opa|qachon|kim|nima|kecha|bugun)\b|когда|кто|что|где|сколько|вчера|сегодня|брат|сестр|друг",
                    re.I)
CONVO = re.compile(r"\b(did i (?:say|tell|mention|ask)|i (?:said|told you|mentioned)|we (?:talked|discussed|spoke|decided)|"
                   r"last time|earlier (?:today|you said)|you (?:said|told me)|our (?:chat|conversation)|"
                   r"aytgan edim|gaplashgan|gaplash\w*|aytdim)\b|говорил|обсужда|разговор|ты сказал", re.I)
PROGRESS_INTENT = re.compile(
    r"\bimprov\w*|\bprogress\b|capabilit\w*|what can you do|worked on|work on|what have i (?:done|worked|improved)|"
    r"changelog|умеешь|можешь|прогресс|улучш|сделал|qila olasan|yaxshilad|ishlad", re.I)
CAPS = re.compile(r"capabilit\w*|what can you do|what are you able|умеешь|что ты можешь|qila olasan", re.I)
# keep in step with the tools you add (one line per area, read aloud in 1-3 sentences)
CAPABILITIES = ("tasks (add, finish, list), birthdays (save, look up, greetings), calendar (add, read; asks to confirm), "
                "practice and songs, Spotify music, Telegram messages, timers and reminders, weather and news, "
                "chess and quizzes, profile/goals/ideas notes, diary journal, project progress log, Mac controls, "
                "answering questions about your own saved data"
                "project progress log, Mac controls, finance (add expenses and revenue in Telegram, saved to Google Sheets; "
"reports need a PIN), answering questions about your own saved data"
                
                
                )
NAME = re.compile(r"\b[A-ZА-ЯЁ][a-zа-яё']{2,}\b")
NOT_NAMES = {"linda", "uzbek", "uzbekistan", "russian", "english", "family", "chess", "friends", "friend", "brother"}


def _tok(s):
    return [w for w in re.findall(r"\w+", str(s).lower()) if len(w) > 2]


UZ_SUFFIX = ("larimiz", "larim", "lardan", "larni", "lari", "lar", "imizni", "imiz", "ning", "imni", "imga",
             "dan", "ni", "ga", "da", "im")


def _stem(t):
    """Uzbek suffixes off a word, keeping at least 4 letters: 'annalarim' -> 'anna', 'islomni' -> 'islom'."""
    for suf in UZ_SUFFIX:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[:-len(suf)]
    return t


def _expand(tokens):
    out = set(tokens)
    out |= {t[:-1] for t in tokens if len(t) > 5 and t.endswith("s")} - STOP   # "alishers" -> "alisher"
    out |= {_stem(t) for t in tokens} - STOP
    for g in REL_GROUPS:
        if out & g:
            out |= g
    return out


def _match(s, t):
    """3 letters: whole word ("aka" must not hit "Zakaria"). 4 letters: start of a word ("anna" -> "Annaxon",
    "isla" not inside other words). Longer: anywhere ("timur" -> "Timurbek")."""
    if len(t) <= 3:
        return bool(re.search(r"\b" + re.escape(t) + r"\b", s))
    if len(t) == 4:
        return bool(re.search(r"\b" + re.escape(t), s))
    return t in s


def _has(s, toks):
    s = str(s).lower()
    return any(_match(s, t) for t in toks)


def _filter(rows, toks, keep=40):
    """rows: list of dicts. Returns (rows, matched)."""
    if not toks:
        return rows[:keep], True
    hit = [r for r in rows if _has(" ".join(map(str, r.values())), toks)]
    return (hit[:keep], True) if hit else (rows[:keep], False)


def _lines(lines, toks, keep=12):
    if not toks:
        return lines[:keep], True
    hit = [l for l in lines if _has(l, toks)]
    return hit[:keep], bool(hit)


def due_when(due):
    """'today' / 'tomorrow' / 'in 3 days (Tue 24 Nov)' / 'overdue by 2 days' / 'no date': computed, not guessed."""
    if not due:
        return "no date"
    try:
        d = datetime.date.fromisoformat(str(due)[:10])
    except ValueError:
        return str(due)
    n = (d - datetime.date.today()).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n < 0:
        return f"overdue by {-n} day" + ("s" if n < -1 else "")
    return f"in {n} days ({d:%a %d %b})"


def _dates_in(text):
    t, today, out = str(text).lower(), datetime.date.today(), []
    if re.search(r"yesterday|вчера|\bkecha\b", t):
        out.append(today - datetime.timedelta(days=1))
    if re.search(r"\btoday\b|сегодня|\bbugun\b", t):
        out.append(today)
    if re.search(r"\btomorrow\b|завтра|\bertaga\b", t):
        out.append(today + datetime.timedelta(days=1))
    for m in re.findall(r"\d{4}-\d{2}-\d{2}", t):
        try:
            out.append(datetime.date.fromisoformat(m))
        except ValueError:
            pass
    return out


def _file_lines(agent, f):
    p = agent.data_dir / f"{f}.md"
    if not p.exists():
        return []
    return [l.strip("-*+ ").strip() for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
            if l.strip() and not l.lstrip().startswith(("<!--", "#"))]


def _diary(agent, day, limit=10):
    """Journal lines for a day. Lines from Linda's own chat log are labelled so they are not read as things you did."""
    jd, out = agent.data_dir / "journal", []
    f = jd / f"{day}.md"
    if f.exists():
        out += [l.strip() for l in f.read_text(encoding="utf-8", errors="ignore").splitlines()
                if l.strip() and not l.startswith("#")][:limit]
    a = jd / f"activity-{day}.md"
    if a.exists():
        asked = [l.split("**You:**", 1)[1].strip().replace("**", "")[:140]
                 for l in a.read_text(encoding="utf-8", errors="ignore").splitlines() if "**You:**" in l]
        out += ["(asked Linda) " + s for s in asked[:3]]
    return out


def _practice_on(agent, day):
    rows = agent.store.q("SELECT s.title, p.instrument, p.minutes, p.focus, p.rating FROM practice_logs p "
                         "LEFT JOIN songs s ON s.id = p.song_id WHERE p.day = ?", (str(day),))
    return [{"song": t or "", "instrument": i or "", "minutes": m, "focus": f or "", "rating": r}
            for t, i, m, f, r in rows]


def _done_on(agent, dates):             # (verify) assumes tasks.done_ts is an epoch time, as finish_task writes it
    con = _tt_db(agent.cfg)
    try:
        rows = con.execute("select title, done_ts from tasks where status='done' and done_ts is not null").fetchall()
    finally:
        con.close()
    want = {str(d) for d in dates}
    out = []
    for t, ts in rows:
        try:
            if str(datetime.date.fromtimestamp(float(ts))) in want:
                out.append(t)
        except (TypeError, ValueError):
            pass
    return out


def song_meta_count(cfg):
    con = _song_db(cfg)
    try:
        return con.execute("select id from songs").fetchall()
    finally:
        con.close()


def projects_brief(agent, per=2):
    """One line per existing project log: title + latest entries, so the model can tell which project work belongs to."""
    pdir = agent.data_dir / "projects"
    out = []
    for p in (sorted(pdir.glob("*.md")) if pdir.exists() else []):
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        title = next((l.lstrip("# ").split(":")[0].strip() for l in lines if l.startswith("#")), p.stem)
        last = [re.sub(r"^-\s*\d{4}-\d{2}-\d{2}\s*·\s*", "", l)[:90] for l in lines if l.startswith("- ")][-per:]
        out.append(title + (f" (recent: {' | '.join(last)})" if last else ""))
    return out


def memory_index(agent):
    """One short line per store, put in the system prompt so the model knows what exists and how to read it."""
    st, d = agent.store, agent.data_dir

    def n(fn):
        try:
            return len(fn())
        except Exception:
            return 0
    jd = d / "journal"
    days = sorted(f.stem for f in jd.glob("2*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)) if jd.exists() else []
    try:
        tl = open_tasks_text(agent, 25)
    except Exception:
        tl = ""
    try:
        pj = "; ".join(projects_brief(agent))
    except Exception:
        pj = ""
    return "\n".join([
        f"birthdays: {n(lambda: st.upcoming(366))} people (name, date, relation)",
        f"open tasks (id: title): {tl or 'none'}",
        f"ongoing projects: {pj or 'none'}",
        f"songs: {n(lambda: song_meta_count(agent.cfg))}",
        "practice: sessions by date",
        f"profile / goals / ideas lines: {n(lambda: _file_lines(agent, 'profile'))} / "
        f"{n(lambda: _file_lines(agent, 'goals'))} / {n(lambda: _file_lines(agent, 'ideas'))}",
        f"decisions: {n(lambda: _file_lines(agent, 'decisions'))} (recall kind='decisions')",
        "past conversations with Linda: searchable by meaning (recall kind='conversations')",
        f"journal: {len(days)} days" + (f", latest {days[-1]}" if days else ""),
        f"quick notes ('remember that...'): {n(lambda: st.notes(200))} (recall kind='memory')",
        f"reference notes (full text via read_reference): {reference.names(agent) or 'none'}",
        "For any question about one day (yesterday, today, a date) call recall(kind='day') once: it returns "
        "journal, progress, practice and finished tasks together. For 'what can you do' or 'what did I improve on you' "
        "call recall(kind='capabilities'). For advice (what should I do, ideas, plans, wishes) "
        "call recall(kind='advice', query=<person if any>).",
    ])


def _gather(agent, kind, q, user_text, use_text=True):
    st = agent.store
    kind = KIND_ALIAS.get(kind, kind)
    qq = q.strip() or (user_text if use_text else "")
    toks = _expand(set(_tok(qq)) - STOP)
    ptoks = set()                       # advice mode: only names and relations are search words ("should", "today" are not)
    if kind == "advice":
        ptoks = _expand({w for w in (n.lower() for n in NAME.findall(f"{q} {user_text}")) if w not in STOP and w not in NOT_NAMES}
                        | {t for t in _tok(f"{q} {user_text}") if any(t in g for g in REL_GROUPS)})
        toks = set()
    dates = [] if kind == "advice" else _dates_in(f"{q} {user_text}")
    if dates and kind in DAY_STORES - {"tasks"}:
        kind = "day"                    # one call covers every date-bound store; kind='tasks' stays a task listing
    dayq = bool(dates) and not toks     # "what did I do yesterday": date stores only
    strict = kind == "all"              # in 'all' mode a section appears only if it matched
    both = f"{q} {user_text}"
    caps = kind == "capabilities" or bool(CAPS.search(both))
    prog_intent = caps or bool(PROGRESS_INTENT.search(both))      # "what did I work on / improve", "what can you do"

    def want(k):
        if kind == "day":
            return k in DAY_STORES
        if kind == "capabilities":
            return k == "progress"
        if kind == "advice":
            return k in ADVICE_STORES or (k == "birthdays" and bool(ptoks))
        if kind == "all":
            if dayq:
                return k in DAY_STORES
            if prog_intent:
                return k == "progress"
            return True
        return kind == k

    out = {"today": f"{datetime.date.today():%A %Y-%m-%d}"}
    if dates:
        out["asked_dates"] = [str(d) for d in dates]
    hits_text = []
    if caps and kind in ("all", "capabilities"):
        out["linda_can_do"] = CAPABILITIES

    def add(label, fn):
        try:
            v = fn()
        except Exception as e:
            print(f"[recall {label}] {e!r}"[:200])
            return
        if v is not None and (v or not strict):
            out[label] = v

    # 1) free text first: names found here are searched in the birthdays table (the 'brother' -> Yusuf join)
    if want("memory"):
        def memo():
            rows, ok = _lines([str(n) for n in (st.notes(60) or [])], toks, 40 if kind == "memory" else 10)
            if strict and not ok:
                return None
            hits_text.extend(rows if toks else [])
            return rows
        add("remembered", memo)
    for f in ("profile", "goals", "ideas", "decisions"):
        if want(f):
            def sect(f=f):
                every = _file_lines(agent, f)
                rows, ok = _lines(every, toks, 10 if strict else 25)
                if strict and not ok:
                    return None
                if toks and not ok:
                    out[f + "_note"] = "no line matched the query; showing the file"
                    rows = every[:12]
                if toks and ok:
                    hits_text.extend(rows)
                return rows
            add(f, sect)

    if kind == "advice" and ptoks:       # what the data says about the person (wishes, gifts, "my brother")
        pool = _file_lines(agent, "profile") + [str(n) for n in (st.notes(60) or [])]
        mine = [l for l in pool if _has(l, ptoks)][:6]
        if mine:
            out["about_person"] = mine
            hits_text.extend(mine)

    user = (agent.cfg.get("user_name") or "").lower()
    names = {n.lower().removesuffix("'s") for l in hits_text for n in NAME.findall(l)} - NOT_NAMES - {user}
    btoks = toks | names | ptoks
    tb = bool(toks or ptoks)

    if want("birthdays"):
        def bday():
            rows = []
            for b in st.upcoming(366):
                r = {"name": b.name, "date": f"{b.date:%b} {b.date.day}", "in_days": b.n, "relation": b.relation or ""}
                if b.lang:
                    r["greeting_lang"] = b.lang
                if b.age:
                    r["turning"] = b.age
                rows.append(r)
            hit, ok = _filter(rows, btoks, 8 if tb else 12)
            if strict and toks and not ok:
                return None
            if tb and not ok:
                out["birthdays_note"] = "no name or relation matched; these are just the next ones by date"
            if tb and ok:
                for r in hit:           # tell the model how each row matched: "Yusuf" must not silently become "Yusufbek"
                    words = set(re.findall(r"\w+", r["name"].lower()))
                    r["match"] = ("name" if words & btoks
                                  else "name_start" if any(w.startswith(t) for w in words for t in btoks if len(t) >= 4)
                                  else "relation" if _has(r["relation"], btoks) else "partial")
            if not tb:
                out["birthdays_total"] = len(rows)
            for r in hit[:5]:
                y = getattr(st.find_birthday(r["name"]), "year", None)
                if y:
                    r["birth_year"] = y
            if tb and ok:                # one short line per person: survives the size budget, so nobody is dropped
                direct = toks | ptoks            # words of the message, not names pulled in from matching notes
                rows_ = [r for r in hit if _has(f"{r['name']} {r['relation']}", direct)] or hit
                rel_words = set().union(*REL_GROUPS)
                named = [r for r in rows_ if _has(r["name"], direct - rel_words)]
                rows_ = named or rows_           # "lola anna": the named grandmother, not both
                kept = []                        # one person stored twice ("Anna" and "Lola Anna", same date)
                base_w = lambda r: set(re.findall(r"\w+", re.sub(r"\s*\(.*\)$", "", r["name"]).lower()))
                for r in sorted(rows_, key=lambda r: -len(base_w(r))):       # fullest name first
                    words = base_w(r)
                    if any(k["date"] == r["date"] and words <= k["_w"] for k in kept):
                        continue
                    kept.append({**r, "_w": words})
                kept.sort(key=lambda r: r["in_days"])
                out["matched_people"] = [f"{r['name']}: {r['relation'] or 'no relation saved'}, birthday {r['date']} "
                                         f"(in {r['in_days']} days)" for r in kept]
            return hit
        add("birthdays", bday)

    if want("tasks"):
        def task_rows():
            tt = task_times(agent.cfg)
            from linda.tools.tasks import split_deadline
            rows = []
            for r in st.open_tasks():
                _, dl, has_time = split_deadline(r[1], agent.cfg)
                row = {"id": r[0], "title": r[1], "due": r[2], "when": due_when(r[2]), "time": tt.get(_norm(r[1]), "")}
                if dl is not None and has_time:          # converted from the title's time zone: say this one
                    row["deadline_local_time"] = f"{dl:%d.%m.%Y %H:%M} Tashkent time"
                rows.append(row)
            if dates:                       # "today's tasks": the code decides the day, never the model
                asked = {str(d) for d in dates}
                on = [r for r in rows if r["due"] and str(r["due"])[:10] in asked]
                if datetime.date.today() in dates:          # same as /today: overdue and undated count as today's
                    on += [r for r in rows if not r["due"] or str(r["due"])[:10] < str(datetime.date.today())]
                return {"due_on_asked_day": on or "none",
                        "rule": "Only due_on_asked_day are tasks for that day. Other open tasks are for later days: "
                                "never list them as that day's tasks.",
                        "later_open_tasks": len(rows) - len(on)}
            hit, ok = _filter(rows, toks, 25)
            return None if (strict and toks and not ok) else hit
        if kind == "tasks" or dates or not dayq:
            add("open_tasks", task_rows)
        if dates:
            add("tasks_done", lambda: _done_on(agent, dates))

    if want("songs"):
        def songs():
            con = _song_db(agent.cfg)
            try:
                have = [r[1] for r in con.execute("pragma table_info(songs)")]
                cols = [c for c in ("id", "title", "artist", "instrument", "status", "priority") if c in have]
                rows = [dict(zip(cols, r)) for r in con.execute(f"select {', '.join(cols)} from songs")]
            finally:
                con.close()
            meta = song_meta(agent.cfg)
            for r in rows:
                t = str(r.get("title", ""))
                r["composer"] = (r.pop("artist", "") or meta.get(_norm(t))
                                 or meta.get(_norm(re.sub(r"\s*\([^)]*\)", "", t))) or "")
            hit, ok = _filter(rows, toks, 20)
            return None if (strict and not ok) else hit
        add("songs", songs)

    if want("practice"):
        def prac():
            if dates:
                return {str(d): _practice_on(agent, d) or "nothing logged" for d in dates}
            if strict and not (toks & {"practice", "practiced", "piano", "guitar", "played"}):
                return None
            return {"last_7_days": st.practice_summary(7)}
        add("practice", prac)

    if want("progress"):
        def prog():
            rows = project_context(agent, 300)
            low = both.lower()
            stems = [p.stem for p in (agent.data_dir / "projects").glob("*.md")]
            named = [x for x in stems if x in low or x.replace("-", " ") in low]
            if not named and "linda" in stems and (caps or (prog_intent and re.search(r"\b(you|your|linda)\b|тебя|тебе", low))):
                named = ["linda"]               # "what did I improve on you" = the Linda project
            if named:
                rows = [r for r in rows if any(f"[{x}]" in r for x in named)]
            if dates:
                return [r for r in rows if r.startswith(tuple(str(d) for d in dates))][:10] or "nothing logged"
            if prog_intent or not toks:         # a listing request: the user's verbs are not search words
                return rows[:10] or "nothing logged"
            hit, ok = _lines(rows, toks, 8)
            return None if (strict and not ok) else hit
        add("progress_log", prog)

    if want("journal"):
        def journal():
            if dates:
                return {str(d): _diary(agent, d) or "nothing recorded" for d in dates}
            if strict:
                return None
            jd = agent.data_dir / "journal"
            days = sorted((f.stem for f in jd.glob("2*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.stem)), reverse=True)[:3]
            return {d: _lines(_diary(agent, d, 8), toks, 6)[0] for d in days}
        add("journal", journal)

    if kind == "conversations" or (kind == "all" and CONVO.search(both)):
        from linda.memory import history
        add("past_conversations", lambda: history.search(agent, qq or both, 5) or None)

    if kind in ("all", "notes") and toks and agent.docs and not dayq:
        add("notes", lambda: [{"file": pathlib.Path(h.path).name, "text": h.text[:600]}
                              for h in agent.docs.search(qq, 10, None)
                              if not pathlib.Path(h.path).name.startswith(("activity-", "root_"))][:3])

    if strict and set(out) <= {"today", "asked_dates"}:
        out["found"] = "nothing matched"
    return out


def _size(x):
    return len(json.dumps(x, ensure_ascii=False, default=str))


KEEP_WHOLE = ("matched_people",)          # never shrunk: the complete answer to "who are my ..." questions


def _dump(out, budget):
    """Shrink the biggest section until it fits, so the JSON stays valid and no store is cut off whole.
    matched_people goes first and is never shrunk."""
    out = {**{k: out[k] for k in KEEP_WHOLE if k in out}, **{k: v for k, v in out.items() if k not in KEEP_WHOLE}}
    budget = max(budget, _size({k: out[k] for k in KEEP_WHOLE if k in out}) + 400)
    while _size(out) > budget:
        big = max((k for k, v in out.items() if isinstance(v, (list, dict)) and len(v) > 1 and k not in KEEP_WHOLE),
                  key=lambda k: _size(out[k]), default=None)
        if big is None:
            break
        v = out[big]
        out[big] = v[:len(v) // 2] if isinstance(v, list) else dict(list(v.items())[:max(1, len(v) // 2)])
    return json.dumps(out, ensure_ascii=False, default=str)[:budget]


def recall_data(agent, a, user_text="", budget=2000):
    """Read-only. Returns JSON text for the model; never speaks."""
    kind, q = str(a.get("kind") or "all").lower(), str(a.get("query") or "")
    print(f"[recall] kind={kind} query={q!r}")
    res = _dump(_gather(agent, kind, q, user_text, use_text=False), budget)
    print(f"[recall] -> {len(res)} chars: {res[:160]}")
    return res


def _mentions_person(agent, text):
    """A short message naming someone from the birthday list ('anna?', 'Timur') is a question about them."""
    if len(text.split()) > 5:
        return False
    toks = {t for t in _expand(set(_tok(text)) - STOP) if len(t) >= 4}
    if not toks:
        return False
    try:
        people = agent.store.upcoming(366)
    except Exception:
        return False
    return any(_has(f"{b.name} {b.relation or ''}", toks) for b in people)


def prefetch(agent, text, budget=1500, think=False):
    """Retrieval first: search every store in code before the model sees the message.
    think=True (advice, planning, wishes): goals, ideas, profile, tasks, recent work, journal, practice, songs,
    plus the person's birthday row and profile lines when a name or relation is mentioned.
    Open tasks and ongoing projects are not fetched here: memory_index() shows them on every turn."""
    if reference.MEM_ASK.search(text):               # "what did I tell you to remember": read EVERYTHING stored
        try:
            out = _gather(agent, "memory", "", text, use_text=False)
            out["reference_files"] = reference.names(agent) or "none"
            res = _dump(out, max(budget, 3000))
        except Exception as e:
            print("[prefetch memory]", repr(e)[:120])
            return ""
        print(f"[prefetch memory] {len(res)} chars: {res[:120]}")
        return res
    if think:
        try:
            res = _dump(_gather(agent, "advice", "", text), max(budget, 2400))
        except Exception as e:
            print("[prefetch think]", repr(e)[:120])
            return ""
        print(f"[prefetch think] {len(res)} chars: {res[:120]}")
        return res
    if not (LOOKUP.search(text) or CONVO.search(text) or text.rstrip().endswith("?") or _mentions_person(agent, text)):
        return ""
    try:
        out = _gather(agent, "all", "", text)
    except Exception as e:
        print("[prefetch]", repr(e)[:120])
        return ""
    if "found" in out:
        return ""
    res = _dump(out, budget)
    print(f"[prefetch] {len(res)} chars: {res[:120]}")
    return res


# names from the sibling modules (imported last, so mutual use works)
from linda.tools.tasks import _norm, _tt_db, open_tasks_text, project_context, task_times  # noqa: E402
from linda.tools.handlers import _song_db, song_meta  # noqa: E402
