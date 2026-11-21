"""BPM/key analysis and set-fit maths.

Uses SampleMiner's grid + key detection when installed (Beat This! if present,
librosa otherwise), else a librosa-only fallback. Pure helpers (Camelot, fit)
need nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ----------------------------------------------------------------- Camelot
_NOTES = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5, "F#": 6, "GB": 6,
          "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11}
# pitch class -> Camelot number
_MAJ = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
_MIN = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}


def camelot(key: str | None) -> str | None:
    """'Am' / 'A minor' / 'F#' / 'Gbm' / '8A' -> Camelot code like '8A'; None if unparseable."""
    if not key:
        return None
    k = key.strip().replace("♯", "#").replace("♭", "b")
    m = re.fullmatch(r"(1[0-2]|[1-9])\s*([ABab])", k)
    if m:
        return f"{int(m.group(1))}{m.group(2).upper()}"
    m = re.fullmatch(r"([A-Ga-g])([#b]?)\s*(m|min|minor|maj|major)?", k)
    if not m:
        return None
    pc = _NOTES.get((m.group(1) + m.group(2)).upper())
    if pc is None:
        return None
    minor = (m.group(3) or "").lower() in ("m", "min", "minor")
    return f"{(_MIN if minor else _MAJ)[pc]}{'A' if minor else 'B'}"


def key_relation(a: str | None, b: str | None) -> tuple[int, str]:
    """(rank, label): 0 same, 1 compatible/relative, 2 ±2 steps, 3 clash, 4 unknown."""
    ca, cb = camelot(a), camelot(b)
    if not ca or not cb:
        return 4, ""
    na, la, nb, lb = int(ca[:-1]), ca[-1], int(cb[:-1]), cb[-1]
    step = min((na - nb) % 12, (nb - na) % 12)
    if ca == cb:
        return 0, "same key"
    if la == lb and step == 1:
        return 1, "compatible"
    if na == nb:
        return 1, "relative"
    if la == lb and step == 2:
        return 2, "+2 steps"
    return 3, "clash"


# ----------------------------------------------------------------- BPM fit
FIT_PCT, CLOSE_PCT = 6.0, 10.0


@dataclass(frozen=True)
class Fit:
    cls: str  # fits | close | slower | faster | unknown
    pct: float  # signed % from target at the best factor
    factor: float  # 1, 2 (half-time track played double) or 0.5

    @property
    def label(self) -> str:
        if self.cls == "unknown":
            return ""
        if self.factor != 1 and self.cls in ("fits", "close"):
            return "half-time ×2" if self.factor == 2 else "double-time ×½"
        if abs(self.pct) < 0.5:
            return "on target"
        return f"{self.pct:+.1f}%".replace("-", "−")

    @property
    def guest_label(self) -> str:
        return {"fits": "Fits now", "close": "Close", "slower": "Slower", "faster": "Faster"}.get(self.cls, "")


def fit(bpm: float | None, target: float | None) -> Fit:
    if not bpm or not target:
        return Fit("unknown", 0.0, 1.0)
    best = min((1.0, 2.0, 0.5), key=lambda f: abs(bpm * f - target))
    pct = (bpm * best - target) / target * 100
    if abs(pct) <= FIT_PCT:
        cls = "fits"
    elif abs(pct) <= CLOSE_PCT:
        cls = "close"
    else:
        best, pct = 1.0, (bpm - target) / target * 100
        cls = "slower" if pct < 0 else "faster"
    return Fit(cls, round(pct, 1), best)


# ----------------------------------------------------------------- analysis
class AnalysisUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    bpm: float
    key: str | None  # Camelot
    confidence: float


def available() -> bool:
    try:
        import librosa  # noqa: F401
        return True
    except ImportError:
        return False


def analyze(path: Path, window: float = 90.0) -> Result:
    """BPM + key of an audio file, from a ``window``-second slice of its middle."""
    try:
        import numpy as np
    except ImportError as e:
        raise AnalysisUnavailable("numpy/librosa not installed (pip install mirlos[analysis])") from e
    try:
        from sampleminer import audio as sm_audio, grid as sm_grid, key as sm_key
        from sampleminer.config import FEAT_SR
        mono = sm_audio.analysis_mono(sm_audio.load_audio(path))
        sr = FEAT_SR
    except ImportError:
        sm_audio = sm_grid = sm_key = None
        try:
            import librosa
        except ImportError as e:
            raise AnalysisUnavailable("librosa not installed (pip install mirlos[analysis])") from e
        mono, sr = librosa.load(str(path), sr=22050, mono=True)
    n = int(window * sr)
    if len(mono) > n:
        start = (len(mono) - n) // 2
        mono = mono[start:start + n]
    mono = np.ascontiguousarray(mono, dtype=np.float32)
    if sm_grid is not None:
        g = sm_grid.detect_grid(mono, sr)
        k, kconf = sm_key.estimate_key(sm_key.chroma(mono, sr))
        return Result(round(g.bpm, 1), camelot(k), round(min(g.confidence, 1.0), 2))
    return _librosa_analyze(mono, sr)


_KK_MAJ = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KK_MIN = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_PC = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]


def _librosa_analyze(mono, sr) -> Result:
    import librosa
    import numpy as np

    tempo, _ = librosa.beat.beat_track(y=mono, sr=sr)
    bpm = float(np.atleast_1d(tempo)[0])
    prof = librosa.feature.chroma_cqt(y=mono, sr=sr).mean(axis=1)
    best = max(((np.corrcoef(prof, np.roll(t, i))[0, 1], i, mi)
                for mi, t in ((False, _KK_MAJ), (True, _KK_MIN)) for i in range(12)))
    return Result(round(bpm, 1), camelot(_PC[best[1]] + ("m" if best[2] else "")), 0.5)
