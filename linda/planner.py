"""Deterministic, explainable planning. Python scores the candidates and records WHY;
the LLM only phrases the result (it must not add or change items)."""
import dataclasses, datetime


@dataclasses.dataclass
class Item:
    kind: str
    title: str
    minutes: int
    reasons: list
    score: float
    ref: dict = dataclasses.field(default_factory=dict)


def test_date(cfg):
    from .util import parse_date
    return parse_date((cfg.get("goals") or {}).get("driving_test_date"))


def build_plan(store, cfg, minutes, today=None, scope="practice"):
    today = today or datetime.date.today()
    stale = cfg["practice"]["stale_days"]
    cands = []

    for s in store.songs():
        last = store.last_practiced(s.id)
        days = (today - last).days if last else None
        score = {1: 0.0, 2: 1.0, 3: 2.0}.get(s.priority, 1.0)
        score += {"learning": 2.0, "polishing": 1.5, "repertoire": 0.5}.get(s.status, 1.0)
        reasons = [f"status: {s.status}"] + ([f"priority {s.priority} of 3"] if s.priority == 3 else [])
        if days is None:
            score += 2; reasons.append("never practiced yet")
        elif days == 0:
            score -= 2.5; reasons.append("already practiced today")
        else:
            score += min(days, stale) / 7
            if days >= 3:
                reasons.append(f"last practiced {days} days ago")
        m = store.last_analysis(s.id)
        if m and m.get("accuracy") is not None and m["accuracy"] < 0.8:
            score += 1; reasons.append(f"last playback check: {round(m['accuracy'] * 100)}% of notes correct")
        if m and m.get("steadiness_cv") is not None and m["steadiness_cv"] > 0.12:
            score += 0.8; reasons.append("timing was uneven last time")
        r = store.last_rating(s.id)
        if r is not None and r <= 2:
            score += 1; reasons.append(f"you rated your last session {r} of 5")
        cands.append(Item("song", f"{s.title} ({s.instrument})", {"learning": 15, "polishing": 10}.get(s.status, 5),
                          reasons, score, {"song_id": s.id, "instrument": s.instrument}))

    td, stats = test_date(cfg), store.quiz_bank_stats()
    if td or stats:
        score, reasons = 0.5, []
        if td and td >= today:
            left = (td - today).days
            score += 1 + 3 * max(0.0, 1 - left / 60)
            reasons.append(f"driving test in {left} days")
        last = store.last_quiz_day()
        if last is None:
            score += 1.5; reasons.append("no theory quiz done yet")
        elif (today - last).days >= 2:
            score += 1; reasons.append(f"last theory quiz {(today - last).days} days ago")
        weak = sorted(((c / n, b) for b, (n, c) in stats.items() if n >= 5))
        if weak and weak[0][0] < 0.8:
            score += 1; reasons.append(f"weakest question set: {weak[0][1]} ({round(weak[0][0] * 100)}% correct)")
        cands.append(Item("quiz", "Driving theory quiz", 10, reasons, score))

    if scope != "practice":
        cs = store.chess_stats()
        if minutes >= 20:
            days = (today - cs["last_game"]).days if cs["last_game"] else None
            cands.append(Item("chess", "A game of chess", 15,
                              [f"last game {days} days ago" if days is not None else "no games logged yet"],
                              min(2.0, 0.8 + (days or 7) / 10)))
        due = store.tasks_due(today)
        if due:
            cands.append(Item("task", f"{len(due)} task(s) due", 10, [t[1] for t in due[:3]], 4.0))

    cands.sort(key=lambda i: -i.score)
    plan, left = [], int(minutes)
    for c in cands:
        if left < 5 or len(plan) >= 3:
            break
        m = min(c.minutes, left)
        if left - m < 5:
            m = left
        plan.append(dataclasses.replace(c, minutes=m))
        left -= m
    return plan


def as_facts(plan, minutes):
    return {"minutes_available": minutes,
            "items": [{"what": i.title, "minutes": i.minutes, "why": i.reasons} for i in plan]}