"""Talking: remember a note, news, weather, the morning briefing, and free chat (Gemini, or the local model offline)."""
import datetime
import ollama
from google.genai import types
from linda.i18n import LANG_NAME, LANGS, fmt_today
from linda.memory import memory_files
from linda.router import Intent

CHAT_SYSTEM = """You are {name}, {user}'s warm, playful personal assistant and friend (never stiff or corporate).
Reply in the same language the user spoke (English, Russian, or Uzbek in Latin script).
Your answer is read aloud: usually 1-4 short sentences, no markdown, bullets, emojis or URLs, unless the user asks for detail.
Use Google Search for anything recent. Today is {today}.{notes}"""


class ConversationMixin:
    """Spoken answers from the cloud model: chat, news, weather, morning."""

    def do_remember(self, it, text):
        self.store.add_note(it.text or text)
        self.k("note_saved")

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
