"""Time-based triggers (morning briefing, practice reminder, evening check-in, birthdays, Telegram morning message).
Each job runs at most once per day; if Linda starts late it still runs inside the job's grace window."""
import datetime, time

JOB_ACTIONS = {"morning": "morning_briefing", "practice": "practice_reminder",
               "evening": "evening_checkin", "birthdays": "birthdays",
               "telegram_morning": "telegram_morning"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_TRIES = 5          # telegram_morning: attempts per day (e.g. no internet right after the Mac wakes up)


def due_jobs(jobs, store, now):
    for j in jobs:
        if j.get("enabled", True) is False:
            continue
        if j.get("days") and DAYS[now.weekday()] not in j["days"]:
            continue
        h, m = map(int, j["at"].split(":"))
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now < sched or now - sched > datetime.timedelta(hours=j.get("grace_h", 3)):
            continue
        if store.get(f"job:{j['id']}") == str(now.date()):
            continue
        yield j


def _run(agent, name):
    """True = done (or nothing to retry), False = failed and worth retrying."""
    if name == "telegram_morning":
        from . import briefing
        tg = getattr(agent, "tg", None)
        if tg is None:
            print("[scheduler] telegram_morning: Telegram is not configured")
            return True
        return briefing.send_morning(agent, tg) is not False
    getattr(agent, "job_" + name)()
    return True


def run_due(cfg, store, agent, now, tries):
    for j in list(due_jobs(cfg.get("schedule", []), store, now)):
        key = f"job:{j['id']}"
        store.set(key, str(now.date()))
        name = j.get("action") or JOB_ACTIONS.get(j["id"], j["id"])
        try:
            ok = _run(agent, name)
        except Exception as e:
            print(f"[scheduler] {j['id']}: {e!r}")
            ok = name != "telegram_morning"          # only the Telegram job is retried; the others never loop
        if not ok:
            tk = f"{j['id']}:{now.date()}"
            tries[tk] = tries.get(tk, 0) + 1
            if tries[tk] < MAX_TRIES:
                store.set(key, "")                   # not marked done: the next pass (30 s) tries again
                print(f"[scheduler] {j['id']} failed, retry {tries[tk]}/{MAX_TRIES - 1}")


def run_loop(cfg, store, agent):
    tries = {}
    while True:
        run_due(cfg, store, agent, datetime.datetime.now(), tries)
        time.sleep(30)


def calendar_watch(cfg, agent, cal):
    """Warns you `remind_minutes` before each event."""
    from . import system
    from .i18n import t
    mins, lang, seen = cfg["calendar"]["remind_minutes"], cfg["default_language"], set()
    while True:
        try:
            now = datetime.datetime.now()
            for e in cal.events(now, now + datetime.timedelta(minutes=mins + 1)):
                left = (e.start - now).total_seconds() / 60
                key = (e.id, e.start.isoformat())
                if not e.all_day and 0 < left <= mins and key not in seen:
                    seen.add(key)
                    line = t("cal_soon", lang, n=max(1, round(left)), title=e.title)
                    system.notify(cfg["name"], line)
                    agent.say(line, lang)
        except Exception as e:
            print("[calendar watcher]", e)
        time.sleep(30)