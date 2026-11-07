"""Saved Playwright session handling.

The ``sp_dc`` cookie is a full-account credential, so this module normalises
sessions without ever exposing cookie values: summaries carry only booleans, a
cookie count and an expiry. Files are written 0600 inside a 0700 directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spotify_uploader.config import write_private_json
from spotify_uploader.errors import ConfigurationError

KEY_COOKIE = "sp_dc"
SPOTIFY_DOMAIN = "spotify.com"
LOGIN_HINT = "run `spotify-uploader login` or `spotify-uploader import-session`"

_SAME_SITE = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
}


def is_spotify_domain(domain: str) -> bool:
    """True for ``spotify.com`` and its subdomains, never for lookalikes."""
    clean = domain.strip().lower()
    return clean == SPOTIFY_DOMAIN or clean.endswith("." + SPOTIFY_DOMAIN)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """The non-secret facts about a session.

    Session cookies (``expires == -1``) have no expiry to inspect, so they are
    treated as live; only a concrete past timestamp counts as expired.
    """

    has_sp_dc: bool
    sp_dc_expires_at: datetime | None
    cookie_count: int

    @property
    def expired(self) -> bool:
        if self.sp_dc_expires_at is None:
            return False
        return self.sp_dc_expires_at <= datetime.now(UTC)


def _cookie_list(raw: object) -> tuple[list[Any], list[Any]]:
    if isinstance(raw, list):
        return list(raw), []
    if isinstance(raw, dict):
        cookies = raw.get("cookies")
        if not isinstance(cookies, list):
            raise ConfigurationError(f"storage state has no cookie list; {LOGIN_HINT}")
        origins = raw.get("origins")
        return list(cookies), list(origins) if isinstance(origins, list) else []
    raise ConfigurationError(
        f"storage state must be a Playwright object or a cookie list; {LOGIN_HINT}"
    )


def _expires(raw: dict[str, Any]) -> float:
    if raw.get("session") is True:
        return -1.0
    value = raw.get("expires", raw.get("expirationDate"))
    if value is None:
        return -1.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    return number if number > 0 else -1.0


def _flag(raw: dict[str, Any], key: str) -> bool:
    return raw.get(key) is True


def _playwright_cookie(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = raw.get("name")
    value = raw.get("value")
    domain = raw.get("domain")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(value, str) or not isinstance(domain, str):
        return None
    if not is_spotify_domain(domain):
        return None
    path = raw.get("path")
    same_site = raw.get("sameSite")
    mapped_same_site = (
        _SAME_SITE.get(same_site.strip().lower(), "Lax") if isinstance(same_site, str) else "Lax"
    )
    return {
        "name": name,
        "value": value,
        "domain": domain.strip(),
        "path": path.strip() if isinstance(path, str) and path.strip() else "/",
        "expires": _expires(raw),
        "httpOnly": _flag(raw, "httpOnly"),
        "secure": _flag(raw, "secure"),
        "sameSite": mapped_same_site,
    }


def normalize_storage_state(raw: object) -> dict[str, Any]:
    """Coerce a Playwright state or browser-extension cookie export into Playwright shape.

    Extensions use ``expirationDate``/``session`` and ``sameSite="no_restriction"``;
    Playwright wants ``expires`` (-1 for session cookies) and ``"None"``. Cookies
    outside ``spotify.com`` are dropped because this session only ever talks to
    Spotify for Creators.
    """
    raw_cookies, origins = _cookie_list(raw)
    cookies: list[dict[str, Any]] = []
    for item in raw_cookies:
        if not isinstance(item, dict):
            continue
        cookie = _playwright_cookie(item)
        if cookie is not None:
            cookies.append(cookie)
    if not cookies:
        raise ConfigurationError(f"session has no spotify.com cookies; {LOGIN_HINT}")
    if not any(cookie["name"] == KEY_COOKIE and cookie["value"] for cookie in cookies):
        raise ConfigurationError(f"session is missing the {KEY_COOKIE} cookie; {LOGIN_HINT}")
    state: dict[str, Any] = {"cookies": cookies, "origins": origins}
    return state


def load_storage_state(path: Path) -> dict[str, Any]:
    """Read and normalise a saved session; a bad file must fail before any browser work."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ConfigurationError(
            f"cannot read session file {path}: {reason}; {LOGIN_HINT}"
        ) from exc
    except ValueError as exc:
        raise ConfigurationError(f"session file {path} is not valid JSON; {LOGIN_HINT}") from exc
    return normalize_storage_state(data)


def save_storage_state(path: Path, state: dict[str, Any]) -> None:
    """Persist a normalised session with 0600/0700 permissions."""
    write_private_json(path, state)


def summarize(state: dict[str, Any]) -> SessionSummary:
    """Describe a stored session without copying any cookie value into the summary."""
    raw_cookies = state.get("cookies")
    if not isinstance(raw_cookies, list):
        return SessionSummary(has_sp_dc=False, sp_dc_expires_at=None, cookie_count=0)
    cookies = [cookie for cookie in raw_cookies if isinstance(cookie, dict)]
    sp_dc = next(
        (
            cookie
            for cookie in cookies
            if cookie.get("name") == KEY_COOKIE
            and isinstance(cookie.get("value"), str)
            and bool(cookie.get("value"))
            and isinstance(cookie.get("domain"), str)
            and is_spotify_domain(cookie["domain"])
        ),
        None,
    )
    expires_at: datetime | None = None
    if sp_dc is not None:
        try:
            expires = float(sp_dc.get("expires", -1))
        except (TypeError, ValueError):
            expires = -1.0
        if expires > 0:
            try:
                expires_at = datetime.fromtimestamp(expires, tz=UTC)
            except (OSError, OverflowError, ValueError):
                expires_at = None
    return SessionSummary(
        has_sp_dc=sp_dc is not None,
        sp_dc_expires_at=expires_at,
        cookie_count=len(cookies),
    )


def import_session(src: Path, dst: Path) -> SessionSummary:
    """Import a browser-exported cookie file and save it as a private Playwright state."""
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ConfigurationError(f"cannot read session file {src}: {reason}; {LOGIN_HINT}") from exc
    except ValueError as exc:
        raise ConfigurationError(f"session file {src} is not valid JSON; {LOGIN_HINT}") from exc
    state = normalize_storage_state(data)
    save_storage_state(dst, state)
    return summarize(state)
