"""Local idempotency ledger keyed on (show id, video SHA-256).

Idempotency is the caller's job (a repeated Spotify upload creates a second
episode), so every real publish is recorded here and an identical file is skipped
for the same show unless forced. Publishing one video to several shows is
legitimate, so the show id is part of the key. Dry runs are never recorded.

Entries written by older versions are keyed by the bare SHA-256 but carry the
show id in their value; they stay readable and are migrated on the next
``record`` for that show.
"""

from __future__ import annotations

from pathlib import Path

from spotify_uploader.config import read_json_object, write_private_json
from spotify_uploader.models import PublishedEpisode


def _entry_key(sha256: str, show_id: str) -> str:
    return f"{show_id}:{sha256}"


def _parse(raw: object) -> PublishedEpisode | None:
    if not isinstance(raw, dict):
        return None
    try:
        return PublishedEpisode.from_dict(raw)
    except (KeyError, ValueError):
        return None


class Ledger:
    """One published episode per (show, video); legacy sha-only keys still read."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, sha256: str, show_id: str | None = None) -> PublishedEpisode | None:
        """Find a recorded episode for this video, optionally limited to one show.

        ``show_id=None`` keeps the original "any show" lookup. Legacy entries are
        matched through the ``show_id`` stored in their value, so reading never
        requires rewriting the file.
        """
        entries = read_json_object(self._path)
        if show_id is not None:
            direct = _parse(entries.get(_entry_key(sha256, show_id)))
            if direct is not None and direct.show_id == show_id:
                return direct
            for key in sorted(entries):
                episode = _parse(entries[key])
                if episode is None or episode.show_id != show_id or episode.sha256 != sha256:
                    continue
                return episode
            return None

        direct = _parse(entries.get(sha256))
        if direct is not None:
            return direct
        for key in sorted(entries):
            episode = _parse(entries[key])
            if episode is not None and episode.sha256 == sha256:
                return episode
        return None

    def record(self, episode: PublishedEpisode) -> None:
        """Store the episode under its show, migrating a matching legacy entry.

        A legacy entry recorded for a *different* show is left alone: the same
        video may legitimately live under more than one show.
        """
        entries = read_json_object(self._path)
        legacy = _parse(entries.get(episode.sha256))
        if legacy is not None and legacy.show_id == episode.show_id:
            del entries[episode.sha256]
        entries[_entry_key(episode.sha256, episode.show_id)] = episode.to_dict()
        write_private_json(self._path, entries)

    def all(self) -> list[PublishedEpisode]:
        """Every distinct episode once, even when a legacy and a new key coexist.

        New-style keys are visited first so a migrated entry wins over a stale
        legacy twin with the same (show, video) identity.
        """
        episodes: list[PublishedEpisode] = []
        seen: set[tuple[str, str]] = set()
        entries = read_json_object(self._path)
        for key in sorted(entries, key=lambda item: (":" not in item, item)):
            episode = _parse(entries[key])
            if episode is None:
                continue
            identity = (episode.show_id, episode.sha256)
            if identity in seen:
                continue
            seen.add(identity)
            episodes.append(episode)
        return episodes
