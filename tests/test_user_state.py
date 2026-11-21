"""Tests para load_user_state / save_user_state."""

import maceta


def test_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    us = maceta.load_user_state()
    assert us["last_folder"] == ""
    assert us["ask_quality"] is False
    assert us["substitute"] is False
    assert us["auto_flac_to_mp3"] is False
    assert us["force_320"] is False
    assert us["active_bucket"] == "todas"


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    maceta.save_user_state({
        "last_folder": "/Users/x/Music",
        "substitute": True,
        "active_bucket": "a_revisar",
    })
    us = maceta.load_user_state()
    assert us["last_folder"] == "/Users/x/Music"
    assert us["substitute"] is True
    assert us["active_bucket"] == "a_revisar"
    # Los defaults se conservan para lo no tocado
    assert us["force_320"] is False


def test_save_merges_with_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(maceta, "CONFIG_PATH", tmp_path / "cfg.json")
    # Otras claves de config no relacionadas se conservan
    maceta.save_config({"slskd_url": "http://x"})
    maceta.save_user_state({"substitute": True})
    cfg = maceta.load_config()
    assert cfg["slskd_url"] == "http://x"
    assert cfg["user_state"]["substitute"] is True
