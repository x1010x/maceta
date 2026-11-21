"""Tests para el mecanismo de auto-update.

Sin tocar la red: mockeamos `requests.get` para retornar contenido controlado.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import maceta


# ---------- helpers puros ----------

def test_parse_version_from_source():
    src = 'VERSION = "1.2.3"\n\nother = 1\n'
    assert maceta._parse_version(src) == "1.2.3"


def test_parse_version_single_quotes():
    src = "VERSION = '0.9.0'\n"
    assert maceta._parse_version(src) == "0.9.0"


def test_parse_version_missing_returns_empty():
    assert maceta._parse_version("no version here") == ""


def test_version_tuple_ordering():
    assert maceta._version_tuple("0.4.10") > maceta._version_tuple("0.4.9")
    assert maceta._version_tuple("1.0.0") > maceta._version_tuple("0.99.99")
    assert maceta._version_tuple("0.4.0") == maceta._version_tuple("0.4.0")


def test_version_tuple_non_numeric_component():
    assert maceta._version_tuple("0.4.dev1") == (0, 4, 0)


# ---------- check_for_update (mocked) ----------

def _mock_response(text, status=200):
    r = MagicMock()
    r.text = text
    r.status_code = status
    r.raise_for_status = MagicMock()
    return r


def test_check_for_update_detects_newer():
    remote = 'VERSION = "999.0.0"\n# rest of file\n'
    with patch("maceta.requests.get", return_value=_mock_response(remote)):
        info = maceta.check_for_update("dummy_token")
    assert info["remote_version"] == "999.0.0"
    assert info["is_newer"] is True
    assert info["content"] == remote


def test_check_for_update_same_version():
    remote = f'VERSION = "{maceta.VERSION}"\n'
    with patch("maceta.requests.get", return_value=_mock_response(remote)):
        info = maceta.check_for_update("dummy_token")
    assert info["is_newer"] is False
    assert info["content"] == ""


def test_check_for_update_bad_remote():
    with patch("maceta.requests.get",
               return_value=_mock_response("garbage without VERSION")):
        with pytest.raises(RuntimeError, match="VERSION"):
            maceta.check_for_update("dummy_token")


# ---------- apply_update ----------

def test_apply_update_backs_up_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "ARCHIVE_DIR", tmp_path / "archivo")
    target = tmp_path / "maceta.py"
    target.write_text('VERSION = "0.1.0"\n# old\n')
    new_content = 'VERSION = "0.2.0"\n# new\nx = 1\n'

    backup = maceta.apply_update(new_content, target=target)

    assert target.read_text() == new_content
    assert backup.exists()
    assert "0.1.0" in backup.read_text()
    # Backup path bajo archivo/ con la versión antigua en el nombre
    assert (tmp_path / "archivo") in backup.parents


def test_apply_update_rejects_broken_syntax(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "ARCHIVE_DIR", tmp_path / "archivo")
    target = tmp_path / "maceta.py"
    original = 'VERSION = "0.1.0"\n# original\n'
    target.write_text(original)
    broken = 'VERSION = "0.2.0"\ndef :::'  # sintaxis inválida

    with pytest.raises(SyntaxError):
        maceta.apply_update(broken, target=target)

    # Fichero original intacto
    assert target.read_text() == original
