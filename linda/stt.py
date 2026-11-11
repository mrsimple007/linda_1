"""Local multilingual speech-to-text (MLX Whisper only). Returns (text, language).

config stt.language_mode:
  auto : Whisper detects the language (default, old behaviour). Costs a SECOND pass when it
         mislabels Uzbek as Turkish/Kazakh/etc.
  hint : trust the language selected in the UI (the `hint` argument). Always one pass, never retries.
         Fastest, but if you speak another language than the selected one the result is wrong.
"""
import threading, time
import numpy as np
import mlx_whisper

ALLOWED = ("en", "ru", "uz")
TURKIC = {"tr", "kk", "az", "tt", "ky", "tk", "ba"}   # Whisper mislabels Uzbek as these
HALLUCINATIONS = {"", "thank you", "thanks for watching", "you", "bye",
                  "спасибо за просмотр", "продолжение следует"}

HALLUCINATIONS |= {"рецепт смотрите под видео", "подписывайтесь на канал", "продолжение следует..."}


class STT:
    def __init__(self, cfg):
        self.model = cfg["model"]
        self.prompt = cfg.get("prompt", "Linda, Линда.")   # biases spelling of the wake word
        self.mode = cfg.get("language_mode", "auto")        # auto | hint
        self.lock = threading.Lock()
        self._no_ts = True                                  # try without_timestamps; disabled if unsupported

    def warmup(self):
        self._run(np.zeros(16000, dtype=np.float32), "en")

    def _run(self, audio, lang):
        opts = dict(language=lang, initial_prompt=self.prompt, condition_on_previous_text=False,
                    no_speech_threshold=0.6, temperature=0.0, word_timestamps=False, verbose=None)
        with self.lock:
            if self._no_ts:                                 # fewer tokens to decode for short commands
                try:
                    return mlx_whisper.transcribe(audio, path_or_hf_repo=self.model,
                                                  without_timestamps=True, **opts)
                except TypeError:
                    self._no_ts = False
            return mlx_whisper.transcribe(audio, path_or_hf_repo=self.model, **opts)

    def _junk(self, r, text):
        low = text.lower().strip(" .!?,")
        if low in HALLUCINATIONS or "субтитр" in low or low == self.prompt.lower().strip(" .!?,"):
            return True
        if any(x in low for x in ("субтитр", "смотрите под видео", "подписывайтесь", "подписывайся")):
            return True
        segs = r.get("segments") or []
        return bool(segs) and all(s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0
                                  for s in segs)

    def transcribe(self, audio, hint=None):
        t0, passes = time.time(), 1
        if self.mode == "hint" and hint in ALLOWED:
            lang = hint
            r = self._run(audio, lang)
        else:
            r = self._run(audio, None)
            lang = r.get("language", "en")
            if lang not in ALLOWED:
                lang = "uz" if lang in TURKIC else (hint if hint in ALLOWED else "en")
                r = self._run(audio, lang)
                passes = 2
        text = r["text"].strip()
        print(f"[stt] {time.time() - t0:.2f}s lang={lang} passes={passes} len={len(audio) / 16000:.1f}s")
        return ("", lang) if self._junk(r, text) else (text, lang)