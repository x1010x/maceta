"""Configuración común para los tests: añade el root del proyecto al sys.path
para poder `import maceta` sin instalarlo, y factoriza fixtures de WAVs."""

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sr():
    return 44100


@pytest.fixture
def make_wav(tmp_path, sr):
    """Devuelve una función que crea un WAV en tmp_path.

    Uso:
        p = make_wav("song.wav", seconds=3, freq=440)
    """
    def _make(name, seconds=3.0, freq=440.0, amp=0.3, low_pass_hz=None):
        t = np.arange(int(sr * seconds)) / sr
        y = amp * np.sin(2 * np.pi * freq * t).astype(np.float32)
        # ruido de banda ancha para que haya contenido hasta Nyquist
        y = y + 0.05 * np.random.randn(len(t)).astype(np.float32)
        if low_pass_hz is not None:
            # Filtro pasa-bajos por FFT (simula un fake recodificado)
            spec = np.fft.rfft(y)
            freqs = np.fft.rfftfreq(len(y), 1 / sr)
            spec[freqs > low_pass_hz] = 0
            y = np.fft.irfft(spec, n=len(y)).astype(np.float32)
        p = tmp_path / name
        sf.write(p, y, sr)
        return p
    return _make
