"""macOS Calendar through EventKit. Works with every account added to Calendar.app
(iCloud, Google, Exchange). First run shows a permission prompt."""
import collections, datetime, threading

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