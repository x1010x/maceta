"""Tests para la persistencia de config (JSON en ~/.maceta_config.json)."""

import json

import maceta


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    maceta.save_config({"slskd_url": "http://x", "slskd_api_key": "abc"})
    cfg = maceta.load_config()
    assert cfg["slskd_url"] == "http://x"
    assert cfg["slskd_api_key"] == "abc"


def test_load_config_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "no.json")
    assert maceta.load_config() == {}


def test_load_config_malformed(tmp_path, monkeypatch):
    p = tmp_path / "bad.json"
    p.write_text("{ not valid")
    monkeypatch.setattr(maceta, "CONFIG_PATH", p)
    assert maceta.load_config() == {}


def test_colors_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    c = maceta.load_colors()
    # Todas las claves del spec deben tener valor
    for key, _label, default in maceta.COLOR_SPEC:
        assert c[key] == default


def test_colors_override(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    maceta.save_colors({"tree_bg": "#123456"})
    c = maceta.load_colors()
    assert c["tree_bg"] == "#123456"
