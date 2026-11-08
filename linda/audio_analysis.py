"""Real performance feedback: pitch tracking (pyin) + onset/timing analysis (librosa).
The LLM only EXPLAINS these numbers; it never 'listens'.
Limits: pitch results are reliable for single-note lines (guitar melodies, piano right-hand lines, scales,
tuning). Chords/polyphony are flagged as unreliable; timing (onsets) still works."""
import difflib, re
import numpy as np

SR = 22050


def _norm(note):
    import librosa
    return librosa.midi_to_note(librosa.note_to_midi(note), unicode=False)    # Db4 -> C#4


def _pc(note):
    return re.sub(r"-?\d+$", "", note)


def _onsets(y, sr):
    """Note starts. wait/delta suppress double triggers; the energy filter drops noise-only onsets."""
    import librosa
    on = librosa.onset.onset_detect(y=y, sr=sr, units="time", wait=6, delta=0.2)
    rms = librosa.feature.rms(y=y, hop_length=512)[0]
    keep = []
    for t in on:
        i = int(t * sr / 512)
        if rms[i:i + 8].size and rms[i:i + 8].max() >= 0.1 * rms.max():
            keep.append(t)
    return np.array(keep)


def tune(y, sr=SR):
    """Single sustained note -> {'note','cents','hz'} (cents>0 = sharp) or None."""
    import librosa
    y = librosa.util.normalize(y)
    f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"), sr=sr,
                                 frame_length=4096, resolution=0.01)
    f = f0[voiced & ~np.isnan(f0)]
    if len(f) < 5:
        return None
    midi = float(librosa.hz_to_midi(np.median(f)))
    m = int(round(midi))
    return {"note": librosa.midi_to_note(m, unicode=False), "cents": int(round((midi - m) * 100)),
            "hz": round(float(np.median(f)), 1)}


def analyze(y, sr=SR, expected=None, target_bpm=None, strict_octave=False):
    import librosa
    if len(y) < sr or float(np.sqrt(np.mean(y ** 2))) < 0.002:
        return {"error": "too_quiet"}
    y = librosa.util.normalize(y)
    dur = len(y) / sr
    onsets = _onsets(y, sr)
    f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
                                 sr=sr, frame_length=2048)
    times = librosa.times_like(f0, sr=sr, hop_length=512)
    ok = voiced & ~np.isnan(f0)

    notes, bounds = [], list(onsets) + [dur]
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = (times >= a + 0.04) & (times < min(b, a + 0.6)) & ok          # skip the attack transient
        if seg.sum() < 3:
            continue
        midi = float(librosa.hz_to_midi(np.median(f0[seg])))
        m = int(round(midi))
        notes.append({"t": round(float(a), 2), "note": librosa.midi_to_note(m, unicode=False),
                      "cents": int(round((midi - m) * 100))})

    out = {"duration_s": round(dur, 1), "onsets": int(len(onsets)), "notes_detected": len(notes),
           "played": [n["note"] for n in notes][:32], "warnings": []}
    if len(notes) >= 3:
        out["avg_abs_cents_off"] = int(round(float(np.mean([abs(n["cents"]) for n in notes]))))
    if len(onsets) >= 4:
        iois = np.diff(onsets)
        out["tempo_bpm"] = round(float(60.0 / np.median(iois)), 1)         # assumes one note per beat
        out["steadiness_cv"] = round(float(np.std(iois) / np.mean(iois)), 3)   # lower = steadier
        if target_bpm:
            out["target_bpm"], out["tempo_ratio"] = target_bpm, round(out["tempo_bpm"] / float(target_bpm), 2)
    if len(onsets) >= 3 and ok.mean() < 0.25:
        out["warnings"].append("polyphonic or noisy audio: note detection unreliable, timing still valid")
    if expected:
        exp = [_norm(n) for n in expected.split()]
        got = out["played"]
        a, b = (exp, got) if strict_octave else ([_pc(n) for n in exp], [_pc(n) for n in got])
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        matched = sum(blk.size for blk in sm.get_matching_blocks())
        out["accuracy"] = round(matched / max(1, len(exp)), 2)
        out["compared_by"] = "exact notes" if strict_octave else "note names (octave errors of the tracker ignored)"
        out["mistakes"] = [{"expected": a[i1:i2], "played": b[j1:j2]}
                           for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"][:5]
    return out
