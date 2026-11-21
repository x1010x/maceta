"""Tests para el parser de CHANGELOG.md."""

from pathlib import Path

import maceta


CHANGELOG = """# Changelog

## 0.4.4

- Nueva pestaña Descargas.
- Panel Resultados inline.

## 0.4.3

- Descargas fuera de Biblioteca.
- Preferencia Forzar 320.

## 0.4.2

- Política target 320.
"""


def test_read_since_older_returns_only_newer(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(CHANGELOG)
    out = maceta.read_changelog_entries(p, since_version="0.4.2",
                                         up_to_version="0.4.4")
    assert "0.4.4" in out
    assert "0.4.3" in out
    assert "0.4.2" not in out  # excluida por ser la since


def test_read_no_since_returns_target_only(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(CHANGELOG)
    out = maceta.read_changelog_entries(p, up_to_version="0.4.4")
    assert "0.4.4" in out
    assert "0.4.2" in out
    assert "0.4.3" in out


def test_read_missing_file(tmp_path):
    assert maceta.read_changelog_entries(tmp_path / "nope.md",
                                          up_to_version="1.0.0") == ""


def test_read_empty(tmp_path):
    (tmp_path / "e.md").write_text("just some prose no headings")
    assert maceta.read_changelog_entries(tmp_path / "e.md",
                                          up_to_version="1.0.0") == ""
