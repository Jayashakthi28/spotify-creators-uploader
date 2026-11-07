"""Header parsing for PNG/JPEG thumbnails, built from hand-made bytes."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from spotify_uploader.errors import ValidationError
from spotify_uploader.images import ImageInfo, is_sixteen_by_nine, read_image_info


def make_png(width: int, height: int) -> bytes:
    """A valid solid-black RGBA PNG, small enough to build in memory."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = b"\x00" + b"\x00\x00\x00\xff" * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


def make_jpeg(
    width: int, height: int, *, sof: int = 0xC0, with_dht: bool = False, fill: bool = False
) -> bytes:
    """A JPEG header with a real SOF segment; no image data is needed for parsing."""

    def segment(marker: int, payload: bytes) -> bytes:
        return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload

    app0 = segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    parts = [b"\xff\xd8", app0]
    if with_dht:
        # 0xC4 is a Huffman table: its payload must not be mistaken for a frame size.
        huffman = b"\x00" + b"\x11" * 16 + struct.pack(">HH", 999, 999)
        parts.append(segment(0xC4, huffman))
    frame = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    parts.append(segment(sof, frame))
    parts.append(segment(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"))
    parts.append(b"\xff\xd9")
    return (b"\xff" if fill else b"").join(parts)


def test_reads_png_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.png"
    path.write_bytes(make_png(1280, 720))

    assert read_image_info(path) == ImageInfo(format="png", width=1280, height=720)


def test_reads_baseline_jpeg_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.jpg"
    path.write_bytes(make_jpeg(1920, 1080))

    assert read_image_info(path) == ImageInfo(format="jpeg", width=1920, height=1080)


def test_reads_progressive_jpeg_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.jpg"
    path.write_bytes(make_jpeg(1280, 720, sof=0xC2))

    assert read_image_info(path) == ImageInfo(format="jpeg", width=1280, height=720)


def test_skips_huffman_tables_and_fill_bytes(tmp_path: Path) -> None:
    path = tmp_path / "thumbnail.jpg"
    path.write_bytes(make_jpeg(1600, 900, with_dht=True, fill=True))

    assert read_image_info(path) == ImageInfo(format="jpeg", width=1600, height=900)


@pytest.mark.parametrize(
    ("name", "builder", "expected"),
    [
        ("thumbnail.jpg", make_png, ImageInfo(format="png", width=1280, height=720)),
        ("thumbnail.png", make_jpeg, ImageInfo(format="jpeg", width=1920, height=1080)),
    ],
)
def test_magic_bytes_beat_a_lying_extension(tmp_path: Path, name, builder, expected) -> None:
    path = tmp_path / name
    path.write_bytes(builder(expected.width, expected.height))

    assert read_image_info(path) == expected


def test_truncated_png_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(make_png(1280, 720)[:20])

    with pytest.raises(ValidationError, match="truncated PNG"):
        read_image_info(path)


def test_truncated_jpeg_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + struct.pack(">H", 1000) + b"JFIF")

    with pytest.raises(ValidationError, match="truncated JPEG"):
        read_image_info(path)


def test_jpeg_without_a_frame_header_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"\xff\xd8" + b"\xff\xd9")

    with pytest.raises(ValidationError, match="no frame size header"):
        read_image_info(path)


def test_zero_sized_png_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(make_png(0, 0))

    with pytest.raises(ValidationError, match="0x0"):
        read_image_info(path)


@pytest.mark.parametrize("payload", [b"GIF89a" + b"\x00" * 20, b"not an image at all"])
def test_non_image_bytes_are_rejected(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "thumbnail.png"
    path.write_bytes(payload)

    with pytest.raises(ValidationError, match="PNG or JPEG"):
        read_image_info(path)


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.png"
    path.write_bytes(b"")

    with pytest.raises(ValidationError, match="PNG or JPEG"):
        read_image_info(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="could not read"):
        read_image_info(tmp_path / "missing.png")


@pytest.mark.parametrize(
    ("width", "height"),
    [(1280, 720), (1920, 1080), (3840, 2160), (1600, 900), (1920, 1081)],
)
def test_sixteen_by_nine_accepts_hd_and_near_misses(width: int, height: int) -> None:
    assert is_sixteen_by_nine(width, height) is True


@pytest.mark.parametrize(("width", "height"), [(1080, 1080), (720, 1280), (1920, 1000), (0, 0)])
def test_sixteen_by_nine_rejects_other_ratios(width: int, height: int) -> None:
    assert is_sixteen_by_nine(width, height) is False


def test_sixteen_by_nine_honours_a_custom_tolerance() -> None:
    assert is_sixteen_by_nine(1920, 1100) is False
    assert is_sixteen_by_nine(1920, 1100, tolerance=0.1) is True
