"""Birthdays (save, list, greet) and sending things to your Telegram."""
import datetime
from linda.i18n import LANG_NAME, LANGS, fmt_date, join_names, t


class MessagingMixin:
    """Birthdays and Telegram sends; every send goes through agent.run_action (confirmation)."""

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
