"""Spotify spec checks and the re-encode, with a fake runner and with real ffmpeg."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from spotify_uploader import cli
from spotify_uploader.errors import ConfigurationError, UploaderError, ValidationError
from spotify_uploader.prepare import (
    MediaInfo,
    build_ffmpeg_command,
    count_edit_lists,
    prepare_for_spotify,
    probe_media,
    spec_issues,
)

GOOD = MediaInfo(
    video_codec="h264",
    width=1920,
    height=1080,
    fps=24.0,
    color_space="bt709",
    color_primaries="bt709",
    color_transfer="bt709",
    audio_codec="aac",
    audio_channels=2,
    audio_kbps=192,
    duration_s=60.0,
    edit_lists=0,
)


def _probe_payload(info: MediaInfo) -> dict[str, Any]:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": info.video_codec,
                "width": info.width,
                "height": info.height,
                "r_frame_rate": f"{round(info.fps)}/1",
                "color_space": info.color_space,
                "color_primaries": info.color_primaries,
                "color_transfer": info.color_transfer,
            },
            {
                "codec_type": "audio",
                "codec_name": info.audio_codec,
                "channels": info.audio_channels,
                "bit_rate": str((info.audio_kbps or 0) * 1000),
            },
        ],
        "format": {"duration": str(info.duration_s)},
    }


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_compliant_media_has_no_issues() -> None:
    assert spec_issues(GOOD) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"video_codec": "vp9"}, "codec"),
        ({"width": 1280, "height": 720}, "below 1080p"),
        ({"width": 1920, "height": 1200}, "16:9"),
        ({"fps": 29.97}, "frame rate"),
        ({"color_space": "smpte170m"}, "Rec.709"),
        ({"color_primaries": "bt470bg"}, "Rec.709"),
        ({"audio_channels": 1}, "stereo"),
        ({"audio_kbps": 96}, "bitrate"),
        ({"audio_codec": None, "audio_channels": 0}, "no audio"),
        ({"edit_lists": 2}, "edit-list"),
    ],
)
def test_each_spec_violation_is_reported(change: dict[str, Any], fragment: str) -> None:
    issues = spec_issues(replace(GOOD, **change))
    assert any(fragment in issue for issue in issues), issues


def test_the_composed_pipeline_video_fails_exactly_where_the_research_said() -> None:
    composed = replace(
        GOOD,
        color_space="smpte170m",
        color_primaries="bt470bg",
        audio_channels=1,
        audio_kbps=95,
        edit_lists=2,
    )
    joined = " ".join(spec_issues(composed))
    assert "Rec.709" in joined
    assert "stereo" in joined
    assert "bitrate" in joined
    assert "edit-list" in joined


def test_edit_lists_are_found_across_a_chunk_boundary(tmp_path: Path) -> None:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 5 + b"elst" + b"y" * 9 + b"elst" + b"z" * 3)
    assert count_edit_lists(path, chunk_bytes=7) == 2
    assert count_edit_lists(path) == 2


def test_no_edit_lists_counts_zero(tmp_path: Path) -> None:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"moov" * 100)
    assert count_edit_lists(path) == 0


@pytest.mark.parametrize(
    ("color_space", "expected"),
    [("smpte170m", "bt601"), ("bt470bg", "bt601"), ("bt709", "bt709"), (None, "bt709")],
)
def test_colour_matrix_is_converted_from_what_the_source_says(
    color_space: str | None, expected: str
) -> None:
    command = build_ffmpeg_command(
        Path("in.mp4"), Path("out.mp4"), replace(GOOD, color_space=color_space)
    )
    scale = command[command.index("-vf") + 1]
    assert f"in_color_matrix={expected}" in scale
    assert "out_color_matrix=bt709" in scale
    assert "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709" in scale


def test_command_follows_the_spec() -> None:
    command = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), replace(GOOD, fps=25.0))

    def value(flag: str) -> str:
        return command[command.index(flag) + 1]

    assert command[-1] == "out.mp4"
    assert value("-use_editlist") == "0"
    assert value("-movflags") == "+faststart"
    assert value("-ac") == "2"
    assert value("-b:a") == "192k"
    assert value("-g") == "25"
    assert command.count("-map") == 2
    assert "0:v:0" in command and "0:a:0" in command
    assert value("-colorspace") == "bt709"


def test_probe_media_parses_ffprobe_json(tmp_path: Path) -> None:
    path = tmp_path / "v.mp4"
    path.write_bytes(b"elst")

    def run(command: list[str], **_: Any) -> Any:
        return _completed(json.dumps(_probe_payload(replace(GOOD, audio_channels=1))))

    info = probe_media(path, run=run)

    assert info.audio_channels == 1
    assert info.audio_kbps == 192
    assert info.height == 1080
    assert info.fps == 24.0
    assert info.edit_lists == 1


def test_probe_media_reports_a_missing_ffprobe(tmp_path: Path) -> None:
    def run(command: list[str], **_: Any) -> Any:
        raise FileNotFoundError

    with pytest.raises(ConfigurationError, match="ffprobe"):
        probe_media(tmp_path / "v.mp4", run=run)


def test_probe_media_reports_a_failed_probe(tmp_path: Path) -> None:
    def run(command: list[str], **_: Any) -> Any:
        return _completed(returncode=1, stderr="Invalid data found")

    with pytest.raises(UploaderError, match="Invalid data"):
        probe_media(tmp_path / "v.mp4", run=run)


class FakeFfmpeg:
    """Stands in for ffmpeg/ffprobe: ffmpeg writes its output, ffprobe reports a preset."""

    def __init__(self, source: MediaInfo, produced: MediaInfo, *, ffmpeg_code: int = 0) -> None:
        self._source = source
        self._produced = produced
        self._ffmpeg_code = ffmpeg_code
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> Any:
        self.commands.append(command)
        if command[0] == "ffmpeg":
            if self._ffmpeg_code != 0:
                return _completed(returncode=self._ffmpeg_code, stderr="encoder exploded")
            Path(command[-1]).write_bytes(b"encoded")
            return _completed()
        info = self._produced if ".partial" in command[-1] else self._source
        return _completed(json.dumps(_probe_payload(info)))


@pytest.fixture
def source_video(tmp_path: Path) -> Path:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"source")
    return path


def test_prepare_publishes_the_output_only_after_it_verifies(
    tmp_path: Path, source_video: Path
) -> None:
    dst = tmp_path / "out" / "ready.mp4"
    fake = FakeFfmpeg(replace(GOOD, audio_channels=1), GOOD)

    info = prepare_for_spotify(source_video, dst, run=fake)

    assert spec_issues(info) == []
    assert dst.read_bytes() == b"encoded"
    assert not dst.with_name("ready.partial.mp4").exists()


def test_a_non_compliant_encode_leaves_nothing_behind(tmp_path: Path, source_video: Path) -> None:
    dst = tmp_path / "ready.mp4"
    fake = FakeFfmpeg(GOOD, replace(GOOD, audio_channels=1))

    with pytest.raises(UploaderError, match="still misses"):
        prepare_for_spotify(source_video, dst, run=fake)

    assert not dst.exists()
    assert not dst.with_name("ready.partial.mp4").exists()


def test_a_failed_encode_leaves_nothing_behind(tmp_path: Path, source_video: Path) -> None:
    dst = tmp_path / "ready.mp4"

    with pytest.raises(UploaderError, match="encoder exploded"):
        prepare_for_spotify(source_video, dst, run=FakeFfmpeg(GOOD, GOOD, ffmpeg_code=1))

    assert not dst.exists()
    assert not dst.with_name("ready.partial.mp4").exists()


def test_prepare_refuses_to_overwrite_its_source(source_video: Path) -> None:
    with pytest.raises(ValidationError, match="differ"):
        prepare_for_spotify(source_video, source_video, run=FakeFfmpeg(GOOD, GOOD))


def test_prepare_rejects_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not found"):
        prepare_for_spotify(tmp_path / "nope.mp4", tmp_path / "out.mp4", run=FakeFfmpeg(GOOD, GOOD))


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)
def test_real_ffmpeg_turns_a_composed_style_video_into_a_compliant_one(tmp_path: Path) -> None:
    source = tmp_path / "composed.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-colorspace", "smpte170m", "-color_primaries", "bt470bg", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", "96k", "-ac", "1", str(source),
        ],
        check=True,
    )  # fmt: skip
    before = spec_issues(probe_media(source))
    assert any("Rec.709" in issue for issue in before)
    assert any("stereo" in issue for issue in before)

    dst = tmp_path / "ready.mp4"
    info = prepare_for_spotify(source, dst)

    assert spec_issues(info) == []
    assert count_edit_lists(dst) == 0
    assert info.audio_channels == 2


def _cli_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_cli_check_reports_a_compliant_file(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "probe_media", lambda path: GOOD)

    assert cli.main(["prepare", "--check", str(tmp_path / "v.mp4")]) == 0
    payload = _cli_json(capsys)
    assert payload["compliant"] is True
    assert payload["issues"] == []


def test_cli_check_exits_2_with_the_issues(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "probe_media", lambda path: replace(GOOD, audio_channels=1))

    assert cli.main(["prepare", "--check", str(tmp_path / "v.mp4")]) == 2
    payload = _cli_json(capsys)
    assert payload["compliant"] is False
    assert any("stereo" in issue for issue in payload["issues"])


def test_cli_prepare_needs_an_output_path(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli.main(["prepare", str(tmp_path / "v.mp4")]) == 2
    assert "output path" in json.loads(capsys.readouterr().err)["message"]


def test_cli_prepare_runs_the_re_encode(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[Path, Path]] = []

    def fake_prepare(src: Path, dst: Path) -> MediaInfo:
        seen.append((src, dst))
        return GOOD

    monkeypatch.setattr(cli, "prepare_for_spotify", fake_prepare)

    assert cli.main(["prepare", str(tmp_path / "a.mp4"), str(tmp_path / "b.mp4")]) == 0
    assert seen == [(tmp_path / "a.mp4", tmp_path / "b.mp4")]
    assert _cli_json(capsys)["compliant"] is True
