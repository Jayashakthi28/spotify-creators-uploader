"""Session normalisation, privacy and file-permission tests. No browser, no network."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from spotify_uploader.errors import ConfigurationError
from spotify_uploader.session import (
    KEY_COOKIE,
    SessionSummary,
    import_session,
    load_storage_state,
    normalize_storage_state,
    save_storage_state,
    summarize,
)

SECRET = "super-secret-cookie-value"


def _extension_cookie(**overrides: Any) -> dict[str, Any]:
    cookie: dict[str, Any] = {
        "name": KEY_COOKIE,
        "value": SECRET,
        "domain": ".spotify.com",
        "path": "/",
        "expirationDate": 1893456000.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "no_restriction",
        "hostOnly": False,
        "storeId": "0",
        "session": False,
    }
    cookie.update(overrides)
    return cookie


def _playwright_cookie(**overrides: Any) -> dict[str, Any]:
    cookie: dict[str, Any] = {
        "name": KEY_COOKIE,
        "value": SECRET,
        "domain": ".spotify.com",
        "path": "/",
        "expires": -1.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


def _state(*cookies: dict[str, Any]) -> dict[str, Any]:
    return normalize_storage_state([_playwright_cookie(), *cookies])


def test_normalize_accepts_playwright_storage_state() -> None:
    origins = [{"origin": "https://creators.spotify.com", "localStorage": []}]
    state = normalize_storage_state({"cookies": [_extension_cookie()], "origins": origins})

    assert state["origins"] == origins
    assert len(state["cookies"]) == 1
    cookie = state["cookies"][0]
    assert cookie["name"] == KEY_COOKIE
    assert cookie["value"] == SECRET
    assert cookie["domain"] == ".spotify.com"
    assert cookie["path"] == "/"
    assert cookie["expires"] == 1893456000.0
    assert cookie["httpOnly"] is True
    assert cookie["secure"] is True
    assert cookie["sameSite"] == "None"
    assert "expirationDate" not in cookie
    assert "session" not in cookie
    assert "hostOnly" not in cookie


def test_bare_extension_cookie_list_gets_empty_origins() -> None:
    state = normalize_storage_state([_extension_cookie()])

    assert state["origins"] == []
    assert [cookie["name"] for cookie in state["cookies"]] == [KEY_COOKIE]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("no_restriction", "None"),
        ("lax", "Lax"),
        ("strict", "Strict"),
        ("unspecified", "Lax"),
        ("None", "None"),
        ("LAX", "Lax"),
    ],
)
def test_same_site_mapping(given: str, expected: str) -> None:
    state = normalize_storage_state([_extension_cookie(sameSite=given)])
    assert state["cookies"][0]["sameSite"] == expected


def test_missing_same_site_defaults_to_lax() -> None:
    cookie = _extension_cookie()
    cookie.pop("sameSite")
    state = normalize_storage_state([cookie])
    assert state["cookies"][0]["sameSite"] == "Lax"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"expirationDate": 1234.5}, 1234.5),
        ({"expirationDate": "1234.5"}, 1234.5),
        ({"expirationDate": 0}, -1.0),
        ({"expirationDate": -1}, -1.0),
        ({"expires": 1234.5}, 1234.5),
        ({"session": True}, -1.0),
    ],
)
def test_expiry_handling(overrides: dict[str, Any], expected: float) -> None:
    state = normalize_storage_state([_extension_cookie(**overrides)])
    assert state["cookies"][0]["expires"] == expected


def test_missing_expiry_is_a_session_cookie() -> None:
    cookie = _extension_cookie()
    cookie.pop("expirationDate")
    state = normalize_storage_state([cookie])
    assert state["cookies"][0]["expires"] == -1.0


def test_non_spotify_cookies_are_dropped() -> None:
    state = normalize_storage_state(
        [
            _extension_cookie(),
            _extension_cookie(name="evil", domain="evil.com"),
            _extension_cookie(name="lookalike", domain="notspotify.com"),
            _extension_cookie(name="suffix", domain="spotify.com.evil.com"),
            _extension_cookie(name="apex", domain="spotify.com"),
        ]
    )

    assert [cookie["name"] for cookie in state["cookies"]] == [KEY_COOKIE, "apex"]
    assert [cookie["domain"] for cookie in state["cookies"]] == [".spotify.com", "spotify.com"]


def test_missing_sp_dc_is_rejected_without_leaking_values() -> None:
    cookies = [_extension_cookie(name="other", value=SECRET)]
    with pytest.raises(ConfigurationError) as excinfo:
        normalize_storage_state(cookies)

    message = str(excinfo.value)
    assert KEY_COOKIE in message
    assert "login" in message
    assert SECRET not in message


def test_empty_sp_dc_value_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        normalize_storage_state([_extension_cookie(value="")])


def test_no_spotify_cookies_is_rejected_without_leaking_values() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        normalize_storage_state([_extension_cookie(domain="evil.com", name="evil", value=SECRET)])

    assert SECRET not in str(excinfo.value)
    assert "spotify.com" in str(excinfo.value)


@pytest.mark.parametrize("raw", [None, 5, "cookies", {"origins": []}, {"cookies": "nope"}])
def test_invalid_storage_shapes_are_rejected(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        normalize_storage_state(raw)


def test_save_storage_state_creates_private_file_and_directory(tmp_path) -> None:
    path = tmp_path / "nested" / "storage_state.json"
    state = normalize_storage_state([_extension_cookie()])

    save_storage_state(path, state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["cookies"][0]["name"] == KEY_COOKIE


def test_load_missing_file_tells_operator_to_login(tmp_path) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_storage_state(tmp_path / "absent.json")

    message = str(excinfo.value)
    assert "spotify-uploader login" in message
    assert "spotify-uploader import-session" in message


def test_load_unparsable_file_tells_operator_to_login(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{ definitely not json", encoding="utf-8")

    with pytest.raises(ConfigurationError) as excinfo:
        load_storage_state(path)

    message = str(excinfo.value)
    assert "not valid JSON" in message
    assert "spotify-uploader login" in message


def test_load_normalizes_a_browser_export(tmp_path) -> None:
    path = tmp_path / "cookies.json"
    path.write_text(
        json.dumps([_extension_cookie(domain="accounts.spotify.com")]),
        encoding="utf-8",
    )

    state = load_storage_state(path)

    assert state["cookies"][0]["domain"] == "accounts.spotify.com"
    assert state["cookies"][0]["sameSite"] == "None"
    assert state["origins"] == []


def test_summarize_never_exposes_cookie_values() -> None:
    summary = summarize(_state())

    rendered = f"{summary!r} {summary}"
    assert SECRET not in rendered
    assert summary.has_sp_dc is True
    assert summary.cookie_count == 1
    assert summary.sp_dc_expires_at is None
    assert summary.expired is False


def test_summary_expiry_and_expired_flag() -> None:
    future = datetime.now(UTC) + timedelta(days=1)
    past = datetime.now(UTC) - timedelta(minutes=5)

    live = normalize_storage_state([_extension_cookie(expirationDate=future.timestamp())])
    dead = normalize_storage_state([_extension_cookie(expirationDate=past.timestamp())])

    live_summary = summarize(live)
    assert live_summary.expired is False
    assert summarize(dead).expired is True
    assert live_summary.sp_dc_expires_at is not None
    assert abs((live_summary.sp_dc_expires_at - future).total_seconds()) < 1


def test_summary_of_a_session_without_sp_dc_is_not_expired() -> None:
    summary = summarize({"cookies": []})

    assert summary == SessionSummary(has_sp_dc=False, sp_dc_expires_at=None, cookie_count=0)
    assert summary.expired is False


def test_summary_lenient_with_bad_expiry() -> None:
    summary = summarize({"cookies": [_playwright_cookie(expires="not-a-number")]})

    assert summary.has_sp_dc is True
    assert summary.sp_dc_expires_at is None
    assert summary.expired is False


def test_import_session_writes_a_private_normalized_copy(tmp_path) -> None:
    src = tmp_path / "export.json"
    src.write_text(json.dumps([_extension_cookie()]), encoding="utf-8")
    dst = tmp_path / "private" / "storage_state.json"

    summary = import_session(src, dst)

    assert summary.has_sp_dc is True
    assert summary.cookie_count == 1
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    saved = json.loads(dst.read_text(encoding="utf-8"))
    assert saved["cookies"][0]["sameSite"] == "None"
    assert saved["cookies"][0]["expires"] == 1893456000.0


def test_import_session_rejects_bad_source_without_writing(tmp_path) -> None:
    src = tmp_path / "bad.json"
    src.write_text("nope", encoding="utf-8")
    dst = tmp_path / "private" / "storage_state.json"

    with pytest.raises(ConfigurationError):
        import_session(src, dst)

    assert not dst.exists()


def test_normalize_is_idempotent() -> None:
    once = normalize_storage_state([_extension_cookie()])
    twice = normalize_storage_state(once)

    assert twice == once
