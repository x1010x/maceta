"""Tests para verify_download_quality — chequeo tras descargar de Soulseek."""

import maceta


def test_verify_flac_accepts_lossless(make_wav):
    # WAV cuenta como lossless en la política.
    p = make_wav("song.wav", seconds=2.0)
    ok, reason = maceta.verify_download_quality(str(p))
    assert ok
    assert "WAV" in reason or "lossless" in reason.lower()


def test_verify_rejects_bad_path(tmp_path):
    ok, reason = maceta.verify_download_quality(str(tmp_path / "nope.mp3"))
    assert not ok
