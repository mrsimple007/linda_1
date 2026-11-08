"""Thin wrapper around the Gemini API (google-genai SDK). Key: GEMINI_API_KEY env var."""
import io, wave
import numpy as np
from google import genai
from google.genai import types
from pydantic import BaseModel
import time

class Heard(BaseModel):
    text: str
    language: str


class Gemini:
    def __init__(self, cfg):
        self.client = genai.Client(http_options=types.HttpOptions(timeout=int(cfg.get("timeout_s", 10)) * 1000))
        self.models = cfg.get("models") or [cfg["model"]]     # e.g. [flash, flash-lite]
        self.cool = {}   
        self.max_out = cfg.get("max_output_tokens", 8192)
        self.minimal_thinking = cfg.get("minimal_thinking", True)   # faster first token; auto-disabled if the model rejects it

    def _config(self, system=None, grounding=False, schema=None, max_out=None):
        kw = dict(system_instruction=system, max_output_tokens=max_out or self.max_out)
        if grounding:
            kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if schema:
            kw.update(response_mime_type="application/json", response_schema=schema)
        return types.GenerateContentConfig(**kw)

    def stream(self, contents, system=None, grounding=False):
        for ch in self.client.models.generate_content_stream(
                model=self.models[0], contents=contents, config=self._config(system, grounding)):
            if ch.text:
                yield ch.text

    def stream_tools(self, contents, system, declarations, model=None):
        now, last = time.time(), None
        chain = [m for m in dict.fromkeys(([model] if model else []) + self.models) if self.cool.get(m, 0) < now]
        for m in chain:
            try:
                yield from self._stream_tools_one(contents, system, declarations, m)
                return
            except Exception as e:
                if getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e):
                    self.cool[m] = time.time() + 300      # this model rests 5 min, try the next one
                    last = e
                    continue
                raise
        raise last or RuntimeError("429 RESOURCE_EXHAUSTED: all Gemini models cooling down")

    def _stream_tools_one(self, contents, system, declarations, model):
        """One streaming call with function calling on a single model. Yields raw chunks (Brain reads text + function_call parts)."""
        def cfg(minimal):
            kw = dict(
                system_instruction=system,
                max_output_tokens=self.max_out,
                tools=[types.Tool(function_declarations=declarations)],
                # Brain runs the tools itself, so the SDK must not call them automatically
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            if minimal:
                kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            return types.GenerateContentConfig(**kw)

        started = False
        try:
            for ch in self.client.models.generate_content_stream(
                    model=model, contents=contents, config=cfg(self.minimal_thinking)):
                started = True
                yield ch
            return
        except Exception as e:
            # model rejects the thinking setting -> turn it off and retry once (only if nothing was streamed yet)
            if started or not self.minimal_thinking or "thinking" not in str(e).lower():
                raise
            self.minimal_thinking = False
            print("[gemini] model rejected minimal thinking, disabled")
        yield from self.client.models.generate_content_stream(
            model=model, contents=contents, config=cfg(False))


    def text(self, contents, system=None, grounding=False, max_out=None):
        r = self.client.models.generate_content(
            model=self.models[0], contents=contents, config=self._config(system, grounding, max_out=max_out))
        return r.text or ""

    def structured(self, contents, schema, system=None, max_out=400):
        r = self.client.models.generate_content(
            model=self.models[0], contents=contents,
            config=self._config(system, schema=schema, max_out=max_out))
        if r.parsed is None:
            raise ValueError("Gemini returned no parsable JSON")
        return r.parsed

    def transcribe(self, audio, sr=16000):
        """float32 mono audio -> (text, language en|ru|uz)."""
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        prompt = ("Transcribe this speech exactly as spoken. It is English, Russian or Uzbek. "
                  "Write Russian in Cyrillic and Uzbek in Latin script. The name 'Linda' may "
                  "appear. language must be en, ru or uz. If there is no clear speech, text is empty.")
        h = self.structured([types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"), prompt],
                            Heard, max_out=400)
        return h.text.strip(), h.language