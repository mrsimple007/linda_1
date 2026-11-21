"""Write handlers for the other tools (notes, birthdays, practice, songs, finance, clients, bot stats), SPECIAL, execute()."""
import datetime
import re
import sqlite3
from linda.i18n import day_label, fmt_date
from linda.router import Intent
from linda.memory import memory_files
from linda.memory import reference
from linda.tools.tasks import _calendar_add, _log_progress, _task_add, _task_done

def _web_answer(agent, a, text):
    q = str(a.get("query") or text)
    agent._out(reply=agent._stream_say(agent.gemini.stream(q, agent._chat_system(), grounding=True), agent.lang))


def _mem(name):
    return lambda agent, a, text: memory_files.handle(agent, name, a)


def _read_note(agent, a, text):
    f = str(a.get("file") or "goals").lower()
    f = f if f in ("profile", "goals", "ideas", "decisions") else "goals"
    p = agent.data_dir / f"{f}.md"
    lines = []
    if p.exists():
        for l in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            l = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+|\*\*|~~|\[[ xX]\]\s*", "", l).strip()
            if l and not l.startswith(("#", "<!--")):
                lines.append(l)
    if not lines:
        return agent.say({"en": f"Your {f} are empty.", "ru": "Пока пусто.", "uz": "Hozircha bo'sh."}[agent.lang])
    agent._out(reply=agent._explain({"file": f, "items": lines[:25]},
                                    f"Tell the user what is in their {f}, briefly and naturally."))


ME = {"", "me", "my", "mine", "i", "myself", "men", "mening", "я", "мой", "моё"}


def _is_me(agent, b):
    user = (agent.cfg.get("user_name") or "").lower()
    return (b.relation or "").lower() == "me" or b.name.lower() in ("me", user)


def birthday_lookup(agent, a, text):
    who = str(a.get("name") or "").strip().lower()
    own = who in ME
    rows = agent.store.upcoming(366)
    if own:
        hits = [x for x in rows if _is_me(agent, x)]
    else:
        toks = _expand(set(_tok(who)) - STOP)
        hits = [x for x in rows if _has(x.name, toks) or _has(x.relation or "", toks)]
    if not hits:
        return agent.say({"en": "I don't have that birthday yet. Tell me the name and the date.",
                          "ru": "Этого дня рождения у меня ещё нет. Скажи имя и дату.",
                          "uz": "Bu tug'ilgan kun menda yo'q. Ism va sanani ayting."}[agent.lang])
    for b in hits[:4]:
        d = fmt_date(b.date, agent.lang)
        agent.say({"en": f"Your birthday is {d}, in {b.n} days." if own else f"{b.name}: {d}, in {b.n} days.",
                   "ru": f"Твой день рождения {d}, через {b.n} дн." if own else f"{b.name}: {d}, через {b.n} дн.",
                   "uz": f"Tug'ilgan kuningiz {d}, {b.n} kundan keyin." if own else f"{b.name}: {d}, {b.n} kundan keyin."}[agent.lang])


def _resolve_day(v):
    """'yesterday' / 'вчера' / 'kecha' / YYYY-MM-DD -> ISO date string, never in the future; None = today."""
    s, today = str(v or "").strip().lower(), datetime.date.today()
    if s in ("yesterday", "вчера", "kecha"):
        return str(today - datetime.timedelta(days=1))
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return str(d) if d <= today else None


def _practice_log(agent, a, text):
    try:
        minutes = int(float(a.get("minutes") or 0))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        return agent.k("need_minutes")
    if not a.get("already_done"):
        if _can_retry(agent):
            return agent.say("error: this is a plan, not a finished session, so it is NOT logged. If no matching task exists, "
                             "call task_add (title like 'Practice <song> on piano', due today, time if said). Open tasks: "
                             + open_tasks_text(agent))
        return agent.say({"en": "That sounds like a plan, so I didn't log it. Tell me when you've played.",
                          "ru": "Это план, поэтому я не записала. Скажи, когда сыграешь.",
                          "uz": "Bu reja, shuning uchun yozmadim. Chalib bo'lgach ayting."}[agent.lang])
    if minutes > 480:
        return agent.say(f"That is {minutes} minutes. How many minutes did you mean?")
    name, instr = str(a.get("name") or ""), str(a.get("instrument") or "").lower()
    song = agent.store.find_song(name, instr) if name else None
    try:
        r = int(float(a.get("rating") or 0))
    except (TypeError, ValueError):
        r = 0
    agent.store.log_practice(minutes, song.id if song else None, song.instrument if song else instr,
                             str(a.get("focus") or ""), r if 1 <= r <= 5 else None, day=_resolve_day(a.get("date")))
    week = agent.store.practice_summary(7)["total_minutes"]
    if song:
        agent.k("practice_logged_song", minutes=minutes, title=song.title, week=week)
    else:
        agent.k("practice_logged", minutes=minutes, week=week)


def _practice_delete(agent, a, text):
    try:
        n = max(1, min(int(float(a.get("count") or 1)), 20))
    except (TypeError, ValueError):
        n = 1
    day = _resolve_day(a.get("date"))
    con = _tt_db(agent.cfg)
    try:
        if day:
            rows = con.execute("select rowid, minutes from practice_logs where day=? order by rowid desc limit ?",
                               (day, n)).fetchall()
        else:
            rows = con.execute("select rowid, minutes from practice_logs order by rowid desc limit ?", (n,)).fetchall()
        for r in rows:
            con.execute("delete from practice_logs where rowid=?", (r[0],))
        con.commit()
    except sqlite3.OperationalError as e:
        print("[practice_delete]", e)
        return agent.k("error")
    finally:
        con.close()
    if not rows:
        return agent.say({"en": "There are no sessions to remove.", "ru": "Нечего удалять.",
                          "uz": "O'chirish uchun mashg'ulot yo'q."}[agent.lang])
    total = sum(int(r[1] or 0) for r in rows)
    week = agent.store.practice_summary(7)["total_minutes"]
    agent.say({"en": f"Removed {len(rows)} session(s), {total} minutes. {week} minutes this week now.",
               "ru": f"Удалила сессий: {len(rows)}, {total} минут. Теперь за неделю {week} минут.",
               "uz": f"{len(rows)} ta mashg'ulot o'chirildi, {total} daqiqa. Bu hafta {week} daqiqa."}[agent.lang])


def _song_db(cfg):
    con = _tt_db(cfg)
    con.execute("create table if not exists song_meta(k text primary key, composer text, notes text)")
    return con


def song_meta(cfg):
    con = _song_db(cfg)
    try:
        return dict(con.execute("select k, composer from song_meta where composer != ''").fetchall())
    finally:
        con.close()


def _song_add(agent, a, text):
    name = str(a.get("name") or "").strip()
    if not name or name.lower() in ("piano", "guitar", "song"):
        return agent.say({"en": "Which song should I add?", "ru": "Какую песню добавить?",
                          "uz": "Qaysi qo'shiqni qo'shay?"}[agent.lang])
    instr = str(a.get("instrument") or agent.cfg["goals"]["instruments"][0]).lower()
    status = str(a.get("status") or "learning").lower()
    status = status if status in ("learning", "polishing", "repertoire") else "learning"
    comp, notes = str(a.get("composer") or "").strip(), str(a.get("notes") or "").strip()
    st = agent.store
    ex = st.find_song(name, instr)
    if ex:
        name = ex.title
        if comp or notes:                # upsert overwrites, so carry the old values over
            st.upsert_song(ex.title, ex.instrument, artist=comp or ex.artist or "", status=ex.status,
                           priority=ex.priority, target_bpm=ex.target_bpm, melody=ex.melody or "",
                           notes=notes or ex.notes or "")
    else:
        st.upsert_song(name, instr, artist=comp, status=status, notes=notes)
    agent.k("song_saved", title=name + (f" by {comp}" if comp else ""), instrument=instr)


def _bot_stats():
    from linda.business import bot_stats
    return bot_stats


def _clients():
    from linda.business import clients
    return clients


def _finance_report(agent, a, text):
    tg = getattr(agent, "tg", None)
    brain = getattr(agent, "brain", None)
    if tg is None or not getattr(brain, "extra_rules", ""):      # extra_rules is only set for Telegram
        return agent.say("error: finance reports are only available in Telegram, behind the PIN.")
    from linda.channels.telegram import briefing
    briefing.finance_request(agent, tg, str(a.get("question") or text))
    agent._fin_done = True
    agent.say("Done: the finance answer was sent to Telegram. Reply with one short line.")

def _query_data(agent, a, text):
    q, dom = str(a.get("question") or text), str(a.get("domain") or "").lower()
    da = getattr(agent, "data_ask", None)
    if da is None or dom not in da.domains:
        return agent.say(f"error: unknown domain. Use one of: {', '.join(da.domains) if da else 'none'}.")
    tg = getattr(agent, "tg", None)
    if dom == "finance":                                  # PIN door stays in code
        return _finance_report(agent, a, text)
    ans = da.ask(q, dom)
    agent.say(ans)                                        # the model sees it and can word it in the reply


# One registry of special handlers.
SPECIAL = {
    "calendar_add": _calendar_add, "task_add": _task_add, "web_answer": _web_answer,
    "save_note": _mem("save_note"), "complete_goal": _mem("complete_goal"), "remove_note": _mem("remove_note"),
    "read_note": _read_note, "birthday_lookup": birthday_lookup,
    "practice_log": _practice_log, "practice_delete": _practice_delete, "log_progress": _log_progress,
    "task_done": _task_done, "song_add": _song_add,
    "finance_report": _finance_report,
    "bot_stats": lambda ag, a, t: _bot_stats().bot_stats(ag, a, t),
    "smart_home": lambda ag, a, t: __import__("linda.skills.smart_home", fromlist=["tool"]).tool(ag, a, t),
    "client_add": lambda ag, a, t: _clients().client_add(ag, a, t),
    "client_update": lambda ag, a, t: _clients().client_update(ag, a, t),
    "client_info": lambda ag, a, t: _clients().client_info(ag, a, t),
    "client_list": lambda ag, a, t: _clients().client_list(ag, a, t),
    "client_undo": lambda ag, a, t: _clients().client_undo(ag, a, t),
    "query_data": _query_data
}


# ---- news / birthday list: built in code from RSS and the database (see briefing.py) ----
def _news(agent, a, text):
    from linda.channels.telegram import briefing
    briefing.run_news(agent, a, text)


def _birthday_list(agent, a, text):
    from linda.channels.telegram import briefing
    briefing.run_birthday_list(agent, a, text)


SPECIAL.update({"news": _news, "list_birthdays": _birthday_list})
SPECIAL.update({"save_reference": reference.save_reference, "read_reference": reference.read_reference})


def execute(agent, name, args, text):
    print(f"[tool] {name} {args}")
    if name not in SPEC:
        raise KeyError(f"unknown tool {name}")
    from linda import safety
    if not safety.request_is_supported(name, text):
        agent.store.audit_action(name, args, "rejected_unsubstantiated")
        agent.say({"en": "I may have misheard. Please repeat what you want me to do.",
                   "ru": "Возможно, я ослышалась. Повторите, пожалуйста, что нужно сделать.",
                   "uz": "Noto'g'ri eshitgan bo'lishim mumkin. Iltimos, nima qilishim kerakligini qaytaring."}
                  [agent.lang])
        return

    def invoke():
        if name in SPECIAL:
            SPECIAL[name](agent, args, text)
        else:
            fields = coerce(args, SPEC[name][4])
            getattr(agent, "do_" + name)(Intent(intent=name, **fields), text)
        if args.get("telegram") and agent._produced and name != "telegram_send":
            agent.send_last()

    if name == "telegram_send":
        invoke()  # send_last owns the confirmation gate and records the actual payload.
    else:
        agent.run_action(name, args, invoke)


def missing(it):
    """Required fields the local model left at their default. Used to reject guessed intents."""
    spec = SPEC.get(it.intent)
    if not spec:
        return []
    out = []
    for p in spec[3]:
        f = spec[4].get(p, p)
        fld = Intent.model_fields.get(f)
        if fld is not None and getattr(it, f) == fld.default:
            out.append(p)
    return out


# names from the sibling modules (imported last, so mutual use works)
from linda.tools.registry import SPEC  # noqa: E402
from linda.tools.tasks import _can_retry, _tt_db, coerce, open_tasks_text  # noqa: E402
from linda.memory.recall import STOP, _expand, _has, _tok  # noqa: E402
