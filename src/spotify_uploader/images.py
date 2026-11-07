"""Read image dimensions without an imaging library.

Spotify only rejects a bad thumbnail after it has been uploaded, so the request
is validated up front with a small stdlib header parser. Only PNG and JPEG are
accepted: they are the two formats Spotify documents for thumbnails, and both
carry their pixel size in the first few hundred bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from spotify_uploader.errors import ValidationError

PNG_FORMAT = "png"
JPEG_FORMAT = "jpeg"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_IHDR = b"IHDR"
_PNG_IHDR_LENGTH = 13
_JPEG_SOI = b"\xff\xd8"
#: SOF0..SOF15 carry the frame size; 0xC4/0xC8/0xCC are DHT/JPG/DAC, not frames.
_JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
#: TEM (0x01) and the restart markers (0xD0..0xD7) have no payload length field.
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xD8)})
_JPEG_SOS = 0xDA
_JPEG_EOI = 0xD9


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """The sniffed format and stored pixel size of an image file."""

    format: str
    width: int
    height: int


def read_image_info(path: Path) -> ImageInfo:
    """Sniff PNG/JPEG by magic bytes and parse the pixel size from the header.

    EXIF orientation is deliberately ignored: this reports the stored width and
    height, which is what Spotify's uploader decodes. A phone JPEG tagged for
    rotation may therefore report portrait dimensions even though viewers show
    it rotated.
    """
    try:
        with path.open("rb") as handle:
            signature = handle.read(8)
            if signature == _PNG_SIGNATURE:
                return _read_png_info(handle, path)
            if signature.startswith(_JPEG_SOI):
                handle.seek(len(_JPEG_SOI))
                return _read_jpeg_info(handle, path)
            if signature and _PNG_SIGNATURE.startswith(signature):
                raise _truncated(path, PNG_FORMAT)
    except OSError as exc:
        raise ValidationError(f"could not read image {path}: {exc}") from exc
    raise ValidationError(f"unsupported image format for {path}: expected PNG or JPEG magic bytes")


def is_sixteen_by_nine(width: int, height: int, tolerance: float = 0.01) -> bool:
    """Whether width:height is 16:9 within a relative tolerance (1% by default).

    The tolerance is relative to 16:9 so that round HD sizes pass exactly while
    uncropped 1920x1000 (1.92:1) fails.
    """
    if width <= 0 or height <= 0 or tolerance < 0:
        return False
    target = 16 / 9
    return abs(width / height - target) / target <= tolerance


def _read_png_info(handle: BinaryIO, path: Path) -> ImageInfo:
    header = handle.read(8)
    if len(header) < 8:
        raise _truncated(path, PNG_FORMAT)
    length, kind = struct.unpack(">I4s", header)
    if kind != _PNG_IHDR or length != _PNG_IHDR_LENGTH:
        raise ValidationError(f"{path} is not a valid PNG: the first chunk is {kind!r}, not IHDR")
    payload = handle.read(_PNG_IHDR_LENGTH)
    if len(payload) < _PNG_IHDR_LENGTH:
        raise _truncated(path, PNG_FORMAT)
    width, height = struct.unpack(">II", payload[:8])
    return _checked_info(path, PNG_FORMAT, width, height)


def _read_jpeg_info(handle: BinaryIO, path: Path) -> ImageInfo:
    while True:
        marker = _next_jpeg_marker(handle)
        if marker is None:
            raise _truncated(path, JPEG_FORMAT)
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if marker in (_JPEG_SOS, _JPEG_EOI):
            raise ValidationError(f"{path} is not a valid JPEG: it has no frame size header")
        length_bytes = handle.read(2)
        if len(length_bytes) < 2:
            raise _truncated(path, JPEG_FORMAT)
        (length,) = struct.unpack(">H", length_bytes)
        if length < 2:
            raise ValidationError(
                f"{path} is not a valid JPEG: a marker segment has length {length}"
            )
        payload = handle.read(length - 2)
        if len(payload) < length - 2:
            raise _truncated(path, JPEG_FORMAT)
        if marker in _JPEG_SOF_MARKERS:
            if len(payload) < 5:
                raise ValidationError(
                    f"{path} is not a valid JPEG: its frame size header is too short"
                )
            height, width = struct.unpack(">HH", payload[1:5])
            return _checked_info(path, JPEG_FORMAT, width, height)


def _next_jpeg_marker(handle: BinaryIO) -> int | None:
    """Read past 0xFF fill bytes to the next marker byte, or None at EOF."""
    byte = handle.read(1)
    while byte and byte != b"\xff":
        byte = handle.read(1)
    while True:
        byte = handle.read(1)
        if not byte:
            return None
        if byte != b"\xff":
            return byte[0]


def _checked_info(path: Path, image_format: str, width: int, height: int) -> ImageInfo:
    if width == 0 or height == 0:
        raise ValidationError(
            f"{path} is not a valid {image_format.upper()} image: it is {width}x{height}"
        )
    return ImageInfo(format=image_format, width=width, height=height)


def _truncated(path: Path, image_format: str) -> ValidationError:
    return ValidationError(f"{path} is a truncated {image_format.upper()} image")
