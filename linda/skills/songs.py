"""Song statuses and song suggestions.

Statuses, in order: wishlist (want to learn) -> learning -> polishing -> learned; archived = paused or dropped.
data/songs.yaml stays the master copy (it is re-imported at every start), so every status change made by
voice or Telegram is written back to the song's line there as well as to the database."""
import datetime, json, re
import yaml
from linda.channels.telegram.telegram import esc

STATUSES = ("wishlist", "learning", "polishing", "learned", "archived")
LABEL = {"wishlist": "Want to learn", "learning": "Learning", "polishing": "Polishing / review",
         "learned": "Learned", "archived": "Paused / dropped"}
ICON = {"wishlist": "💭", "learning": "📖", "polishing": "✨", "learned": "✅", "archived": "⏸"}
# what people say -> status (en / ru / uz); checked in this order, so "want to learn" wins over "learn"
ALIASES = [
    ("wishlist", r"wish|want|plan|later|someday|next|todo|to.?learn|хочу|план|потом|xohla|reja|keyin"),
    ("archived", r"archiv|pause|drop|stop|quit|gave up|abandon|паус|брос|забро|отлож|to'?xta|tashla"),
    ("learned", r"learned|learnt|known|know it|repertoire|finish|done|master|выучил|знаю|репертуар|o'?rgandim|bildim"),
    ("polishing", r"polish|refin|clean|almost|шлиф|почти|silliq|deyarli"),
    ("learning", r"learning|start|began|practis|practic|work|уч|начал|o'?rgan|boshla"),
]


def normalize(status):
    """'want to learn' -> 'wishlist', 'repertoire' -> 'learned', ... ; None when it is not a status."""
    s = str(status or "").strip().lower()
    if s in STATUSES:
        return s
    for key, pat in ALIASES:
        if re.search(pat, s):
            return key
    return None


# ---------- songs.yaml write-back ----------
def _flow(d):
    return yaml.safe_dump(d, default_flow_style=True, allow_unicode=True, sort_keys=False, width=10 ** 6).strip()


def write_yaml(path, title, instrument, **fields):
    """Set fields on the song's line in songs.yaml (comments and the other lines stay as they are).
    A song that is not in the file yet is appended. Returns True when the file changed."""
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    key = (title.strip().lower(), instrument.strip().lower())
    for i, line in enumerate(lines):
        body = line.strip()
        if not body.startswith("- {"):
            continue
        try:
            d = yaml.safe_load(body[2:])
        except yaml.YAMLError:
            continue
        if not isinstance(d, dict):
            continue
        if (str(d.get("title", "")).strip().lower(), str(d.get("instrument", "piano")).strip().lower()) == key:
            d.update({k: v for k, v in fields.items() if v not in (None, "")})
            lines[i] = line[:len(line) - len(line.lstrip())] + "- " + _flow(d)
            break
    else:
        if not any(l.strip() == "songs:" for l in lines):
            lines.append("songs:")
        d = {"title": title, "instrument": instrument, **{k: v for k, v in fields.items() if v not in (None, "")}}
        lines.append("  - " + _flow(d))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


# ---------- reading ----------
def all_songs(store):
    """Every song, archived ones too, with days since last practice."""
    from linda.store import SONG_COLS, Song
    today, out = datetime.date.today(), []
    for r in store.q(f"SELECT {SONG_COLS} FROM songs ORDER BY priority DESC, title"):
        s = Song(*r)
        last = store.last_practiced(s.id)
        out.append({"title": s.title, "artist": s.artist or "", "instrument": s.instrument,
                    "status": normalize(s.status) or "learning", "priority": s.priority,
                    "days_since_practice": (today - last).days if last else None})
    return out


def overview(store):
    """{status: [song dicts]} in STATUSES order, empty groups left out."""
    groups = {k: [] for k in STATUSES}
    for s in all_songs(store):
        groups[s["status"]].append(s)
    return {k: v for k, v in groups.items() if v}


# ---------- tools ----------
def set_status(agent, a, text=""):
    """Tool song_status: move a song to another status (adds it when it is new)."""
    name = str(a.get("name") or "").strip()
    status = normalize(a.get("status"))
    say = lambda en, ru, uz: agent.say({"en": en, "ru": ru, "uz": uz}[agent.lang])
    if not name:
        return say("Which song?", "Какая песня?", "Qaysi qo'shiq?")
    if not status:
        return say("Which status: want to learn, learning, polishing, learned or paused?",
                   "Какой статус: хочу выучить, учу, шлифую, выучил или на паузе?",
                   "Qaysi holat: o'rganmoqchiman, o'rganyapman, silliqlayapman, o'rgandim yoki to'xtatdim?")
    st, instr = agent.store, str(a.get("instrument") or "").lower().strip()
    song = st.find_song(name, instr)
    if song:
        st.x("UPDATE songs SET status = ? WHERE id = ?", (status, song.id))
        title, instr = song.title, song.instrument
    else:
        title = name
        instr = instr or agent.cfg["goals"]["instruments"][0]
        st.upsert_song(title, instr, artist=str(a.get("composer") or ""), status=status)
    write_yaml(agent.data_dir / "songs.yaml", title, instr, status=status)
    agent._out(reply=f"{title}: {LABEL[status]}")
    say(f"{title} is now: {LABEL[status].lower()}.", f"{title}: теперь «{LABEL[status].lower()}».",
        f"{title}: endi «{LABEL[status].lower()}».")


def suggest(agent, a, text=""):
    """Tool song_suggest: hand the model the songs grouped by status plus how to use them; it writes the advice."""
    groups = overview(agent.store)
    facts = {k: [{x: s[x] for x in ("title", "artist", "instrument", "priority", "days_since_practice")}
                 for s in v] for k, v in groups.items()}
    focus = str(a.get("focus") or "").strip() or "general"
    agent.say(json.dumps({
        "songs_by_status": facts,
        "focus": focus,
        "how_to_suggest": [
            "practice: favour 'learning' songs not practiced for days, then 'polishing'; 'learned' ones only to keep them fresh",
            "next to learn: pick from 'wishlist' first, preferring high priority and what fits after the current 'learning' songs",
            "new ideas: suggest well-known pieces (title + composer/artist) similar in style or a small step harder than "
            "'learned' and 'learning', never ones already listed",
            "if 3 or more songs are 'learning', advise finishing one before starting another",
            "use only the songs listed here as the user's songs; offer to save a new idea with status wishlist",
        ]}, ensure_ascii=False, default=str))


# ---------- Telegram /songs ----------
def card(store):
    """-> (title, sections) for /songs."""
    groups = overview(store)
    sections = []
    for k, songs in groups.items():
        lines = []
        for s in songs:
            extra = f" · {esc(s['artist'])}" if s["artist"] else ""
            if k in ("learning", "polishing") and s["days_since_practice"] is not None:
                extra += f" · practiced {s['days_since_practice']}d ago"
            elif k in ("learning", "polishing"):
                extra += " · not practiced yet"
            lines.append(f"• <b>{esc(s['title'])}</b>{extra}")
        sections.append((f"{ICON[k]} {LABEL[k]} ({len(songs)})", lines))
    return "Your songs", sections


def send_card(agent, tg):
    title, sections = card(agent.store)
    if not sections:
        return tg.send_styled(title, icon="🎵", intro="No songs yet. Tell me one you want to learn.")
    return tg.send_styled(title, sections, icon="🎵",
                          footer="Change one: “Clair de Lune: learned” · Ask: “what should I learn next?”")
