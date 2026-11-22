"""macOS Calendar through EventKit. Works with every account added to Calendar.app
(iCloud, Google, Exchange). First run shows a permission prompt."""
import collections, datetime, threading
from pydantic import BaseModel
from linda.i18n import WHEN, day_label

Event = collections.namedtuple("Event", "id title start end all_day")


class CalendarUnavailable(Exception):
    pass


class Calendar:
    def __init__(self):
        try:
            import EventKit, Foundation
        except ImportError as e:
            raise CalendarUnavailable("pip install pyobjc-framework-EventKit") from e
        self.EK, self.NS = EventKit, Foundation
        self.store = EventKit.EKEventStore.alloc().init()
        self.lock = threading.Lock()
        done, res = threading.Event(), {}

        def cb(granted, error):
            res["ok"] = granted
            done.set()

        if hasattr(self.store, "requestFullAccessToEventsWithCompletion_"):      # macOS 14+
            self.store.requestFullAccessToEventsWithCompletion_(cb)
        else:
            self.store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, cb)
        done.wait(60)
        if not res.get("ok"):
            raise CalendarUnavailable("Calendar access denied: System Settings > Privacy & Security > Calendars")

    def _ns(self, dt):
        return self.NS.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())

    @staticmethod
    def _py(nsdate):
        return datetime.datetime.fromtimestamp(nsdate.timeIntervalSince1970())

    def events(self, start, end):
        with self.lock:
            pred = self.store.predicateForEventsWithStartDate_endDate_calendars_(
                self._ns(start), self._ns(end), None)
            evs = self.store.eventsMatchingPredicate_(pred) or []
            out = [Event(str(e.eventIdentifier()), str(e.title() or ""), self._py(e.startDate()),
                         self._py(e.endDate()), bool(e.isAllDay())) for e in evs]
        return sorted(out, key=lambda e: e.start)

    def add(self, title, start, minutes=60):
        with self.lock:
            ev = self.EK.EKEvent.eventWithEventStore_(self.store)
            ev.setTitle_(title)
            ev.setStartDate_(self._ns(start))
            ev.setEndDate_(self._ns(start + datetime.timedelta(minutes=minutes)))
            ev.setCalendar_(self.store.defaultCalendarForNewEvents())
            ok, err = self.store.saveEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
            if not ok:
                raise RuntimeError(f"Could not save event: {err}")
            return str(ev.eventIdentifier())

    def delete(self, event_id):
        with self.lock:
            ev = self.store.eventWithIdentifier_(event_id)
            if ev is None:
                return False
            ok, _ = self.store.removeEvent_span_error_(ev, self.EK.EKSpanThisEvent, None)
            return bool(ok)


class EventSpec(BaseModel):
    title: str
    start: str
    duration_minutes: int = 60


class CalendarMixin:
    """Voice calendar commands on agent.cal: add an event (Gemini reads the time), undo, agenda."""

    def _cal(self):
        if not self.cal:
            self.k("cal_missing")
        return self.cal

    def do_calendar_add(self, it, text):
        cal = self._cal()
        if not cal:
            return
        if not self.cloud():
            return self.k("need_internet")
        now = datetime.datetime.now()
        prompt = (f"Now: {now:%Y-%m-%d %H:%M} ({now:%A}). The user said: \"{text}\"\n"
                  "Extract ONE calendar event. start = local ISO datetime like 2026-10-28T15:00 (no timezone). "
                  "If no time is given use 09:00. duration_minutes defaults to 60. "
                  "title = short, in the user's own language, without date or time words.")
        spec = self.gemini.structured(prompt, EventSpec)
        start = datetime.datetime.fromisoformat(spec.start)
        if start.tzinfo:
            start = start.astimezone().replace(tzinfo=None)
        self.last_event = cal.add(spec.title, start, max(5, spec.duration_minutes))
        self.k("cal_added", title=spec.title, day=day_label(start.date(), self.lang),
               time=start.strftime("%H:%M"))

    def do_calendar_undo(self, it, text):
        cal = self._cal()
        if not cal:
            return
        if self.last_event and cal.delete(self.last_event):
            self.last_event = None
            self.k("cal_undo_ok")
        else:
            self.k("cal_undo_none")

    def do_calendar_agenda(self, it, text):
        if self._cal():
            self._agenda(it.when if it.when in WHEN else "today")

    def _agenda(self, when):
        start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        if when == "tomorrow":
            start += datetime.timedelta(days=1)
        end = start + datetime.timedelta(days=7 if when == "week" else 1)
        evs = self.cal.events(start, end)
        if when == "today":
            evs = [e for e in evs if e.all_day or e.end > datetime.datetime.now()]
        label = WHEN[when][self.lang]
        if not evs:
            return self.k("cal_none", when=label)
        self.k("cal_count", n=len(evs), when=label)
        for e in evs[:8]:
            day = day_label(e.start.date(), self.lang)
            if e.all_day:
                self.k("cal_allday", day=day, title=e.title)
            else:
                self.k("cal_item", day=day, time=e.start.strftime("%H:%M"), title=e.title)
