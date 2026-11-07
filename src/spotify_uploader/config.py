"""Paths and runtime options.

Everything private lives under one home directory outside any repo
(``~/.spotify-uploader`` by default, ``$SPOTIFY_UPLOADER_HOME`` overrides): the
Playwright session, the idempotency ledger and the show id. Files are 0600 and the
directory 0700; credential values are never logged or rendered.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spotify_uploader.errors import ConfigurationError

HOME_ENV = "SPOTIFY_UPLOADER_HOME"
SHOW_ID_ENV = "SPOTIFY_SHOW_ID"
_SHOW_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def default_home() -> Path:
    override = os.environ.get(HOME_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".spotify-uploader"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write JSON with mode 0600, so a crash never leaves a torn session file."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)
    path.chmod(0o600)


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object, returning ``{}`` when the file is absent or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def validate_show_id(show_id: str) -> str:
    cleaned = show_id.strip()
    if not _SHOW_ID_RE.match(cleaned):
        raise ConfigurationError(f"invalid show id {show_id!r}; expected letters, digits, _ or -")
    return cleaned


@dataclass(frozen=True, slots=True)
class UploaderConfig:
    """Resolved settings for one run.

    ``channel="chrome"`` runs the system Google Chrome instead of Playwright's
    bundled Chromium. ``debug_dir`` turns on failure artifacts (screenshot, HTML,
    trace) written per failed step.
    """

    home: Path = field(default_factory=default_home)
    show_id: str | None = None
    headless: bool = True
    channel: str | None = None
    upload_timeout_s: float = 1800.0
    step_timeout_s: float = 30.0
    debug_dir: Path | None = None
    slow_mo_ms: int = 0

    @property
    def storage_state_path(self) -> Path:
        return self.home / "storage_state.json"

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.json"

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def chrome_profile_dir(self) -> Path:
        return self.home / "chrome-profile"

    @classmethod
    def load(
        cls,
        *,
        home: Path | None = None,
        show_id: str | None = None,
        headless: bool | None = None,
        channel: str | None = None,
        debug_dir: Path | None = None,
    ) -> UploaderConfig:
        """Merge explicit arguments over ``$SPOTIFY_SHOW_ID`` over ``config.json``."""
        base = home.expanduser() if home else default_home()
        stored = read_json_object(base / "config.json")
        chosen = show_id or os.environ.get(SHOW_ID_ENV, "").strip() or stored.get("show_id")
        return cls(
            home=base,
            show_id=validate_show_id(str(chosen)) if chosen else None,
            headless=True if headless is None else headless,
            channel=channel or stored.get("channel") or None,
            debug_dir=debug_dir,
        )


def save_show_id(config: UploaderConfig, show_id: str) -> None:
    """Persist the show id next to the session so later runs need no flag."""
    stored = read_json_object(config.config_path)
    stored["show_id"] = validate_show_id(show_id)
    write_private_json(config.config_path, stored)
