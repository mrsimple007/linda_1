"""Bridge between Linda's synchronous code (worker threads) and the browser (asyncio WebSocket)."""
import asyncio, base64, contextvars, re, threading, time
from .tts import detect_lang

CHANNEL = contextvars.ContextVar("linda_channel", default=None)   # ordered-speech channel of a tool call
TURN = contextvars.ContextVar("linda_turn", default=None)          # cancel Event of the current turn


class Timer:
    """Per-stage latency of one command."""
    def __init__(self, route=""):
        self.t0, self.route, self.marks = time.perf_counter(), route, {}

    def mark(self, name):
        self.marks.setdefault(name, time.perf_counter() - self.t0)

    def line(self, client_ms=None):
        parts = [f"{k} {v * 1000:.0f}ms" for k, v in self.marks.items()]
        if client_ms is not None:
            parts.append(f"browser final->audio {client_ms:.0f}ms")
        return f"[latency] {self.route or '?'} | " + " | ".join(parts)


class Sequencer:
    """Parallel tool calls must still speak in call order: only the head channel speaks live,
    the others are buffered until it finishes."""
    def __init__(self, sink):
        self.sink, self.chans, self.lock = sink, [], threading.RLock()

    def new(self):
        ch = {"buf": [], "done": False}
        with self.lock:
            self.chans.append(ch)
        return ch

    def put(self, ch, item):
        with self.lock:
            if self.chans and self.chans[0] is ch:
                self.sink(item)
            else:
                ch["buf"].append(item)

    def finish(self, ch):
        with self.lock:
            ch["done"] = True
            while self.chans and self.chans[0]["done"]:
                self.chans.pop(0)
                if self.chans:
                    for it in self.chans[0]["buf"]:
                        self.sink(it)
                    self.chans[0]["buf"].clear()


class Client:
    def __init__(self, ws):
        self.ws, self.q = ws, asyncio.Queue()

    async def writer(self):
        while True:
            msg = await self.q.get()
            if msg is None:
                return
            await self.ws.send_json(msg)


class Bridge:
    def __init__(self, cfg, synth=None):
        self.cfg, self.synth = cfg, synth or self._edge_synth
        self.loop, self.tts_queue, self.clients = None, None, set()
        self.pending, self.gen, self.lock = 0, 0, threading.Lock()
        self.client_playing, self.quiet_until = False, 0.0
        self.timer = self.last_timer = None

    def start(self, loop):
        self.loop, self.tts_queue = loop, asyncio.Queue()
        self.sender = loop.create_task(self._tts_sender())

    def shutdown(self):
        if self.loop:
            self.sender.cancel()

    # ---- thread-safe sending ----
    def send(self, msg):
        if self.loop is None:
            return

        def _put():
            for c in list(self.clients):
                c.q.put_nowait(msg)
        self.loop.call_soon_threadsafe(_put)

    def _dec(self):
        with self.lock:
            self.pending = max(0, self.pending - 1)

    # ---- speech ----
    def speak(self, text, lang):
        if self.loop is None:
            return
        with self.lock:
            self.pending += 1
        self.loop.call_soon_threadsafe(self._enqueue, text, lang, self.gen)

    def _enqueue(self, text, lang, gen):
        fut = self.loop.create_task(self.synth(text, lang))
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())     # never leave errors unretrieved
        self.tts_queue.put_nowait((gen, fut, text, lang))

    async def _tts_sender(self):
        while True:
            gen, fut, text, lang = await self.tts_queue.get()
            try:
                msg = await fut
            except Exception as e:
                print("[tts] edge-tts failed, browser voice instead:", e)
                msg = {"type": "speak_local", "text": text, "lang": lang}
            if gen == self.gen:
                self.quiet_until = time.time() + 0.8
                if self.timer:
                    self.timer.mark("tts_audio")
                for c in list(self.clients):
                    c.q.put_nowait(msg)
            self._dec()

    def stop_audio(self):
        if self.loop is None:
            return
        self.gen += 1

        def _do():
            while not self.tts_queue.empty():
                self.tts_queue.get_nowait()
                self._dec()
            for c in list(self.clients):
                c.q.put_nowait({"type": "stop_audio"})
        self.loop.call_soon_threadsafe(_do)

    async def _edge_synth(self, text, lang):
        t = self.cfg["tts"]
        if t.get("engine") == "browser":
            return {"type": "speak_local", "text": text, "lang": lang}
        import edge_tts
        buf = bytearray()
        async for ch in edge_tts.Communicate(text, t["voices"][lang]["edge"], rate=t["edge_rate"]).stream():
            if ch["type"] == "audio":
                buf += ch["data"]
        if not buf:
            raise RuntimeError("no audio returned")
        return {"type": "audio", "mime": "audio/mpeg", "b64": base64.b64encode(bytes(buf)).decode(),
                "text": text, "lang": lang}


class WebSpeaker:
    """Same interface as tts.Speaker, so every existing skill speaks through the browser unchanged."""
    def __init__(self, bridge, cfg):
        self.bridge, self.cfg, self.turn_log, self._fb = bridge, cfg, [], None
        self.seq = Sequencer(self._emit)

    def say(self, text, lang="en"):
        ev = TURN.get()
        if ev is not None and ev.is_set():           # this turn was interrupted: stay silent
            return
        text = re.sub(r"https?://\S+|[*#`_>~^=]", "", text).strip()
        if not text:
            return
        item = (text, detect_lang(text, lang))
        self.turn_log.append(text)
        ch = CHANNEL.get()
        self._emit(item) if ch is None else self.seq.put(ch, item)

    def _emit(self, item):
        text, lang = item
        if self.bridge.timer:
            self.bridge.timer.mark("first_sentence")
        if self.bridge.clients:
            self.bridge.speak(text, lang)
        else:                                         # no browser tab open: speak on the Mac itself
            if self._fb is None:
                from .tts import Speaker
                self._fb = Speaker(self.cfg["tts"])
            self._fb.say(text, lang)

    def stop(self):
        self.bridge.stop_audio()
        if self._fb:
            self._fb.stop()

    def is_speaking(self):
        b = self.bridge
        return (b.pending > 0 or b.client_playing or time.time() < b.quiet_until
                or bool(self._fb and self._fb.is_speaking()))


class WebBoard:
    """Replaces the Tk window: the chess board lives in the browser."""
    def __init__(self, bridge):
        self.b = bridge

    def open(self):
        pass

    def update(self, fen, last=None, status="", flip=False):
        self.b.send({"type": "chess", "fen": fen, "last": last, "status": status, "flip": flip,
                     "active": not status.startswith("Game over")})

    def close(self):
        self.b.send({"type": "chess", "fen": None})