"""Plain data carried across the uploader's boundary.

Frozen dataclasses with no I/O, so a host application can embed them, serialise them
and compare them without pulling in a browser.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from spotify_uploader.errors import ValidationError
from spotify_uploader.images import is_sixteen_by_nine, read_image_info

#: ``(fraction 0..1, human detail)``; a plain callable so any progress UI can plug in.
ProgressCallback = Callable[[float, str], None]

SUPPORTED_SUFFIXES = (".mp4", ".mov")
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 4000
#: A local sanity floor, not a Spotify rule: a 16:9 image this small looks broken.
MIN_THUMBNAIL_WIDTH = 320


class PublishStatus(StrEnum):
    PUBLISHED = "published"
    PROCESSING = "processing"
    DRY_RUN = "dry_run"
    ALREADY_PUBLISHED = "already_published"


@dataclass(frozen=True, slots=True)
class EpisodeRequest:
    """One video episode to publish immediately."""

    video_path: Path
    title: str
    description: str = ""
    explicit: bool = False
    thumbnail_path: Path | None = None

    def validate(self) -> None:
        """Reject a bad request before any browser work, so it never burns an upload."""
        if not self.video_path.is_file():
            raise ValidationError(f"video file not found: {self.video_path}")
        if self.video_path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ValidationError(
                f"unsupported video type {self.video_path.suffix!r}; use .mp4 or .mov"
            )
        if not self.title.strip():
            raise ValidationError("title must not be empty")
        if len(self.title) > MAX_TITLE_CHARS:
            raise ValidationError(f"title is longer than {MAX_TITLE_CHARS} characters")
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            raise ValidationError(f"description is longer than {MAX_DESCRIPTION_CHARS} characters")
        if self.thumbnail_path is not None:
            self._validate_thumbnail(self.thumbnail_path)

    @staticmethod
    def _validate_thumbnail(thumbnail: Path) -> None:
        """Fail on a thumbnail Spotify would silently ignore or reject.

        The format is sniffed from magic bytes and the size is measured here so
        a mistake costs no browser work and no video upload.
        """
        if not thumbnail.exists():
            raise ValidationError(f"thumbnail file not found: {thumbnail}")
        if not thumbnail.is_file():
            raise ValidationError(f"thumbnail is not a file: {thumbnail}")
        info = read_image_info(thumbnail)
        if not is_sixteen_by_nine(info.width, info.height):
            raise ValidationError(
                f"thumbnail {thumbnail} is {info.width}x{info.height}; Spotify requires "
                "a horizontal 16:9 image (1920x1080 suggested)"
            )
        if info.width < MIN_THUMBNAIL_WIDTH:
            raise ValidationError(
                f"thumbnail {thumbnail} is only {info.width}px wide; {MIN_THUMBNAIL_WIDTH}px "
                "is a local sanity limit, not a Spotify requirement"
            )


@dataclass(frozen=True, slots=True)
class PublishedEpisode:
    """What Spotify (or the local ledger) reports for an episode."""

    status: PublishStatus
    title: str
    show_id: str
    sha256: str
    published_at: str
    episode_id: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishedEpisode:
        return cls(
            status=PublishStatus(data["status"]),
            title=str(data["title"]),
            show_id=str(data["show_id"]),
            sha256=str(data["sha256"]),
            published_at=str(data["published_at"]),
            episode_id=data.get("episode_id"),
            url=data.get("url"),
        )


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hex SHA-256 of a file, streamed so a multi-GB video never loads into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
