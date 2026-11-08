"""Microphone capture + voice activity detection, plus a raw recorder for music analysis."""
import collections, queue
import numpy as np
import sounddevice as sd
import webrtcvad

FRAME_MS = 30


def _device(name):
    """Index of the first INPUT device whose name contains `name` (case-insensitive), else None = system default."""
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i
    print(f"[audio] input device '{name}' not found, using the system default")
    return None

MAX_FRAMES = 30_000 // FRAME_MS
MIN_FRAMES = 360 // FRAME_MS


class Listener:
    def __init__(self, cfg, is_speaking):
        self.sr = cfg["sample_rate"]
        self.block = int(self.sr * FRAME_MS / 1000)
        self.vad = webrtcvad.Vad(cfg["vad_aggressiveness"])
        self.silence_frames = cfg["silence_ms"] // FRAME_MS
        self.is_speaking = is_speaking
        self.music_device = cfg.get("music_device") or ""
        self.min_rms = cfg.get("min_rms", 0.004)
        self.paused = False                  # mic mute
        self.barge_in = True                # keep listening while she speaks (headphones!)
        self.heard_while_speaking = False
        self.q = queue.Queue()
        self.stream = sd.RawInputStream(
            samplerate=self.sr, blocksize=self.block, dtype="int16", device=_device(cfg.get("input_device")),
            channels=1, callback=lambda d, *_: self.q.put(bytes(d)))
        self.stream.start()

    def drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    def utterance(self):
        pre = collections.deque(maxlen=10)
        voiced, active, silence, during = [], False, 0, False
        while True:
            frame = self.q.get()
            speaking = self.is_speaking()
            if self.paused or (speaking and not self.barge_in):
                pre.clear(); voiced, active, silence, during = [], False, 0, False
                continue
            speech = self.vad.is_speech(frame, self.sr)
            if not active:
                pre.append(frame)
                if speech:
                    voiced, active, silence, during = list(pre), True, 0, speaking
                continue
            voiced.append(frame)
            during = during or speaking
            silence = 0 if speech else silence + 1
            if silence >= self.silence_frames or len(voiced) >= MAX_FRAMES:
                if len(voiced) >= MIN_FRAMES:
                    pcm = np.frombuffer(b"".join(voiced), dtype=np.int16).astype(np.float32) / 32768.0
                    if float(np.sqrt(np.mean(pcm ** 2))) >= self.min_rms:
                        self.heard_while_speaking = during
                        return pcm
                voiced, active, silence, during = [], False, 0, False
from .recorder import record_audio  # noqa: E402,F401  (kept for the classic main.py)