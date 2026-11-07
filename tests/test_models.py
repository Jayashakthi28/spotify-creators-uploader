"""Episode validation, serialization and streaming-hash tests."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from spotify_uploader.errors import ValidationError
from spotify_uploader.models import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    EpisodeRequest,
    PublishedEpisode,
    PublishStatus,
    sha256_file,
)


def _video(tmp_path: Path, name: str = "video.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"video bytes")
    return path


def _request(tmp_path: Path, **overrides) -> EpisodeRequest:
    values = {
        "video_path": _video(tmp_path),
        "title": "A title",
        "description": "A description",
    }
    values.update(overrides)
    return EpisodeRequest(**values)


@pytest.mark.parametrize("name", ["video.mp4", "video.mov", "video.MP4", "video.MOV"])
def test_valid_request_accepts_mp4_and_mov(tmp_path, name) -> None:
    _request(tmp_path, video_path=_video(tmp_path, name)).validate()


def test_missing_video_is_rejected(tmp_path) -> None:
    with pytest.raises(ValidationError, match="not found"):
        _request(tmp_path, video_path=tmp_path / "missing.mp4").validate()


@pytest.mark.parametrize("name", ["video.avi", "video.mkv", "video.txt", "video"])
def test_unsupported_suffix_is_rejected(tmp_path, name) -> None:
    with pytest.raises(ValidationError, match="unsupported video type"):
        _request(tmp_path, video_path=_video(tmp_path, name)).validate()


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_blank_title_is_rejected(tmp_path, title) -> None:
    with pytest.raises(ValidationError, match="title must not be empty"):
        _request(tmp_path, title=title).validate()


def test_title_at_limit_is_allowed_and_over_limit_is_rejected(tmp_path) -> None:
    _request(tmp_path, title="x" * MAX_TITLE_CHARS).validate()

    with pytest.raises(ValidationError, match="longer than"):
        _request(tmp_path, title="x" * (MAX_TITLE_CHARS + 1)).validate()


def test_description_at_limit_is_allowed_and_over_limit_is_rejected(tmp_path) -> None:
    _request(tmp_path, description="x" * MAX_DESCRIPTION_CHARS).validate()

    with pytest.raises(ValidationError, match="longer than"):
        _request(tmp_path, description="x" * (MAX_DESCRIPTION_CHARS + 1)).validate()


def test_published_episode_round_trip() -> None:
    episode = PublishedEpisode(
        status=PublishStatus.PROCESSING,
        title="A title",
        show_id="show1",
        sha256="a" * 64,
        published_at="2026-01-01T00:00:00+00:00",
        episode_id="episode1",
        url="https://creators.spotify.com/pod/show/show1/episode/episode1",
    )

    payload = episode.to_dict()

    assert payload["status"] == "processing"
    assert PublishedEpisode.from_dict(payload) == episode


def test_published_episode_optional_fields_default_to_none() -> None:
    payload = {
        "status": "published",
        "title": "A title",
        "show_id": "show1",
        "sha256": "b" * 64,
        "published_at": "2026-01-01T00:00:00+00:00",
    }

    episode = PublishedEpisode.from_dict(payload)

    assert episode.episode_id is None
    assert episode.url is None
    assert episode.status is PublishStatus.PUBLISHED


def test_publish_status_values() -> None:
    assert {status.value for status in PublishStatus} == {
        "published",
        "processing",
        "dry_run",
        "already_published",
    }


@pytest.mark.parametrize("chunk_size", [1, 3, 1024, 1 << 20])
def test_sha256_file_streams_to_the_expected_digest(tmp_path, chunk_size) -> None:
    path = tmp_path / "payload.bin"
    data = bytes(range(256)) * 41
    path.write_bytes(data)

    assert sha256_file(path, chunk_size=chunk_size) == hashlib.sha256(data).hexdigest()


def test_sha256_file_handles_empty_files(tmp_path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    assert sha256_file(path) == hashlib.sha256(b"").hexdigest()


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    row = b"\x00" + b"\x00\x00\x00\xff" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int, height: int) -> bytes:
    def segment(marker: int, payload: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload

    frame = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return (
        b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01") + segment(0xC0, frame)
    )


def _thumbnail(
    tmp_path: Path,
    *,
    width: int = 1280,
    height: int = 720,
    name: str = "thumb.png",
    payload: bytes | None = None,
) -> Path:
    path = tmp_path / name
    path.write_bytes(payload if payload is not None else _png(width, height))
    return path


def test_a_valid_png_thumbnail_is_accepted(tmp_path) -> None:
    _request(tmp_path, thumbnail_path=_thumbnail(tmp_path)).validate()


def test_a_valid_jpeg_thumbnail_is_accepted(tmp_path) -> None:
    thumb = _thumbnail(tmp_path, name="thumb.jpg", payload=_jpeg(1920, 1080))
    _request(tmp_path, thumbnail_path=thumb).validate()


def test_a_missing_thumbnail_is_rejected(tmp_path) -> None:
    with pytest.raises(ValidationError, match="thumbnail file not found"):
        _request(tmp_path, thumbnail_path=tmp_path / "missing.png").validate()


def test_a_directory_as_thumbnail_is_rejected(tmp_path) -> None:
    directory = tmp_path / "thumb-dir"
    directory.mkdir()

    with pytest.raises(ValidationError, match="not a file"):
        _request(tmp_path, thumbnail_path=directory).validate()


def test_non_image_bytes_are_rejected_as_a_thumbnail(tmp_path) -> None:
    thumb = _thumbnail(tmp_path, name="thumb.png", payload=b"not an image")

    with pytest.raises(ValidationError, match="PNG or JPEG"):
        _request(tmp_path, thumbnail_path=thumb).validate()


@pytest.mark.parametrize(("width", "height"), [(1080, 1080), (720, 1280), (1920, 1000)])
def test_a_non_sixteen_by_nine_thumbnail_is_rejected(tmp_path, width, height) -> None:
    thumb = _thumbnail(tmp_path, width=width, height=height)

    with pytest.raises(ValidationError) as info:
        _request(tmp_path, thumbnail_path=thumb).validate()

    assert f"{width}x{height}" in str(info.value)
    assert "16:9" in str(info.value)


def test_png_bytes_named_as_jpeg_are_accepted(tmp_path) -> None:
    thumb = _thumbnail(tmp_path, name="thumb.jpg", payload=_png(1280, 720))
    _request(tmp_path, thumbnail_path=thumb).validate()


def test_a_narrow_thumbnail_is_rejected_by_the_sanity_floor(tmp_path) -> None:
    thumb = _thumbnail(tmp_path, width=300, height=169)

    with pytest.raises(ValidationError) as info:
        _request(tmp_path, thumbnail_path=thumb).validate()

    assert "300px wide" in str(info.value)
    assert "sanity limit" in str(info.value)


def test_the_minimum_width_thumbnail_is_accepted(tmp_path) -> None:
    thumb = _thumbnail(tmp_path, width=320, height=180)
    _request(tmp_path, thumbnail_path=thumb).validate()
