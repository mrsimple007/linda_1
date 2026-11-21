"""Local ears: mic -> VAD -> Whisper -> wake word / window logic -> command.
Runs in two threads. Talks to the server through callbacks, so it knows nothing about websockets."""
#linda.ears.py
import re, subprocess, threading, time

STOP = re.compile(r"^[\W_]*(stop|be quiet|shut up|стоп|хватит|замолчи|to'?xta|jim)[\W_]*$", re.I)

ONE_WORD_OK = re.compile(r"^[\W_]*(yes|no|ok|okay|yeah|nope|stop|thanks|please|sure|again|repeat|cancel|done|play|pause|next|"
                         r"weather|news|time|tasks|timer|да|нет|стоп|хорошо|ладно|спасибо|ha|yo'?q|yaxshi|rahmat|xop|bo'?ldi|\d+)[\W_]*$", re.I)


HALLUC = re.compile(r"^[\W_]*(музыка|продолжение следует|субтитры\b.*|редактор субтитров.*|спасибо за просмотр|"
                    r"thanks for watching|thank you for watching|amara\.org.*|dimatorzok.*|♪.*)[\W_]*$", re.I)

def build_wake(pattern):
    # Python's \w is Unicode-aware, so Cyrillic "Линда" works too
    return re.compile(r"^[\W_]*(?:(?:hey|hi|ok|okay|эй|привет)[,\s]+)?(?:" + pattern +
                      r")(?!\w)[,.!?:\s]*(?P<cmd>.*)$", re.I | re.S)


class Ears:
    def __init__(self, listener, stt, speaker, wake_pattern, window, *,
                 on_command, on_interrupt, notify, in_session=lambda: False, lang=lambda: "en"):
        self.l, self.stt, self.sp = listener, stt, speaker
        self.wake, self.window = build_wake(wake_pattern), window
        self.on_command, self.on_interrupt, self.notify = on_command, on_interrupt, notify
        self.in_session, self.lang = in_session, lang
        self.window_until, self.t_cmd = 0.0, 0.0
        self.thinking=False

    def start(self):
        threading.Thread(target=self.stt.warmup, daemon=True).start()
        threading.Thread(target=self._listen, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def window_open(self):
        return time.time() < max(self.window_until, self.sp.quiet_until + self.window) or self.in_session()

    def _beep(self):
        subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"])

    def _command(self, text, lang):
        self.window_until = time.time() + self.window
        self.notify({"type": "state", "state": "thinking"})
        self.notify({"type": "transcript", "text": "You: " + text})
        self.on_command(text, lang)

    def _listen(self):
        while True:
            audio = self.l.utterance()
            self.t_cmd = time.perf_counter()
            during = self.l.heard_while_speaking
            text, lang = self.stt.transcribe(audio, hint=self.lang())
            if not text:
                continue
            if getattr(self.sp, "is_echo", lambda t: False)(text):
                continue
            if HALLUC.match(text) or len(re.sub(r"\W", "", text)) < 2:
                print(f"[ears] ignored noise: {text!r}")
                continue
            if len(re.findall(r"\w+", text)) == 1 and not ONE_WORD_OK.match(text) and not self.wake.match(text):
                print(f"[ears] ignored single word: {text!r}")
                continue

            if len(text) > 40 and len(set(text.lower())) * 8 < len(text):      # "вечечечечече..." loops
                print("[ears] ignored garbage loop")
                continue
            self.notify({"type": "transcript", "text": text})
            m = self.wake.match(text)
            rest = m.group("cmd").strip() if m else ""
            if during or self.sp.is_speaking():        # only her name or "stop" counts (probably her own voice)
                if m or STOP.match(text):
                    self.on_interrupt()
                    if rest: self._command(rest, lang)
                    else: self._beep()
                continue
            if m:
                if rest: self._command(rest, lang)
                else:
                    self._beep(); self.window_until = time.time() + self.window
                    self.notify({"type": "state", "state": "listening"})
            elif self.window_open():
                self._command(text, lang)

    def _watch(self):
        """Pushes speaking/listening/idle transitions to the UI and reports latency."""
        last = None
        while True:
            time.sleep(0.1)
            s = "speaking" if self.sp.is_speaking() else ("listening" if self.window_open() else "idle")
            if s == last or (s != "speaking" and last == "thinking"):
                continue
            if s == "speaking" and last != "speaking" and self.t_cmd:
                self.notify({"type": "latency_ms", "ms": round((time.perf_counter() - self.t_cmd) * 1000)})
                self.t_cmd = 0
            self.notify({"type": "state", "state": s}); last = s

            if self.thinking and s != "speaking":
                continue