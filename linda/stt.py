"""Local multilingual speech-to-text (MLX Whisper). Returns (text, language)."""
import threading
import numpy as np
import mlx_whisper

ALLOWED = ("en", "ru", "uz")
TURKIC = {"tr", "kk", "az", "tt", "ky", "tk", "ba"}   # Whisper mislabels Uzbek as these
HALLUCINATIONS = {"", "thank you", "thanks for watching", "you", "bye",
                  "спасибо за просмотр", "продолжение следует"}


class STT:
    def __init__(self, cfg):
        self.model = cfg["model"]
        self.prompt = cfg.get("prompt", "Linda, Линда.")   # biases spelling of the wake word
        self.lock = threading.Lock()

    def warmup(self):
        self._run(np.zeros(16000, dtype=np.float32), "en")

    def _run(self, audio, lang):
        with self.lock:
            return mlx_whisper.transcribe(
                audio, path_or_hf_repo=self.model, language=lang, initial_prompt=self.prompt,
                condition_on_previous_text=False, no_speech_threshold=0.6, verbose=None)

    def _junk(self, r, text):
        low = text.lower().strip(" .!?,")
        if low in HALLUCINATIONS or "субтитр" in low or low == self.prompt.lower().strip(" .!?,"):
            return True
        segs = r.get("segments") or []
        return bool(segs) and all(s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0
                                  for s in segs)

    def transcribe(self, audio, hint=None):
        r = self._run(audio, None)
        lang = r.get("language", "en")
        if lang not in ALLOWED:
            lang = "uz" if lang in TURKIC else (hint if hint in ALLOWED else "en")
            r = self._run(audio, lang)
        text = r["text"].strip()
        return ("", lang) if self._junk(r, text) else (text, lang)