"""Speech output. edge-tts (online, has ru/uz/en voices) with macOS `say` as fallback.
Sentences are synthesized ahead of playback so speech flows without gaps."""
import asyncio, os, queue, re, subprocess, tempfile, threading, time
from .util import online


def detect_lang(text, hint="en"):
    letters = [c for c in text if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3:
        return "ru"
    t = text.lower().replace("ʻ", "'").replace("’", "'").replace("‘", "'")
    if re.search(r"\b\w*[og]'\w*|\b(men|siz|nima|qanday|yaxshi|salom|rahmat|bugun|kerak|bilan|uchun)\b", t):
        return "uz"
    return "en"

class Speaker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.q, self.play_q = queue.Queue(), queue.Queue()
        self.pending, self.quiet_until, self.proc, self.n, self.gen = 0, 0.0, None, 0, 0
        self.lock = threading.Lock()
        self.tmp = tempfile.mkdtemp(prefix="linda_tts_")
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def say(self, text, lang="en"):
        text = text.translate({0x202f: " ", 0xa0: " ", 0x2009: " ", 0x2010: "-", 0x2011: "-", 0x2212: "-"}).strip()
        if not text:
            return
        with self.lock:
            self.pending += 1
            gen = self.gen
        self.q.put((text, detect_lang(text, lang), gen))

    async def _edge(self, text, voice, path):
        import edge_tts
        await edge_tts.Communicate(text, voice, rate=self.cfg["edge_rate"]).save(path)

    @staticmethod
    def _rm(path):
        if path and os.path.exists(path):
            os.remove(path)

    def _synth_loop(self):
        t0 = time.time()
        asyncio.run(self._edge(text, voice["edge"], path))
        print(f"[tts] synth {time.time() - t0:.2f}s: {text[:30]!r}")
        while True:
            text, lang, gen = self.q.get()
            if gen != self.gen:
                continue
            voice, path = self.cfg["voices"][lang], None
            if self.cfg["engine"] == "edge" and online():
                try:
                    self.n += 1
                    path = os.path.join(self.tmp, f"{self.n}.mp3")
                    asyncio.run(self._edge(text, voice["edge"], path))
                except Exception as e:
                    print("[edge-tts failed, using say]", e)
                    self._rm(path); path = None
            if gen != self.gen:          # stop() happened while synthesizing
                self._rm(path); continue
            self.play_q.put((text, voice, path, gen))

    def _play_loop(self):
        while True:
            text, voice, path, gen = self.play_q.get()
            if gen != self.gen:
                self._rm(path); continue
            cmd = (["afplay", path] if path else
                   ["say", "-v", voice["say"], "-r", str(self.cfg["say_rate"]), "--", text])
            try:
                self.proc = subprocess.Popen(cmd)
                self.proc.wait()
            finally:
                self._rm(path)
                with self.lock:
                    if gen == self.gen:  # stop() already zeroed the counter otherwise
                        self.pending -= 1
                    self.quiet_until = time.time() + 0.4

    def stop(self):
        with self.lock:
            self.gen += 1
            self.pending = 0
            self.quiet_until = time.time() + 0.4
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def is_speaking(self):
        return self.pending > 0 or time.time() < self.quiet_until