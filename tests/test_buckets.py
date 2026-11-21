"""Tests para el clasificador de estado (row_bucket + display_verdict)."""

import maceta


def _row(**kw):
    base = {
        "analyzed": True,
        "verdict": "",
        "slsk_status": "",
        "bitrate_kbps": 320,
    }
    base.update(kw)
    return base


# ---------- row_bucket ----------

def test_bucket_not_analyzed():
    assert maceta.row_bucket(_row(analyzed=False)) == "sin_analizar"


def test_bucket_ok_native():
    assert maceta.row_bucket(_row(verdict="OK")) == "listas"


def test_bucket_lossless_native():
    assert maceta.row_bucket(_row(verdict="LOSSLESS")) == "listas"


def test_bucket_fake_untouched():
    assert maceta.row_bucket(_row(verdict="FAKE")) == "fakes"


def test_bucket_mismatch_download_needs_review():
    # "no coincide" manda sobre el veredicto: es trabajo pendiente
    # aunque la canción en sí esté bien.
    r = _row(verdict="OK", slsk_status="✗ no coincide (sim 0.41)")
    assert maceta.row_bucket(r) == "a_revisar"


def test_bucket_fake_but_substituted_goes_to_sustituidas():
    r = _row(verdict="FAKE", slsk_status="✓ sustituido (sim 0.98)")
    assert maceta.row_bucket(r) == "sustituidas"


def test_bucket_converted_mp3_goes_to_sustituidas():
    r = _row(verdict="FAKE", slsk_status="✓ MP3 320 (convertido)")
    assert maceta.row_bucket(r) == "sustituidas"


def test_bucket_searching_is_en_proceso():
    r = _row(verdict="FAKE", slsk_status="buscando: Daft Punk")
    assert maceta.row_bucket(r) == "en_proceso"


def test_bucket_downloading_is_en_proceso():
    r = _row(verdict="FAKE", slsk_status="↓ FLAC de user123")
    assert maceta.row_bucket(r) == "en_proceso"


def test_bucket_no_coincide_needs_review():
    r = _row(verdict="FAKE", slsk_status="✗ no coincide (sim 0.42)")
    assert maceta.row_bucket(r) == "a_revisar"


def test_bucket_error_verdict():
    assert maceta.row_bucket(_row(verdict="ERROR")) == "errores"


# ---------- display_verdict ----------

def test_display_ok_shows_bitrate():
    assert maceta.display_verdict(_row(verdict="OK", bitrate_kbps=320)) \
        == "✅ 320 OK"


def test_display_lossless():
    assert maceta.display_verdict(_row(verdict="LOSSLESS")) == "🎵 LOSSLESS"


def test_display_fake():
    assert maceta.display_verdict(_row(verdict="FAKE")) == "❌ FAKE"


def test_display_substituted_wins_over_verdict():
    r = _row(verdict="FAKE", slsk_status="✓ sustituido (sim 0.9)")
    assert maceta.display_verdict(r) == "⬇ Sustituida"


def test_display_mp3_320_conversion():
    r = _row(verdict="FAKE", slsk_status="✓ MP3 320 (convertido)")
    assert maceta.display_verdict(r) == "⬇ MP3 320"


def test_display_downloading():
    r = _row(verdict="FAKE", slsk_status="↓ FLAC de user123")
    assert maceta.display_verdict(r) == "⏳ En cola"


def test_display_searching():
    r = _row(verdict="FAKE", slsk_status="buscando: query")
    assert maceta.display_verdict(r) == "🔎 Buscando"


def test_display_no_coincide():
    r = _row(verdict="FAKE", slsk_status="✗ no coincide (sim 0.42)")
    assert maceta.display_verdict(r) == "⚠ Revisar match"


def test_display_not_analyzed():
    assert maceta.display_verdict(_row(analyzed=False, verdict="")) \
        == "❓ sin analizar"
