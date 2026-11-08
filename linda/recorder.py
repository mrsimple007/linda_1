"""Raw microphone recording for music analysis (the browser handles speech recognition)."""


def _device(name):
    """Index of the first INPUT device whose name contains `name`, else None = system default."""
    import sounddevice as sd
    if not name:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name.lower() in d["name"].lower():
            return i
    print(f"[audio] input device '{name}' not found, using the system default")
    return None


def record_audio(seconds, sr=22050, listener=None, device=""):
    import sounddevice as sd
    device = device or getattr(listener, "music_device", "")
    y = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32", device=_device(device))
    sd.wait()
    if listener:
        listener.drain()
    return y[:, 0]