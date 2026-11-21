"""Tests del pipeline de análisis: metadatos, espectro, veredicto."""

import numpy as np
import pytest

import maceta


def test_get_metadata_wav(make_wav):
    p = make_wav("t.wav", seconds=2.0)
    m = maceta.get_metadata(str(p))
    assert m["codec"] == "WAV"
    assert m["sample_rate"] == 44100
    assert 1.9 < m["duration_s"] < 2.1


def test_compute_spectrum_shape(sr):
    y = np.random.randn(sr * 2).astype(np.float32)
    freqs, psd_db = maceta.compute_spectrum(y, sr, n_fft=4096)
    assert freqs.shape == psd_db.shape
    assert freqs[0] == 0
    assert freqs[-1] == pytest.approx(sr / 2)


def test_find_cutoff_broadband_reaches_nyquist(sr):
    y = np.random.randn(sr * 2).astype(np.float32)
    freqs, psd_db = maceta.compute_spectrum(y, sr)
    cut = maceta.find_cutoff(freqs, psd_db)
    assert cut > sr / 2 - 500  # broadband → hasta cerca de Nyquist


def test_find_cutoff_lowpass_at_16k(sr):
    # Señal broadband cortada a 16 kHz (simula fake)
    y = np.random.randn(sr * 3).astype(np.float32)
    spec = np.fft.rfft(y)
    freqs = np.fft.rfftfreq(len(y), 1 / sr)
    spec[freqs > 16000] = 0
    y = np.fft.irfft(spec, n=len(y)).astype(np.float32)
    f, psd = maceta.compute_spectrum(y, sr)
    cut = maceta.find_cutoff(f, psd)
    assert 15000 < cut < 17000


def test_judge_lossless_short_circuits():
    v, _ = maceta.judge(None, None, "FLAC")
    assert v == "LOSSLESS"


def test_judge_ok_full_bandwidth():
    v, detail = maceta.judge(320, 20500, "MP3")
    assert v == "OK"
    assert "20500 Hz" in detail


def test_judge_ok_middle_band_is_now_ok():
    # Antes SOSPECHOSO; tras la refactor de agosto siempre es OK.
    v, _ = maceta.judge(320, 19000, "MP3")
    assert v == "OK"


def test_judge_fake_low_cutoff():
    v, detail = maceta.judge(320, 15000, "MP3")
    assert v == "FAKE"
    assert "calidad real" in detail
    assert "declara 320" in detail


def test_judge_below_target_but_real_is_ok():
    # El veredicto lo decide SOLO el espectro. Un 192 con corte a 19 kHz
    # suena bien de verdad, así que es OK — pero el detalle avisa de que
    # declara menos del target por si quieres mejorarlo igualmente.
    v, detail = maceta.judge(192, 19000, "MP3")
    assert v == "OK"
    assert "declara 192" in detail


def test_judge_128_with_matching_cutoff_is_ok():
    # Un 128 honesto (corte justo en el suelo) no es un transcode: es un
    # fichero pequeño pero sincero. FAKE se reserva para los que mienten.
    v, detail = maceta.judge(128, 16000, "MP3")
    assert v == "OK"
    assert "declara 128" in detail


def test_judge_fake_floor_is_tunable():
    # El suelo es configurable desde el panel de opciones.
    original = maceta.FAKE_CUTOFF_FLOOR_HZ
    try:
        maceta.FAKE_CUTOFF_FLOOR_HZ = 17000
        assert maceta.judge(320, 16500, "MP3")[0] == "FAKE"
        maceta.FAKE_CUTOFF_FLOOR_HZ = 16000
        assert maceta.judge(320, 16500, "MP3")[0] == "OK"
    finally:
        maceta.FAKE_CUTOFF_FLOOR_HZ = original


def test_analyze_row_end_to_end(make_wav):
    p = make_wav("full.wav", seconds=4.0)
    row = maceta.scan_row(str(p))
    row = maceta.analyze_row(row)
    assert row["analyzed"] is True
    assert row["verdict"] == "LOSSLESS"
    assert row["cutoff_hz"] > 15000
