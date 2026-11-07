"""Re-encode a video into what Spotify's transcoder expects.

Spotify's video specs ask for one video and one audio track, no MP4 edit lists,
stereo AAC at 128 kbps or more, and Rec.709 colour. Videos from many editors miss
some of those (edit-list atoms, mono audio, a BT.601 matrix), so a re-encode can run
before upload. The colour matrix is converted, not just relabelled: relabelling
BT.601 pixels as BT.709 would visibly shift colours.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from spotify_uploader.errors import ConfigurationError, UploaderError, ValidationError

Runner = Callable[..., subprocess.CompletedProcess[str]]

_ALLOWED_FPS = (24.0, 25.0, 30.0, 50.0, 60.0)
_MIN_AUDIO_KBPS = 128
_BT601_TAGS = frozenset({"smpte170m", "bt470bg"})
_CHUNK_BYTES = 8 << 20


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """The facts about a video that Spotify's specs care about."""

    video_codec: str | None
    width: int
    height: int
    fps: float
    color_space: str | None
    color_primaries: str | None
    color_transfer: str | None
    audio_codec: str | None
    audio_channels: int
    audio_kbps: int | None
    duration_s: float
    edit_lists: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "video": f"{self.video_codec} {self.width}x{self.height} {self.fps:g}fps",
            "colour": f"{self.color_space}/{self.color_primaries}/{self.color_transfer}",
            "audio": f"{self.audio_codec} {self.audio_channels}ch {self.audio_kbps}kbps",
            "duration_s": round(self.duration_s, 1),
            "edit_lists": self.edit_lists,
        }


def count_edit_lists(path: Path, *, chunk_bytes: int = _CHUNK_BYTES) -> int:
    """Count ``elst`` atoms by streaming the file, so a multi-GB video stays off the heap."""
    count = 0
    tail = b""
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            data = tail + chunk
            count += data.count(b"elst")
            tail = data[-3:]
    return count


def probe_media(path: Path, *, ffprobe: str = "ffprobe", run: Runner = subprocess.run) -> MediaInfo:
    """Read the stream facts of ``path`` with ffprobe."""
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"ffprobe not found ({ffprobe}); install ffmpeg") from exc
    if result.returncode != 0:
        raise UploaderError(f"ffprobe could not read {path.name}: {result.stderr.strip()[-300:]}")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise UploaderError(f"ffprobe returned unreadable output for {path.name}") from exc

    streams = data.get("streams", [])
    video: dict[str, Any] = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio: dict[str, Any] = next((s for s in streams if s.get("codec_type") == "audio"), {})
    bit_rate = audio.get("bit_rate")
    return MediaInfo(
        video_codec=video.get("codec_name"),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=_parse_fps(video.get("r_frame_rate")),
        color_space=video.get("color_space"),
        color_primaries=video.get("color_primaries"),
        color_transfer=video.get("color_transfer"),
        audio_codec=audio.get("codec_name"),
        audio_channels=int(audio.get("channels", 0)),
        audio_kbps=int(bit_rate) // 1000 if bit_rate else None,
        duration_s=float(data.get("format", {}).get("duration", 0.0)),
        edit_lists=count_edit_lists(path),
    )


def _parse_fps(value: object) -> float:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def spec_issues(info: MediaInfo) -> list[str]:
    """Every way ``info`` misses Spotify's published video specs; empty means compliant."""
    issues: list[str] = []
    if info.video_codec not in ("h264", "hevc"):
        issues.append(f"video codec {info.video_codec!r} is not H.264 or H.265")
    if info.height < 1080:
        issues.append(f"resolution {info.width}x{info.height} is below 1080p")
    if info.height and abs(info.width / info.height - 16 / 9) > 0.02:
        issues.append(f"aspect ratio {info.width}x{info.height} is not 16:9")
    if not any(abs(info.fps - allowed) < 0.01 for allowed in _ALLOWED_FPS):
        issues.append(f"frame rate {info.fps:g} is not one of 24, 25, 30, 50, 60")
    if info.color_space != "bt709" or info.color_primaries != "bt709":
        issues.append(
            f"colour is {info.color_space}/{info.color_primaries}, not Rec.709 (bt709/bt709)"
        )
    if info.audio_codec is None:
        issues.append("no audio track")
    else:
        if info.audio_channels != 2:
            issues.append(f"audio has {info.audio_channels} channel(s); stereo is required")
        if info.audio_kbps is not None and info.audio_kbps < _MIN_AUDIO_KBPS:
            issues.append(f"audio bitrate {info.audio_kbps} kbps is below {_MIN_AUDIO_KBPS}")
    if info.edit_lists:
        issues.append(f"{info.edit_lists} MP4 edit-list atom(s); edit lists are unsupported")
    return issues


def build_ffmpeg_command(
    src: Path, dst: Path, info: MediaInfo, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """The re-encode: matrix-correct Rec.709, stereo AAC, no edit lists, faststart."""
    matrix = "bt601" if info.color_space in _BT601_TAGS else "bt709"
    # ffmpeg now reads colour tags from the frames, not the encoder flags, so the
    # tags are set on the frames too; otherwise the primaries come out untagged.
    scale = (
        f"scale=in_color_matrix={matrix}:out_color_matrix=bt709:in_range=tv:out_range=tv,"
        "format=yuv420p,"
        "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
    )
    keyframe_every = str(max(1, round(info.fps or 24)))
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-maxrate",
        "12M",
        "-bufsize",
        "24M",
        "-g",
        keyframe_every,
        "-keyint_min",
        keyframe_every,
        "-sc_threshold",
        "0",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-use_editlist",
        "0",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        str(dst),
    ]


def prepare_for_spotify(
    src: Path,
    dst: Path,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    run: Runner = subprocess.run,
) -> MediaInfo:
    """Re-encode ``src`` to ``dst`` and prove the result meets Spotify's specs.

    Encodes to a ``.partial`` sibling and renames only after verification, so a failed
    or non-compliant encode never leaves a file under the final name.
    """
    if not src.is_file():
        raise ValidationError(f"video file not found: {src}")
    if dst.resolve() == src.resolve():
        raise ValidationError("output path must differ from the source video")

    source_info = probe_media(src, ffprobe=ffprobe, run=run)
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_name(f"{dst.stem}.partial{dst.suffix}")
    try:
        try:
            result = run(
                build_ffmpeg_command(src, partial, source_info, ffmpeg=ffmpeg),
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ConfigurationError(f"ffmpeg not found ({ffmpeg}); install ffmpeg") from exc
        if result.returncode != 0:
            raise UploaderError(f"ffmpeg failed: {result.stderr.strip()[-400:]}")
        prepared = probe_media(partial, ffprobe=ffprobe, run=run)
        issues = spec_issues(prepared)
        if issues:
            raise UploaderError("prepared video still misses Spotify's specs: " + "; ".join(issues))
        os.replace(partial, dst)
        return prepared
    finally:
        partial.unlink(missing_ok=True)
