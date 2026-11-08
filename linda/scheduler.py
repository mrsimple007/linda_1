"""Time-based triggers (morning briefing, practice reminder, evening check-in, birthdays).
Each job runs at most once per day; if Linda starts late it still runs inside the job's grace window."""
import datetime, time

JOB_ACTIONS = {"morning": "morning_briefing", "practice": "practice_reminder",
               "evening": "evening_checkin", "birthdays": "birthdays"}
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


def run_loop(cfg, store, agent):
    while True:
        now = datetime.datetime.now()
        for j in list(due_jobs(cfg.get("schedule", []), store, now)):
            store.set(f"job:{j['id']}", str(now.date()))
            try:
                getattr(agent, "job_" + (j.get("action") or JOB_ACTIONS.get(j["id"], j["id"])))()
            except Exception as e:
                print(f"[scheduler] {j['id']}: {e!r}")
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