"""Executes intents. The small local model decides WHAT; Gemini does the heavy work."""
#linda.agent.py
import datetime, pathlib, re, time
from dataclasses import dataclass
from typing import Any, Callable
import ollama
from google.genai import types
from pydantic import BaseModel
from . import safety, system as sysm
from .chess_game import ChessGame, speak_move
from .domains import DocsMixin, PracticeMixin, QuizMixin, ReviewMixin, TaskMixin
from .files import UnsupportedFile, latest_download, prepare_input, save_text, split_title
from .i18n import LANG_NAME, LANGS, WHEN, day_label, fmt_date, fmt_today, join_names, t
from .router import Intent
from .util import online
from .import fastpath, tools, memory_files



CHAT_SYSTEM = """You are {name}, {user}'s warm, playful personal assistant and friend (never stiff or corporate).
Reply in the same language the user spoke (English, Russian, or Uzbek in Latin script).
Your answer is read aloud: usually 1-4 short sentences, no markdown, bullets, emojis or URLs, unless the user asks for detail.
Use Google Search for anything recent. Today is {today}.{notes}"""

FILE_WRAPPER = """Task: {task}

Work on the content provided above (original file name: {orig}).
Output format: the first line must be exactly "TITLE: <short descriptive file name based on the document's main topic or its original name, no extension>", then a blank line, then the result as plain text only. No markdown fences, no commentary."""

CHESS_STOP = ("stop", "resign", "quit", "стоп", "хватит", "сдаюсь", "to'xta", "toxta", "tugat")
CHESS_UNDO = ("undo", "take back", "назад", "верни ход", "qaytar", "orqaga")
CHESS_HINT = ("hint", "best move", "подсказ", "maslahat")


@dataclass
class PendingAction:
    tool: str
    arguments: dict
    callback: Callable[[], Any]
    tier: int
    lang: str
    created: float


class BoardWindow:
    def __init__(self):
        # Initialize your window properties here
        pass


class EventSpec(BaseModel):
    title: str
    start: str
    duration_minutes: int = 60


class Agent(PracticeMixin, QuizMixin, TaskMixin, DocsMixin, ReviewMixin):
    def __init__(self, cfg, store, speaker, gemini, router, cal=None, tg=None, docs=None, recorder=None):
        self.cfg, self.store, self.speaker = cfg, store, speaker
        self.gemini, self.router, self.cal, self.tg = gemini, router, cal, tg
        self.docs, self.recorder = docs, recorder
        self.data_dir = pathlib.Path(cfg.get('data_dir', 'data')).expanduser()
        self.quiz, self.pending_review, self.active_until = None, 0, 0.0
        self.lang, self.history, self.chess, self.bw = "en", [], None, None
        self.last_reply = self.last_file = self.last_event = None
        self._produced = False
        self.pending_actions = []

    # ---------- helpers ----------
    def say(self, text, lang=None):
        self.speaker.say(text, lang or self.lang)

    def k(self, key, lang=None, **kw):
        lang = lang or self.lang
        self.speaker.say(t(key, lang, **kw), lang)

    def cloud(self):
        return bool(self.gemini) and online()

    def _wait_speech(self):
        while self.speaker.is_speaking():
            time.sleep(0.1)

    def _llm_stream(self, prompt, system=None, local=False):
        """Providers first (Groq/Mistral), then Gemini, then local. local=True (private notes) never leaves the machine."""
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        if not local:
            for p in getattr(self, "providers", []):
                if p.ready():
                    try:
                        r = p.client.chat.completions.create(model=p.model, messages=msgs, max_tokens=1500)
                        return iter([r.choices[0].message.content or ""])
                    except Exception as e:
                        p.cool(e)
                        print(f"[{p.name}] narrate failed:", repr(e)[:120])
        if self.cloud() and not local:
            return self.gemini.stream(prompt, system)
        return (c.message.content for c in ollama.chat(
            model=self.cfg["local"]["model"], messages=msgs, stream=True,
            keep_alive=self.cfg["local"]["keep_alive"]))

    def _out(self, reply=None, file=None):
        """Remember what this request produced (for 'send that to my Telegram')."""
        self.last_reply, self.last_file, self._produced = reply, file, True

    def _stream_say(self, chunks, lang):
        buf = full = ""
        for c in chunks:
            full += c
            parts = re.split(r"(?<=[.!?…])\s+|\n+", buf + c)
            for p in parts[:-1]:
                self.say(p, lang)
            buf = parts[-1]
        if buf.strip():
            self.say(buf, lang)
        print(f"[llm answer] {full.strip()[:400]}")
        return full

    def run_action(self, tool: str, arguments: dict, callback: Callable[[], Any]) -> bool:
        """Audit an action and require confirmation before sensitive operations."""
        tier = safety.permission_tier(tool, arguments)
        if tier >= 2:
            action = PendingAction(tool, dict(arguments), callback, tier, self.lang, time.time())
            self.store.audit_action(tool, arguments, "confirmation_required")
            self.pending_actions.append(action)
            phrase = safety.confirmation_phrase(tier, self.lang)
            prompts = {
                "en": f"I need your confirmation before I do that. Say '{phrase}' to proceed, or say 'cancel'.",
                "ru": f"Нужно подтверждение. Скажите «{phrase}», чтобы продолжить, или «отмена».",
                "uz": f"Tasdiqlashingiz kerak. Davom etish uchun «{phrase}» deng yoki «bekor» deng.",
            }
            self.say(prompts[self.lang])
            return False
        self._execute_action(tool, arguments, callback)
        return True

    def _execute_action(self, tool, arguments, callback, confirmed_by=None) -> Any:
        try:
            result = callback()
        except Exception:
            self.store.audit_action(tool, arguments, "failed", confirmed_by)
            raise
        self.store.audit_action(tool, arguments, "completed", confirmed_by)
        return result

    def _cancel_pending(self, reason):
        pending, self.pending_actions = self.pending_actions, []
        for action in pending:
            self.store.audit_action(action.tool, action.arguments, reason)

    def cancel_pending_actions(self, reason="cancelled_by_user"):
        self._cancel_pending(reason)

    def handle_confirmation(self, text: str) -> bool:
        """Consume confirmation turns before they can be routed as ordinary commands."""
        if not self.pending_actions:
            return False
        action = self.pending_actions[0]
        if time.time() - action.created > 90:
            self._cancel_pending("confirmation_expired")
            self.say({"en": "That confirmation expired. Please ask again.",
                      "ru": "Время подтверждения истекло. Повторите запрос.",
                      "uz": "Tasdiqlash vaqti tugadi. Iltimos, qayta so'rang."}[self.lang])
            return True
        if safety.is_confirmation(text, action.tier, action.lang):
            self.pending_actions.pop(0)
            self._execute_action(action.tool, action.arguments, action.callback,
                                 confirmed_by="user")
            if self.pending_actions:
                nxt = self.pending_actions[0]
                nxt.created = time.time()
                phrase = safety.confirmation_phrase(nxt.tier, nxt.lang)
                self.say({"en": f"Next, say '{phrase}' to confirm the following action.",
                          "ru": f"Для следующего действия скажите «{phrase}».",
                          "uz": f"Keyingi amal uchun «{phrase}» deng."}[nxt.lang], nxt.lang)
            return True
        if safety.is_cancellation(text):
            self._cancel_pending("cancelled_by_user")
            self.say({"en": "Cancelled.", "ru": "Отменено.", "uz": "Bekor qilindi."}[self.lang])
            return True
        if safety.normalize_confirmation(text) in {"yes", "да", "ha"}:
            phrase = safety.confirmation_phrase(action.tier, action.lang)
            self.say({"en": f"Please say '{phrase}' exactly to confirm.",
                      "ru": f"Для подтверждения точно скажите «{phrase}».",
                      "uz": f"Tasdiqlash uchun aynan «{phrase}» deng."}[self.lang])
            return True
        self._cancel_pending("superseded_by_new_request")
        self.say({"en": "I didn't get the confirmation, so I cancelled that action. Please repeat your request if you still want it.",
                  "ru": "Подтверждение не получено, действие отменено. Повторите запрос, если он ещё нужен.",
                  "uz": "Tasdiq olmadim, amal bekor qilindi. Kerak bo'lsa, so'rovingizni qaytadan ayting."}[self.lang])
        return True

    # ---------- entry ----------
    def run(self, text, lang="en"):
        self.lang = lang if lang in LANGS else "en"
        self._produced = False
        try:
            if self.handle_confirmation(text):
                return
            if self.chess and self.chess_turn(text):
                return
            if self.quiz and self.quiz_turn(text):
                return
            if self.pending_review and time.time() < self.pending_review:
                self.pending_review = 0
                return self._finish_review(text)
            if self.log_pending(text):
                return
            it = self.router.route(text)
            print(f"[intent] {it.model_dump(exclude_defaults=True)}")
            if not safety.request_is_supported(it.intent, text):
                self.store.audit_action(it.intent, it.model_dump(exclude_defaults=True),
                                        "rejected_unsubstantiated")
                self.say({"en": "I may have misheard. Please repeat what you want me to do.",
                          "ru": "Возможно, я ослышалась. Повторите, пожалуйста, что нужно сделать.",
                          "uz": "Noto'g'ri eshitgan bo'lishim mumkin. Iltimos, nima qilishim kerakligini qaytaring."}
                         [self.lang])
                return
            if it.intent == "process_download" and it.preset and it.preset not in self.cfg.get("presets", {}):
                print(f"[router] unknown download preset {it.preset!r}; ignoring it")
                it.preset = ""
            gaps = tools.missing(it)
            if gaps:
                print(f"[router] {it.intent} missing {gaps} -> chat")
                it = Intent(intent="chat")
            args = it.model_dump(exclude_defaults=True)
            callback = lambda: getattr(self, "do_" + it.intent, self.do_chat)(it, text)
            if it.intent == "telegram_send":
                callback()
            else:
                self.run_action(it.intent, args, callback)
            if it.telegram and self._produced and it.intent != "telegram_send":
                self.send_last()
        except Exception as e:
            print("[error]", repr(e))
            self.k("error")

    # ---------- simple local skills ----------
    def do_time(self, it, text):
        self.k("time", time=datetime.datetime.now().strftime("%H:%M"))

    def do_battery(self, it, text):
        n = sysm.battery_percent()
        self.k("battery", n=n) if n is not None else self.k("error")

    def do_open_app(self, it, text):
        if it.app and sysm.open_app(it.app):
            self.k("opened", app=it.app)
        else:
            self.k("error")

    def do_volume(self, it, text):
        if it.level < 0:
            return self.k("error")
        self.k("volume", n=sysm.set_volume(it.level))

    def do_music(self, it, text):
        res = sysm.music(it.action, it.text)
        status, detail = res if isinstance(res, tuple) else (("ok", it.action) if res else ("fail", ""))
        L = {"played": {"en": f"Playing {detail}. Enjoy!", "ru": f"Включаю {detail}. Приятного!", "uz": f"{detail} qo'yaman. Rohat qiling!"},
             "opened": {"en": f"I opened Spotify's search for {detail}, but I can't press play myself without Spotify keys.",
                        "ru": f"Открыла поиск Spotify по запросу {detail}, но сама включить не могу без ключей Spotify.",
                        "uz": f"Spotify qidiruvini ochdim, lekin Spotify kalitlarisiz o'zim qo'sha olmayman."},
             "ok": {"en": "Done!", "ru": "Готово!", "uz": "Tayyor!"}}
        if status in L:
            self.say(L[status][self.lang])
        else:
            self.say({"en": "I couldn't control Spotify. Is the app installed?", "ru": "Не получилось управлять Spotify.",
                      "uz": "Spotify'ni boshqara olmadim."}[self.lang])
            
    def do_mac(self, it, text):
        a = it.action.lower()
        if a == "screenshot":
            sysm.screenshot()
            return self.k("screenshot")
        if a in ("mute", "unmute"):
            sysm.mute(a == "mute")
        elif a == "lock":
            self.k("done")
            sysm.lock()
            return
        else:
            return self.k("error")
        self.k("done")

    def do_timer(self, it, text):
        n, lang, label = max(1, it.seconds), self.lang, it.text.strip()
        sysm.start_timer(n, lambda: self.k("timer_done_label", lang, label=label) if label
                         else self.k("timer_done", lang))
        self.k("timer_set", n=n)

    def do_remember(self, it, text):
        self.store.add_note(it.text or text)
        self.k("note_saved")

    def do_clipboard(self, it, text):
        clip = sysm.clipboard().strip()
        if not clip:
            return self.k("clip_empty")
        if not self.cloud():
            return self.k("need_internet")
        prompt = (f"{it.instruction or 'Summarize this briefly'}.\nAnswer in {LANG_NAME[self.lang]}, in plain "
                  f"spoken sentences, no markdown.\n\n---\n{clip[:20000]}")
        self._out(reply=self._stream_say(self.gemini.stream(prompt), self.lang))

    # ---------- birthdays ----------
    def do_add_birthday(self, it, text):
        try:
            datetime.date(2000, it.month, it.day)
        except ValueError:
            return self.k("bday_bad")
        name, rel = it.name.strip(), it.relation
        if name.lower() in ("me", "my", "myself", "i", "mine"):
            name, rel = self.cfg.get("user_name") or "Me", "me"
        if not name:
            return self.k("bday_bad")
        old, year, updated = self.store.find_birthday(name), it.year or None, False
        if old:
            same = any(b.name == old.name and (b.date.month, b.date.day) == (it.month, it.day) for b in self.store.upcoming(366))
            if same and getattr(old, "year", None) == year:
                return self.say("That birthday is already saved.")
            self.store.delete_birthday(old.name)               # a new date for a known person is a correction
            name, rel, year, updated = old.name, rel or getattr(old, "relation", ""), year or getattr(old, "year", None), True
        self.store.add_birthday(name, it.day, it.month, year, rel, it.language if it.language in LANGS else "")
        d = fmt_date(datetime.date(2000, it.month, it.day), self.lang)
        if updated:
            self.say({"en": f"Updated: {name}'s birthday is now {d}.", "ru": f"Обновила: у {name} день рождения {d}.",
                      "uz": f"Yangiladim: {name}ning tug'ilgan kuni endi {d}."}[self.lang])
        else:
            self.k("bday_saved", name=name, date=d)

    def do_delete_birthday(self, it, text):
        ok = self.store.delete_birthday(it.name)
        self.k("bday_deleted" if ok else "bday_notfound", name=it.name)

    def do_list_birthdays(self, it, text):
        items = self.store.upcoming(366)[:5]
        if not items:
            return self.k("bday_none")
        for b in items:
            self.k("bday_item", name=b.name, date=fmt_date(b.date, self.lang), n=b.n)

    def birthday_lines(self, lang, horizon=1):
        items = self.store.upcoming(horizon)
        today = [b.name for b in items if b.n == 0]
        tomorrow = [b.name for b in items if b.n == 1]
        lines = []
        if today:
            lines.append(t("bday_today", lang, names=join_names(today, lang)))
        if tomorrow:
            lines.append(t("bday_tomorrow", lang, names=join_names(tomorrow, lang)))
        for b in (x for x in items if x.n > 1):
            lines.append(t("bday_item", lang, name=b.name, date=fmt_date(b.date, lang), n=b.n))
        return lines

    def make_greeting(self, name, relation, lang, age=None):
        prompt = (f"Write a warm, personal birthday greeting from me to my {relation or 'dear friend or relative'} "
                  f"{name}{f', who is turning {age}' if age else ''}. 2-4 sentences, sincere, natural for native "
                  f"speakers, at most one emoji, no placeholders or brackets, no sign-off name. "
                  f"Write ONLY in {LANG_NAME[lang]}. Output just the greeting text.")
        return self.gemini.text(prompt, max_out=500).strip()

    def birthday_telegram(self, ui_lang):
        """Do not send messages from a background job without an interactive confirmation."""
        if self.tg and self.store.upcoming(0):
            self.say({
                "en": "I won't send birthday messages automatically. Ask me to draft one when you're ready.",
                "ru": "Я не отправляю поздравления автоматически. Попросите меня подготовить его, когда будете готовы.",
                "uz": "Tug'ilgan kun tabriklarini avtomatik yubormayman. Kerak bo'lganda tayyorlab berishimni so'rang.",
            }[ui_lang], ui_lang)
        return 0

    def do_birthday_greeting(self, it, text):
        if not self.cloud():
            return self.k("need_internet")
        p = self.store.find_birthday(it.name)
        name = p.name if p else it.name
        relation = (p.relation if p else "") or it.relation
        age = datetime.date.today().year - p.year if p and p.year else None
        lang = (it.language if it.language in LANGS
                else (p.lang if p and p.lang in LANGS else self.cfg["birthdays"]["greeting_language"]))
        greeting = self.make_greeting(name, relation, lang, age)
        self._out(reply=greeting)
        if self.tg:
            self.send_last(greeting)
        else:
            self.k("tg_missing")
            self.say(greeting, lang)

    # ---------- telegram ----------
def send_last(self, text=""):
    if not self.tg:
        self.k("tg_missing")
        return {"status": "failed", "error": "telegram not configured"}
    if text:
        args = {"kind": "message", "text": text}
        send = lambda: self.tg.send(text)
    elif self.last_file:
        args = {"kind": "file", "path": str(self.last_file)}
        path = self.last_file
        send = lambda: self.tg.send_file(path)
    elif self.last_reply:
        reply = self.last_reply
        args = {"kind": "message", "text": reply}
        send = lambda: self.tg.send(reply)
    else:
        self.k("tg_nothing")
        return {"status": "failed", "error": "nothing to send"}

    try:
        done = self.run_action("telegram_send", args, send)
    except Exception as e:
        print("[telegram]", e)
        self.k("tg_fail")
        return {"status": "failed", "error": str(e)[:100]}
    if done:
        self.say({"en": "Sent to your Telegram.",
                  "ru": "Отправила в Telegram.",
                  "uz": "Telegramga yubordim."}[self.lang])
        return {"status": "sent"}
    return {"status": "waiting_for_confirmation"}

def do_telegram_send(self, it, text):
    return self.send_last(it.text.strip())

    # ---------- calendar ----------
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

    # ---------- news / weather / morning / chat (Gemini) ----------
    def do_news(self, it, text):
        if not self.cloud():
            return self.k("need_internet")
        lang = it.language if it.language in LANGS else self.lang
        self.k("working", lang)
        topic = f" about {it.topic}" if it.topic else ""
        prompt = (f"Use Google Search. Give a spoken news briefing{topic}: the 5 most important world "
                  f"news stories of the last 24 hours, one or two short sentences each. Neutral tone. "
                  f"Write ONLY in {LANG_NAME[lang]}. Plain sentences: no markdown, bullets, numbering, "
                  f"URLs or source lists. Start directly with the first story. Today is {datetime.date.today()}.")
        self._out(reply=self._stream_say(self.gemini.stream(prompt, grounding=True), lang))

    def do_weather(self, it, text):
        if not self.cloud():
            return self.k("need_internet")
        city = it.topic or self.cfg.get("city", "")
        self.k("working")
        prompt = (f"Use Google Search. Weather in {city} right now and for today: temperature, conditions, "
                  f"high and low, chance of rain. Two short spoken sentences. Write ONLY in "
                  f"{LANG_NAME[self.lang]}. No markdown, no URLs.")
        self._out(reply=self._stream_say(self.gemini.stream(prompt, grounding=True), self.lang))

    def do_morning(self, it, text):
        user = f", {self.cfg['user_name']}" if self.cfg.get("user_name") else ""
        self.k("good_morning", user=user, date=fmt_today(datetime.date.today(), self.lang))
        for line in self.birthday_lines(self.lang, horizon=7):
            self.say(line)
        if self.cal:
            self._agenda("today")
        if self.cfg["morning"].get("weather"):
            self.do_weather(Intent(intent="weather"), text)
        if self.cfg["morning"].get("news"):
            self.do_news(Intent(intent="news"), text)
    def _chat_system(self):
        notes = self.store.notes()
        block = ("\nThings the user asked you to remember:\n- " + "\n- ".join(notes)) if notes else ""
        return CHAT_SYSTEM.format(name=self.cfg["name"], user=self.cfg.get("user_name") or "the user",
                                  today=fmt_today(datetime.date.today(), "en"), notes=block) \
               + "\n\n" + memory_files.context_block(rules=False) + "\n\n" + getattr(self, "facts", lambda: "")()
    def do_chat(self, it, text):
        self.history.append(("user", text))
        system, recent = self._chat_system(), self.history[-12:]
        if self.cloud():
            contents = [types.Content(role=r, parts=[types.Part.from_text(text=m)]) for r, m in recent]
            chunks = self.gemini.stream(contents, system, self.cfg["gemini"]["grounding_for_chat"])
        else:                                              # offline: the small local model answers
            msgs = [{"role": "system", "content": system}] + [
                {"role": "assistant" if r == "model" else "user", "content": m} for r, m in recent]
            chunks = (c.message.content for c in ollama.chat(
                model=self.cfg["local"]["model"], messages=msgs, stream=True, think=False,
                keep_alive=self.cfg["local"]["keep_alive"]))
        reply = self._stream_say(chunks, self.lang)
        self.history = (self.history + [("model", reply)])[-24:]
        self._out(reply=reply)

    # ---------- latest download -> Gemini -> .txt ----------
    def do_process_download(self, it, text):
        if not self.cloud():
            return self.k("need_internet")
        folder = self.cfg["downloads_dir"]
        path = latest_download(folder)
        if not path:
            return self.k("no_file")
        presets = self.cfg.get("presets", {})
        task = presets[it.preset]["prompt"] if it.preset in presets else (it.instruction or text)
        self.k("file_working", name=path.name)
        try:
            parts = prepare_input(path)
        except UnsupportedFile:
            return self.k("file_bad")
        out = self.gemini.text([*parts, FILE_WRAPPER.format(task=task, orig=path.name)])
        title, body = split_title(out, path.stem)
        saved = save_text(folder, title, body)
        sysm.reveal(saved)
        self._out(file=saved)
        self.k("file_done", name=saved.name)

    # ---------- chess ----------
    def _show(self):
        g = self.chess
        g.show()
        if self.bw:
            last = g.board.peek().uci() if g.board.move_stack else None
            status = "Your move" if g.board.turn == g.user_white else "Linda is thinking…"
            if g.board.is_check():
                status += " – check!"
            if g.board.is_game_over():
                status = f"Game over: {g.board.result()}"
            self.bw.update(g.board.fen(), last, status, flip=not g.user_white)

    def do_chess(self, it, text):
        if self.chess:
            self.chess.close()
        c = self.cfg["chess"]
        white = it.color != "black"
        self.chess = ChessGame(white, c["skill_level"], c["think_time"])
        if c.get("window", True):
            self.bw = self.bw or BoardWindow()
            self.bw.open()
        self._show()
        self.k("chess_white" if white else "chess_black")
        if not white:
            self._engine_turn()

    def _log_chess(self, result):
        g = self.chess
        if not g or len(g.board.move_stack) < 4:
            return
        import chess.pgn
        pgn = str(chess.pgn.Game.from_board(g.board))
        self.store.log_chess(g.user_white, self.cfg["chess"]["skill_level"], result, len(g.board.move_stack), pgn)

    def chess_turn(self, text):
        low, g = text.lower(), self.chess
        if any(w in low for w in CHESS_STOP):
            self._log_chess("abandoned")
            g.close()
            self.chess = None
            if self.bw:
                self.bw.close()
            self.k("chess_stop")
            return True
        if any(w in low for w in CHESS_UNDO):
            if len(g.board.move_stack) >= 2:
                g.board.pop(); g.board.pop()
                self._show()
                self.k("chess_undo")
            else:
                self.k("chess_illegal")
            return True
        if any(w in low for w in CHESS_HINT):
            self.k("chess_hint", move=speak_move(g.board, g.engine_move(), self.lang))
            return True
        mv = g.parse(text) or self._gemini_move(text)
        if mv is None:
            self.k("chess_illegal")
            return True
        g.board.push(mv)
        self._show()
        if not self._finished():
            self._engine_turn()
        return True

    def _gemini_move(self, text):
        """Fallback for phrases like 'конь эф три': Gemini picks one of the legal moves."""
        if not self.cloud():
            return None
        g = self.chess
        legal = g.legal_san()
        prompt = (f"Chess position (FEN): {g.board.fen()}\nLegal moves (SAN): {', '.join(legal)}\n"
                  f"The player said (English/Russian/Uzbek, may be misheard): \"{text}\"\n"
                  "Reply with exactly one legal move from the list, or NONE.")
        try:
            ans = self.gemini.text(prompt, max_out=20).strip().strip(".")
        except Exception:
            return None
        return g.move_from_san(ans) if ans in legal else None

    def _engine_turn(self):
        g = self.chess
        mv = g.engine_move()
        desc = speak_move(g.board, mv, self.lang)
        g.board.push(mv)
        self._show()
        self.k("chess_my_move", move=desc)
        if not self._finished() and g.board.is_check():
            self.k("chess_check")

    def _finished(self):
        g = self.chess
        if not g.board.is_game_over():
            return False
        w = g.board.outcome().winner
        key = "chess_draw" if w is None else ("chess_win_user" if w == g.user_white else "chess_win_me")
        self.k(key)
        self._log_chess({"chess_draw": "draw", "chess_win_user": "win"}.get(key, "loss"))
        g.close()
        self.chess = None                                  # board window stays open until you close it
        return True
