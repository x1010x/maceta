"""Tests para la lógica de búsqueda y elección de resultados en Soulseek.

Todo puro — no toca la red ni slskd. Le pasamos dicts que emulan las
respuestas de la API.
"""

import maceta


def _resp(user, ext, size, br=None, slot=True, queue=0, speed=200000):
    return {
        "username": user,
        "hasFreeUploadSlot": slot,
        "uploadSpeed": speed,
        "queueLength": queue,
        "files": [{
            "filename": f"x\\{user}\\track.{ext}",
            "size": size,
            "extension": ext,
            "bitRate": br,
        }],
    }


# ---------- is_acceptable ----------

def test_is_acceptable_flac_yes():
    assert maceta.is_acceptable({"filename": "a.flac", "extension": "flac",
                                  "size": 30_000_000})


def test_is_acceptable_mp3_320_yes():
    assert maceta.is_acceptable({"filename": "a.mp3", "extension": "mp3",
                                  "size": 10_000_000, "bitRate": 320})


def test_is_acceptable_mp3_128_no():
    assert not maceta.is_acceptable({"filename": "a.mp3", "extension": "mp3",
                                      "size": 4_000_000, "bitRate": 128})


def test_is_acceptable_mp3_192_yes_with_lower_min():
    # Con min_bitrate=192 aceptamos 192 (política oportunista).
    f = {"filename": "a.mp3", "extension": "mp3",
         "size": 5_000_000, "bitRate": 192}
    assert maceta.is_acceptable(f, min_bitrate=192)
    assert not maceta.is_acceptable(f, min_bitrate=320)


def test_is_acceptable_effective_bitrate_fallback():
    # sin bitRate reportado pero size×8/length ≈ 320 kbps → OK
    f = {"filename": "a.mp3", "extension": "mp3",
         "size": 320 * 1000 * 200 // 8,   # 320 kbps × 200 s
         "length": 200, "bitRate": None}
    assert maceta.is_acceptable(f)


def test_is_acceptable_giant_file_rejected():
    assert not maceta.is_acceptable({"filename": "a.flac", "extension": "flac",
                                      "size": 2 * 1024 * 1024 * 1024})


# ---------- choose_best ----------

def test_choose_best_prefers_flac_over_mp3():
    resps = [_resp("a", "mp3", 9_000_000, br=320),
             _resp("b", "flac", 30_000_000)]
    _, f = maceta.choose_best(resps, "any query")
    assert f["extension"] == "flac"


def test_choose_best_prefers_larger_within_same_codec():
    # dos MP3 declarados 320: gana el más grande (el chico miente)
    resps = [_resp("a", "mp3", 3_000_000, br=320),
             _resp("b", "mp3", 9_000_000, br=320)]
    resp, _ = maceta.choose_best(resps, "any")
    assert resp["username"] == "b"


def test_choose_best_returns_none_when_no_acceptable():
    resps = [_resp("a", "mp3", 4_000_000, br=128)]
    assert maceta.choose_best(resps, "any") is None


def test_choose_best_prefers_available_over_larger_waiting():
    # Un peer con slot libre pero MP3 320 gana a un FLAC más grande
    # pero con cola de 40. Regla dura: disponible primero.
    resps = [
        _resp("a", "mp3", 8_000_000, br=320, slot=True, queue=0),
        _resp("b", "flac", 30_000_000, slot=False, queue=40),
    ]
    resp, _ = maceta.choose_best(resps, "any")
    assert resp["username"] == "a"


def test_choose_best_falls_back_to_waiting_if_no_available():
    # Ningún peer disponible → toma el mejor del grupo waiting.
    resps = [
        _resp("a", "mp3", 8_000_000, br=320, slot=False, queue=15),
        _resp("b", "flac", 30_000_000, slot=False, queue=40),
    ]
    resp, f = maceta.choose_best(resps, "any")
    # FLAC gana a MP3 en su grupo (codec > tamaño > score)
    assert f["extension"] == "flac"
    assert resp["username"] == "b"


def test_choose_best_available_zero_queue_no_slot():
    # `hasFreeUploadSlot=False` pero `queueLength=0` cuenta como disponible.
    resps = [
        _resp("a", "mp3", 8_000_000, br=320, slot=False, queue=0),
        _resp("b", "mp3", 9_000_000, br=320, slot=False, queue=5),
    ]
    resp, _ = maceta.choose_best(resps, "any")
    assert resp["username"] == "a"


# ---------- build_query / _clean_keep_brackets ----------

def test_clean_strips_leading_number():
    assert maceta._clean_keep_brackets(
        "007 - Jackson 5 - I Want You Back") == "Jackson 5 I Want You Back"


def test_clean_drops_bitrate_tag_but_keeps_version_words():
    # Lo de dentro de los paréntesis suele ser el remix o la versión, y
    # eso sí importa para buscar; la etiqueta de calidad no.
    assert maceta._clean_keep_brackets(
        "Song [320 kbps] (Remaster 2019)") == "Song Remaster 2019"


def test_build_query_raw_is_the_filename():
    row = {"path": "/tmp/04 - Daft Punk - Discovery (2001) [320].mp3"}
    assert maceta.build_query(row, "raw") == \
        "04 - Daft Punk - Discovery (2001) [320]"


def test_build_query_clean_drops_track_number_and_bitrate():
    row = {"path": "/tmp/04 - Daft Punk - Discovery (2001) [320].mp3"}
    assert maceta.build_query(row, "clean") == "Daft Punk Discovery 2001"
