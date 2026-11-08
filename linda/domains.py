"""Domain modules mixed into Agent. They all read/write the SAME database (self.store)."""
import datetime, json, pathlib, random, threading, time
from pydantic import BaseModel
from . import system as sysm
from .audio_analysis import SR, analyze, tune
from .i18n import LANG_NAME, LANGS, fmt_date, t
from .planner import as_facts, build_plan, test_date
from .quizbank import load_banks, parse_choice, pick
from .router import Intent
import re

QUIZ_STOP = ("stop", "quit", "стоп", "хватит", "to'xta", "toxta", "tugat")
QUIZ_REPEAT = ("repeat", "again", "повтор", "takror")
STATUSES = ("learning", "polishing", "repertoire")

DONE = re.compile(r"\b(done|finished|completed|did it|i practi[cs]ed|готово|закончил\w*|сделал\w*|tugadi|bo'?ldi|tayyor)\b", re.I)
class ReviewSpec(BaseModel):
    summary: str
    tomorrow_focus: str
    new_tasks: list[str] = []


class PracticeMixin:
    """Songs, practice logs, session planning, audio feedback, stats."""

    def do_song_add(self, it, text):
        if not it.name.strip() or it.name.strip().lower() in ("piano", "guitar", "song"):
            return self.say({"en": "Which song should I add?", "ru": "Какую песню добавить?",
                             "uz": "Qaysi qo'shiqni qo'shay?"}[self.lang])

    def do_practice_log(self, it, text):
        if it.minutes <= 0:
            return self.k("need_minutes")
        song = self.store.find_song(it.name, it.action) if it.name else None
        self.store.log_practice(it.minutes, song.id if song else None,
                                song.instrument if song else it.action.lower(), it.text,
                                it.score if 1 <= it.score <= 5 else None)
        week = self.store.practice_summary(7)["total_minutes"]
        if song:
            self.k("practice_logged_song", minutes=it.minutes, title=song.title, week=week)
        else:
            self.k("practice_logged", minutes=it.minutes, week=week)

    def _explain(self, facts, instruction, local=False):
        prompt = (f"{instruction}\nAnswer in {LANG_NAME[self.lang]}, in 2-4 short spoken sentences, plain text, "
                  f"no markdown or lists. Use ONLY these facts, never invent numbers:\n"
                  f"{json.dumps(facts, ensure_ascii=False, default=str)}")
        return self._stream_say(self._llm_stream(prompt, local=local), self.lang)

    def do_plan(self, it, text):
        minutes = it.minutes or self.cfg["practice"]["default_minutes"]
        scope = "practice" if it.topic in ("", "practice", "music") else "all"
        plan = build_plan(self.store, self.cfg, minutes, scope=scope)
        if not plan:
            return self.k("plan_none")
        facts = as_facts(plan, minutes)
        self.pending_plan = {"items": plan, "ts": time.time()}
        if self.cloud() or not self.cfg["local"].get("skip_offline_explain"):
            try:
                reply = self._explain(facts, "You are Linda, a practical practice coach. Present this session plan "
                                      "(already computed, do not add, remove or reorder items). For each item say "
                                      "what to do, for how long and WHY, using the 'why' reasons.")
                return self._out(reply=reply)
            except Exception as e:
                print("[plan explain failed]", e)
        for i in plan:
            self.k("plan_item", minutes=i.minutes, title=i.title)

    def do_analyze_playing(self, it, text):
        if not self.recorder:
            return self.k("error")
        song = self.store.find_song(it.name, it.action) if it.name else None
        secs = int(self.cfg["analysis"]["record_seconds"])
        self.k("record_start", n=secs)
        self._wait_speech()
        m = analyze(self.recorder(secs), SR, expected=song.melody if song else None,
                    target_bpm=song.target_bpm if song else None)
        if m.get("error"):
            return self.k("record_quiet")
        self.store.save_analysis(song.id if song else None, song.instrument if song else it.action, m)
        facts = {"song": song.title if song else "free playing", "measured": m}
        self._out(reply=self._explain(
            facts, "You are a supportive music teacher. These numbers come from pitch and onset analysis of the "
                   "user's playing (not from your own hearing). Say what went well, the single most important "
                   "thing to fix, and one concrete drill. steadiness_cv is timing irregularity (lower is better), "
                   "tempo_ratio 1.0 means exactly on target tempo. If warnings mention polyphonic audio, say the "
                   "note check may be unreliable."))

    def do_tune(self, it, text):
        if not self.recorder:
            return self.k("error")
        self.k("record_start", n=3)
        self._wait_speech()
        r = tune(self.recorder(3), SR)
        if not r:
            return self.k("tune_none")
        c = r["cents"]
        if abs(c) <= 5:
            self.k("tune_in", note=r["note"])
        else:
            self.k("tune_flat" if c < 0 else "tune_sharp", note=r["note"], n=abs(c))

    def do_stats(self, it, text):
        topic, facts = it.topic.lower(), {}
        if topic in ("", "practice", "music"):
            facts["practice_7_days"] = self.store.practice_summary(7)
            facts["practice_30_days"] = self.store.practice_summary(30)
        if topic in ("", "chess"):
            facts["chess"] = self.store.chess_stats()
        if topic in ("", "driving", "quiz", "theory"):
            facts["driving_quiz_by_set"] = {b: {"answered": n, "correct": c} for b, (n, c) in self.store.quiz_bank_stats().items()}
            facts["driving_results"] = self.store.driving_recent(5)
            td = test_date(self.cfg)
            if td:
                facts["days_to_driving_test"] = (td - datetime.date.today()).days
        if topic in ("", "tasks"):
            facts["open_tasks"] = len(self.store.open_tasks())
        self._out(reply=self._explain(facts, "You are Linda. Summarize the user's progress honestly and "
                                             "mention one thing to improve."))

    def do_log_driving(self, it, text):
        self.store.log_driving(it.topic or "mock_test", it.score, it.total, it.text)
        self.k("driving_logged", score=it.score, total=it.total)

    def log_pending(self, text):
        """'done' / 'finished' right after a plan: log the planned sessions without asking again."""
        p = getattr(self, "pending_plan", None)
        if not p or time.time() - p["ts"] > 4 * 3600 or len(text.split()) > 8 or not DONE.search(text):
            return False
        self.pending_plan, total = None, 0
        for i in p["items"]:
            song = self.store.find_song(i.title, "") if i.title.strip() else None
            self.store.log_practice(i.minutes, song.id if song else None,
                                    song.instrument if song else "", "from plan", None)
            total += i.minutes
        self.k("practice_logged", minutes=total, week=self.store.practice_summary(7)["total_minutes"])
        return True


class QuizMixin:
    """Voice quiz from data/quizzes/*.txt; every answer is logged, weak questions come back more often."""

    def do_quiz(self, it, text):
        banks = load_banks(self.data_dir / "quizzes")
        qs = pick(self.store, banks, it.total or self.cfg["quiz"]["questions_per_session"], it.topic or None) if banks else []
        if not qs:
            return self.k("quiz_none")
        items = []
        for q in qs:
            opts = [q.right] + random.sample(q.wrong, min(3, len(q.wrong)))
            random.shuffle(opts)
            items.append((q, opts, opts.index(q.right) + 1))
        self.quiz = {"items": items, "i": 0, "ok": 0}
        self.k("quiz_start", n=len(items))
        self._quiz_ask()

    def _quiz_ask(self):
        z, ql = self.quiz, self.cfg["quiz"]["language"]
        q, opts, _ = z["items"][z["i"]]
        self.k("quiz_q", i=z["i"] + 1, n=len(z["items"]))
        self.say(q.text, ql)
        for j, o in enumerate(opts, 1):
            self.say(f"{j}. {o}", ql)

    def quiz_turn(self, text):
        z, low, ql = self.quiz, text.lower(), self.cfg["quiz"]["language"]
        if any(w in low for w in QUIZ_STOP):
            self._quiz_finish()
            return True
        if any(w in low for w in QUIZ_REPEAT):
            self._quiz_ask()
            return True
        q, opts, right = z["items"][z["i"]]
        c = parse_choice(text, len(opts))
        if c is None:
            self.k("quiz_repeat")
            return True
        ok = c == right
        self.store.log_quiz(q.bank, q.h, ok)
        if ok:
            z["ok"] += 1
            self.k("quiz_right")
        else:
            self.k("quiz_wrong")
            self.say(q.right, ql)
        z["i"] += 1
        self._quiz_finish() if z["i"] >= len(z["items"]) else self._quiz_ask()
        return True

    def _quiz_finish(self):
        z, self.quiz = self.quiz, None
        if z["i"]:
            self.store.log_driving("theory_quiz", z["ok"], z["i"], ",".join(sorted({q.bank for q, _, _ in z["items"]})))
            self.k("quiz_done", c=z["ok"], n=z["i"])


class TaskMixin:
    def _due(self, it):
        today = datetime.date.today()
        if it.when == "tomorrow":
            return today + datetime.timedelta(days=1)
        if it.when == "today":
            return today
        if it.month and it.day:
            try:
                d = datetime.date(it.year or today.year, it.month, it.day)
                return d if d >= today or it.year else datetime.date(today.year + 1, it.month, it.day)
            except ValueError:
                return None
        return None

    def do_task_add(self, it, text):
        from . import tools
        title = (it.text or it.name or text).strip()
        tools.add_task_once(self, title, self._due(it), it.topic)

    def do_task_list(self, it, text):
        rows = self.store.open_tasks()
        if not rows:
            return self.k("task_none")
        self.k("task_count", n=len(rows))
        for _, title, due, _, _ in rows[:6]:
            self.k("task_item", title=title, due=f", {fmt_date(datetime.date.fromisoformat(due), self.lang)}" if due else "")

    def do_task_done(self, it, text):
        title = self.store.complete_task(it.text or it.name or text)
        self.k("task_done", title=title) if title else self.k("task_notfound")


class DocsMixin:
    """Journal + semantic search over notes / papers / projects / journal."""

    def _journal_append(self, text):
        p = self.data_dir / "journal" / f"{datetime.date.today()}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {datetime.date.today()} {datetime.datetime.now():%H:%M} {text}\n")
        if self.docs:
            threading.Thread(target=self._safe_index, args=(p,), daemon=True).start()

    def _safe_index(self, p):
        try:
            self.docs.index_file(p, "journal")
        except Exception as e:
            print("[index]", e)

    def do_journal_add(self, it, text):
        self._journal_append(it.text or text)
        self.k("journal_saved")

    def do_search_notes(self, it, text):
        if not self.docs:
            return self.k("docs_none")
        q = it.text or text
        hits = self.docs.search(q, self.cfg["docs"]["top_k"], it.topic if it.topic in self.docs.folders else None)
        if not hits:
            return self.k("docs_none")
        ctx = "\n\n".join(f"[{pathlib.Path(h.path).name}] {h.text}" for h in hits)
        prompt = (f"Answer the question using ONLY these excerpts from the user's own notes. Mention the source "
                  f"file name once. If the answer is not in the excerpts, say so. Answer in {LANG_NAME[self.lang]}, "
                  f"2-5 spoken sentences, no markdown.\nQuestion: {q}\n\nExcerpts:\n{ctx}")
        local = self.cfg["docs"]["answer_with"] == "local"
        self._out(reply=self._stream_say(self._llm_stream(prompt, local=local), self.lang))


class ReviewMixin:
    """Scheduler jobs + the evening review."""

    def open_window(self, seconds=300):
        self.active_until = time.time() + seconds

    def job_birthdays(self):
        lang = self.cfg["default_language"]
        for line in self.birthday_lines(lang, horizon=1):
            if self.cfg["birthdays"]["notify"]:
                sysm.notify(self.cfg["name"], line)
            self.say(line, lang)
        self.birthday_telegram(lang)

    def job_morning_briefing(self):
        self.lang = self.cfg["default_language"]
        self.do_morning(Intent(intent="morning"), "")

    def job_practice_reminder(self):
        if self.store.minutes_on(datetime.date.today()) > 0:
            return
        self.lang = self.cfg["default_language"]
        self.k("practice_nudge")
        self.do_plan(Intent(intent="plan", topic="practice"), "")
        self.open_window()

    def job_evening_checkin(self):
        self.lang = self.cfg["default_language"]
        self.k("checkin_intro", minutes=self.store.minutes_on(datetime.date.today()),
               tasks=len(self.store.open_tasks()))
        self.pending_review = time.time() + 900
        self.open_window()

    def _finish_review(self, text):
        if len(text.split()) < 4:
            return self.say("Okay, I'll skip the review for now.")
        summary = focus = ""
        if self.cloud():
            try:
                spec = self.gemini.structured(
                    f"The user gave their evening review: \"{text}\"\nReturn summary (one sentence), "
                    f"tomorrow_focus (one short, most useful focus for tomorrow) and new_tasks (concrete tasks they "
                    f"mention, empty list if none). Use the user's own language.", ReviewSpec, max_out=500)
                summary, focus = spec.summary, spec.tomorrow_focus
                for task in spec.new_tasks:
                    self.store.add_task(task)
            except Exception as e:
                print("[review]", e)
        self.store.save_review(datetime.date.today(), text, summary, focus)
        self._journal_append(f"Evening review: {text}" + (f" | Tomorrow: {focus}" if focus else ""))
        self.k("review_saved", focus=focus) if focus else self.k("journal_saved")
