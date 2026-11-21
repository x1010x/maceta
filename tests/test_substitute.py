"""Tests para sustitución/archivado de ficheros y matching por chroma."""

from pathlib import Path

import pytest

import maceta


def test_archive_path_for_mirrors_original(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "ARCHIVE_DIR", tmp_path / "archivo")
    p = "/Users/x/Music/A/Song.mp3"
    arch = maceta.archive_path_for(p)
    assert str(arch).endswith("archivo/Users/x/Music/A/Song.mp3")


def test_substitute_file_moves_and_replaces(tmp_path, monkeypatch, make_wav):
    monkeypatch.setattr(maceta, "ARCHIVE_DIR", tmp_path / "archivo")
    orig = make_wav("original.wav", seconds=1.5)
    downloaded = make_wav("downloaded.flac", seconds=1.5)
    # Renombrar el "downloaded" a .flac para probar cambio de extensión
    flac = orig.parent / "downloaded_real.flac"
    downloaded.rename(flac)

    new_path = maceta.substitute_file(str(orig), str(flac))

    assert new_path.exists()
    assert new_path.suffix == ".flac"
    assert new_path.stem == "original"
    assert not orig.exists()  # se ha movido
    # Original debe vivir en archivo/
    mirrored = maceta.archive_path_for(str(orig))
    assert mirrored.exists()


def test_substitute_file_missing_original(tmp_path):
    with pytest.raises(FileNotFoundError):
        maceta.substitute_file(str(tmp_path / "nope.mp3"),
                               str(tmp_path / "also_nope.mp3"))


def test_songs_match_identical(make_wav):
    p = make_wav("same.wav", seconds=4.0, freq=440)
    match, sim, _ = maceta.songs_match(str(p), str(p))
    assert match
    assert sim == pytest.approx(1.0, abs=0.01)


def test_songs_match_different(make_wav):
    # A (440) vs F (349) — pitch classes distintas, chroma distinta.
    # OJO: no usar octavas (440 y 880) — misma nota, chroma idéntica.
    a = make_wav("a.wav", seconds=4.0, freq=440.0, amp=0.5)
    b = make_wav("b.wav", seconds=4.0, freq=349.23, amp=0.5)
    match, _, _ = maceta.songs_match(str(a), str(b))
    assert not match


def test_songs_match_duration_mismatch(make_wav):
    short = make_wav("short.wav", seconds=2.0)
    long = make_wav("long.wav", seconds=20.0)
    match, _, reason = maceta.songs_match(str(short), str(long))
    assert not match
    assert "duración" in reason
