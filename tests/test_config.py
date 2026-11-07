"""Settings precedence, validation and private-file tests. No network, no browser."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from spotify_uploader.config import (
    HOME_ENV,
    SHOW_ID_ENV,
    UploaderConfig,
    default_home,
    read_json_object,
    save_show_id,
    validate_show_id,
    write_private_json,
)
from spotify_uploader.errors import ConfigurationError


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch):
    monkeypatch.delenv(HOME_ENV, raising=False)
    monkeypatch.delenv(SHOW_ID_ENV, raising=False)


def _write_config(home: Path, payload: dict) -> None:
    write_private_json(home / "config.json", payload)


def test_show_id_precedence_explicit_over_env_over_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _write_config(home, {"show_id": "from_file"})

    assert UploaderConfig.load(home=home).show_id == "from_file"

    monkeypatch.setenv(SHOW_ID_ENV, "from_env")
    assert UploaderConfig.load(home=home).show_id == "from_env"
    assert UploaderConfig.load(home=home, show_id="explicit").show_id == "explicit"


def test_blank_env_value_falls_back_to_file(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    _write_config(home, {"show_id": "from_file"})
    monkeypatch.setenv(SHOW_ID_ENV, "   ")

    assert UploaderConfig.load(home=home).show_id == "from_file"


def test_home_env_override_is_used_by_default_home_and_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "state"))

    assert default_home() == tmp_path / "state"
    assert UploaderConfig().home == tmp_path / "state"


def test_home_env_override_expands_user(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(HOME_ENV, "~/state")

    assert default_home() == tmp_path / "state"


@pytest.mark.parametrize("bad_show_id", ["bad id!", "show/1", "show.1", "", "   "])
def test_invalid_show_id_is_rejected(bad_show_id: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_show_id(bad_show_id)


def test_invalid_show_id_from_config_file_is_rejected(tmp_path) -> None:
    home = tmp_path / "home"
    _write_config(home, {"show_id": "not valid"})

    with pytest.raises(ConfigurationError):
        UploaderConfig.load(home=home)


def test_invalid_show_id_from_env_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SHOW_ID_ENV, "not valid")

    with pytest.raises(ConfigurationError):
        UploaderConfig.load(home=tmp_path / "home")


def test_valid_show_id_is_stripped() -> None:
    assert validate_show_id("  show_id-1  ") == "show_id-1"


def test_save_show_id_writes_private_file_and_preserves_other_keys(tmp_path) -> None:
    home = tmp_path / "home"
    _write_config(home, {"channel": "chrome", "custom": True})
    config = UploaderConfig(home=home)

    save_show_id(config, "show1")

    payload = read_json_object(home / "config.json")
    assert payload == {"channel": "chrome", "custom": True, "show_id": "show1"}
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_load_reads_channel_and_explicit_values_win(tmp_path) -> None:
    home = tmp_path / "home"
    _write_config(home, {"channel": "chrome"})

    stored = UploaderConfig.load(home=home)
    explicit = UploaderConfig.load(home=home, channel="chromium")

    assert stored.channel == "chrome"
    assert explicit.channel == "chromium"


def test_load_defaults_headless_and_accepts_overrides(tmp_path) -> None:
    home = tmp_path / "home"
    debug_dir = tmp_path / "debug"

    default = UploaderConfig.load(home=home)
    headed = UploaderConfig.load(home=home, headless=False, debug_dir=debug_dir)

    assert default.headless is True
    assert headed.headless is False
    assert headed.debug_dir == debug_dir


def test_load_uses_default_home_when_none_given(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(HOME_ENV, str(tmp_path / "from-env"))

    assert UploaderConfig.load().home == tmp_path / "from-env"


def test_paths_live_under_home(tmp_path) -> None:
    config = UploaderConfig(home=tmp_path / "home")

    assert config.storage_state_path == tmp_path / "home" / "storage_state.json"
    assert config.ledger_path == tmp_path / "home" / "ledger.json"
    assert config.config_path == tmp_path / "home" / "config.json"
    assert config.chrome_profile_dir == tmp_path / "home" / "chrome-profile"


def test_unreadable_config_file_is_ignored(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{not json", encoding="utf-8")

    assert UploaderConfig.load(home=home).show_id is None
