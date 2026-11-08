"""Offline regression checks for speech recognition cleanup."""
import pathlib, sys
from types import SimpleNamespace
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.modules.setdefault("mlx_whisper", SimpleNamespace(transcribe=None))
from linda.stt import STT


def recognized(text, language="en", segments=None):
    result = {"text": text, "language": language}
    if segments is not None:
        result["segments"] = segments
    return result


def main():
    stt = STT({"model": "unused"})

    calls = []
    stt._run = lambda audio, lang: calls.append(lang) or recognized(
        "bonjour", "fr")
    assert stt.transcribe([]) == ("", "en")
    assert calls == [None]

    stt._run = lambda audio, lang: recognized("We'll see you next time.")
    assert stt.transcribe([]) == ("", "en")

    stt._run = lambda audio, lang: recognized(
        "unclear words", segments=[{"no_speech_prob": 0.9, "avg_logprob": -1.5}])
    assert stt.transcribe([]) == ("", "en")

    calls = []
    def uzbek_retry(audio, lang):
        calls.append(lang)
        return recognized("salom", "tr") if lang is None else recognized("salom", "uz")
    stt._run = uzbek_retry
    assert stt.transcribe([]) == ("salom", "uz")
    assert calls == [None, "uz"]

    print("STT smoke checks passed")


if __name__ == "__main__":
    main()
