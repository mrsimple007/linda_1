"""Speech output. edge-tts (online, has ru/uz/en voices) with macOS `say` as fallback.
Sentences are synthesized ahead of playback so speech flows without gaps."""
import asyncio, concurrent.futures, hashlib, os, pathlib, queue, re, shutil, subprocess, tempfile, threading, time
from .util import online

CACHE = pathlib.Path.home() / "Library" / "Caches" / "linda_tts"     # short phrases: synthesized once, then instant
CACHE_MAX_CHARS = 80
SENTENCE = re.compile(r"(?<=[.!?…])\s+(?=\S)")


def sentences(text, group=300):
    """First sentence alone (sound starts sooner), the rest grouped up to `group` chars (fewer edge-tts calls)."""
    parts = [p.strip() for p in SENTENCE.split(text) if p.strip()]
    if not parts:
        return [text]
    out = [parts[0]]
    for p in parts[1:]:
        if len(out) > 1 and len(out[-1]) + len(p) < group:
            out[-1] += " " + p
        else:
            out.append(p)
    return out


UZ_WORDS = re.compile(
    r"\b\w*[og]'\w*|\b(men|siz|sen|biz|nima|nega|qanday|qachon|qayer\w*|kim|necha\w*|yaxshi|salom|rahmat|"
    r"bugun\w*|ertaga|kecha|kerak|bilan|uchun|emas|yo'?q|ha|va|lekin|endi|hozir|vazifa\w*|reja\w*|"
    r"ber\w*|qil\w*|ayt\w*|ko'?rsat\w*|yoz\w*|eslat\w*|ochib|yop\w*|kun\w*|soat\w*|oy\w*|yil\w*|"
    r"tug'?il\w*|do'?st\w*|aka\w*|uka\w*|opa\w*|ona\w*|ota\w*)\b")
EN_WORDS = re.compile(r"\b(the|and|what|is|are|my|me|you|to|for|of|in|on|with|please|can|how|when|who)\b")


def detect_lang(text, hint="en"):
    letters = [c for c in text if c.isalpha()]
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.3:
        return "ru"
    t = text.lower().replace("ʻ", "'").replace("’", "'").replace("‘", "'")
    if UZ_WORDS.search(t):
        return "uz"
    if hint == "uz" and not EN_WORDS.search(t):          # Latin, no English words, last turn was Uzbek
        return "uz"
    return "en"

class Speaker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.play_q = queue.Queue()
        self.pending, self.quiet_until, self.proc, self.n, self.gen = 0, 0.0, None, 0, 0
        self.lock = threading.Lock()
        self.tmp = tempfile.mkdtemp(prefix="linda_tts_")
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)   # all pieces of a reply at once
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        threading.Thread(target=self._play_loop, daemon=True).start()

    def say(self, text, lang="en"):
        text = text.translate({0x202f: " ", 0xa0: " ", 0x2009: " ", 0x2010: "-", 0x2011: "-", 0x2212: "-"}).strip()
        if not text:
            return
        lang = detect_lang(text, lang)
        voice = self.cfg["voices"][lang]
        for part in sentences(text):         # synthesized in parallel, played in order
            with self.lock:
                self.pending += 1
                gen = self.gen
                self.n += 1
                n = self.n
            fut = self.pool.submit(self._synth, part, voice, gen, n)
            self.play_q.put((part, voice, fut, gen))

    async def _edge(self, text, voice, path):
        import edge_tts
        await edge_tts.Communicate(text, voice, rate=self.cfg["edge_rate"]).save(path)

    @staticmethod
    def _rm(path):
        if path and os.path.exists(path):
            os.remove(path)

    def _cached(self, text, voice):
        if len(text) > CACHE_MAX_CHARS:
            return None
        key = hashlib.sha1(f"{voice}|{self.cfg['edge_rate']}|{text}".encode()).hexdigest()
        return CACHE / f"{key}.mp3"

    def _synth(self, text, voice, gen, n):
        """-> mp3 path, or None for macOS `say`."""
        if self.cfg["engine"] != "edge" or gen != self.gen:
            return None
        t0, path = time.time(), os.path.join(self.tmp, f"{n}.mp3")
        hit = self._cached(text, voice["edge"])
        try:
            if hit and hit.exists():
                shutil.copyfile(hit, path)
                return path
            if not online():
                return None
            asyncio.run(self._edge(text, voice["edge"], path))
            if hit:
                shutil.copyfile(path, hit)
            print(f"[tts] synth {time.time() - t0:.2f}s: {text[:40]!r}")
            return path
        except Exception as e:
            print("[edge-tts failed, using say]", e)
            self._rm(path)
            return None

    def _play_loop(self):
        while True:
            text, voice, fut, gen = self.play_q.get()
            try:
                path = fut.result(timeout=20)
            except Exception:
                path = None
            if gen != self.gen:                  # stop() happened while synthesizing
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