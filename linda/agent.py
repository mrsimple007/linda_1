"""Linda's core: speaking, the cloud check, the LLM stream, confirmations for risky actions, and run()
(one command: confirmations -> chess/quiz/review sessions -> router -> do_<intent>).
Feature commands live in mixins: skills/{chess_game, calendar_skill, mac, messaging, conversation}.py
and domains.py (practice, quiz, tasks, docs, reviews)."""
#linda.agent.py
import pathlib, re, time
from dataclasses import dataclass
from typing import Any, Callable
import ollama
from linda import safety
from linda.i18n import LANGS, t
from linda.router import Intent
from linda.util import online
from linda import fastpath, tools  # noqa: F401  (fastpath: kept as before)
from linda.domains import DocsMixin, PracticeMixin, QuizMixin, ReviewMixin, TaskMixin
from linda.skills.calendar_skill import CalendarMixin, EventSpec  # noqa: F401  (EventSpec: tests import it from here)
from linda.skills.chess_game import (BoardWindow, CHESS_HINT, CHESS_STOP, CHESS_UNDO,  # noqa: F401
                                     ChessMixin)
from linda.skills.conversation import CHAT_SYSTEM, ConversationMixin  # noqa: F401
from linda.skills.mac import FILE_WRAPPER, MacMixin  # noqa: F401
from linda.skills.messaging import MessagingMixin


@dataclass
class PendingAction:
    tool: str
    arguments: dict
    callback: Callable[[], Any]
    tier: int
    lang: str
    created: float


class Agent(ChessMixin, CalendarMixin, MacMixin, MessagingMixin, ConversationMixin,
            PracticeMixin, QuizMixin, TaskMixin, DocsMixin, ReviewMixin):
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
