"""Tests para parse_song_filename / suggest_filename / sanitize_filename."""

import maceta


def test_sanitize_replaces_forbidden():
    assert maceta.sanitize_filename("A/B:C") == "A_B_C"


def test_sanitize_collapses_spaces():
    assert maceta.sanitize_filename("  a   b  c  ") == "a b c"


def test_sanitize_empty_falls_back():
    assert maceta.sanitize_filename("") == "sin_nombre"


def test_parse_leading_track_number():
    assert maceta.parse_song_filename("01 - Daft Punk - One More Time") == {
        "artist": "Daft Punk", "title": "One More Time",
    }


def test_parse_dotted_track_number():
    assert maceta.parse_song_filename("07. The Doors - Riders on the Storm") == {
        "artist": "The Doors", "title": "Riders on the Storm",
    }


def test_parse_underscore_separators():
    assert maceta.parse_song_filename("Radiohead_-_Creep") == {
        "artist": "Radiohead", "title": "Creep",
    }


def test_parse_bracketed_junk_stripped():
    assert maceta.parse_song_filename(
        "04 - Blur - Song 2 [Remaster 2012] (Deluxe Edition)"
    ) == {"artist": "Blur", "title": "Song 2"}


def test_parse_bitrate_junk_stripped():
    assert maceta.parse_song_filename(
        "Massive Attack - Teardrop 320 kbps"
    ) == {"artist": "Massive Attack", "title": "Teardrop"}


def test_parse_single_part_is_title_only():
    assert maceta.parse_song_filename("just_a_title") == {
        "artist": "", "title": "just a title",
    }


def test_suggest_uses_artist_and_title():
    assert maceta.suggest_filename("Björk", "Human Behaviour", ".mp3") == \
        "Björk - Human Behaviour.mp3"


def test_suggest_title_only():
    assert maceta.suggest_filename("", "Intro", "flac") == "Intro.flac"


def test_suggest_sanitizes_slashes():
    assert maceta.suggest_filename("AC/DC", "Highway to Hell", "mp3") == \
        "AC_DC - Highway to Hell.mp3"


def test_suggest_empty_falls_back():
    assert maceta.suggest_filename("", "", "mp3").startswith("Unknown")
