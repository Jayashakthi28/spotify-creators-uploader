"""Wizard behaviour against the local fake Spotify site."""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Page

from fake_spotify import SHOW_ID, FakeBehavior, FakeSpotify
from spotify_uploader.errors import AuthExpiredError, ShowSetupError, UploadError
from spotify_uploader.models import EpisodeRequest, ProgressCallback
from spotify_uploader.wizard import EpisodeWizard, WizardResult

pytestmark = pytest.mark.usefixtures("require_chromium")


def _request(video_file: Path, **overrides: object) -> EpisodeRequest:
    values: dict[str, object] = {
        "video_path": video_file,
        "title": "Test Episode",
        "description": "A test description.",
    }
    values.update(overrides)
    return EpisodeRequest(**values)  # type: ignore[arg-type]


async def _run(
    page: Page,
    request: EpisodeRequest,
    *,
    publish: bool = True,
    on_progress: ProgressCallback | None = None,
    step_timeout_s: float = 5.0,
    resume_episode_id: str | None = None,
) -> WizardResult:
    wizard = EpisodeWizard(
        page,
        show_id=SHOW_ID,
        step_timeout_s=step_timeout_s,
        upload_timeout_s=10.0,
        on_progress=on_progress,
    )
    return await wizard.run(request, publish=publish, resume_episode_id=resume_episode_id)


def _published_events(fake: FakeSpotify) -> list[dict[str, Any]]:
    return [event for event in fake.events if event["type"] == "published"]


def _thumbnail_file(tmp_path: Path, *, width: int = 1280, height: int = 720) -> Path:
    """A real 16:9 PNG, so the browser can decode it into a naturalWidth."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    row = b"\x00" + b"\x00\x00\x00\xff" * width
    path = tmp_path / "thumbnail.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
    return path


async def test_happy_path_publishes_immediately(page_factory, video_file: Path) -> None:
    page, fake = await page_factory()
    result = await _run(page, _request(video_file))

    assert result.processing is False
    assert result.episode_id is not None
    assert result.url is not None
    assert result.episode_id in result.url

    types = [event["type"] for event in fake.events]
    assert types.count("wizard_opened") == 1
    assert "file_set" in types
    assert types.index("publish_now_selected") < types.index("published")
    file_set = next(event for event in fake.events if event["type"] == "file_set")
    assert result.episode_id == file_set["episode_id"]

    published = _published_events(fake)
    assert len(published) == 1
    event = published[0]
    assert event["title"] == "Test Episode"
    assert event["description"] == "A test description."
    assert event["filename"] == "episode.mp4"
    assert event["explicit"] is False


async def test_dry_run_stops_before_publish(page_factory, video_file: Path) -> None:
    page, fake = await page_factory()
    result = await _run(page, _request(video_file), publish=False)

    assert result == WizardResult(episode_id=None, url=None, processing=False)
    types = [event["type"] for event in fake.events]
    assert "publish_now_selected" in types
    assert "published" not in types


async def test_expired_session_raises_auth_expired(page_factory, video_file: Path) -> None:
    page, _ = await page_factory(FakeBehavior(expired_session=True))
    with pytest.raises(AuthExpiredError):
        await _run(page, _request(video_file))


async def test_upload_failure_reports_upload_step(page_factory, video_file: Path) -> None:
    page, fake = await page_factory(FakeBehavior(upload_fails=True))
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file))

    assert info.value.step == "upload"
    assert _published_events(fake) == []


@pytest.mark.parametrize("mode", ["normal", "reverts"])
async def test_description_sticks(page_factory, video_file: Path, mode: str) -> None:
    page, fake = await page_factory(FakeBehavior(description_mode=mode))
    description = "A description that must survive React"
    result = await _run(page, _request(video_file, description=description))

    assert result.episode_id is not None
    published = _published_events(fake)
    assert len(published) == 1
    assert published[0]["description"] == description

    types = [event["type"] for event in fake.events]
    if mode == "normal":
        assert "fiber_insert" not in types
    else:
        assert "fiber_insert" in types


async def test_broken_description_never_publishes(page_factory, video_file: Path) -> None:
    page, fake = await page_factory(FakeBehavior(description_mode="broken"))
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file))

    assert info.value.step == "description"
    assert _published_events(fake) == []


async def test_explicit_switch_is_turned_on_when_requested(page_factory, video_file: Path) -> None:
    page, fake = await page_factory()
    await _run(page, _request(video_file, explicit=True))

    assert "explicit_selected" in [event["type"] for event in fake.events]
    assert _published_events(fake)[0]["explicit"] is True


async def test_explicit_switch_is_left_alone_when_not_requested(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory()
    await _run(page, _request(video_file, explicit=False))

    assert "explicit_selected" not in [event["type"] for event in fake.events]
    assert _published_events(fake)[0]["explicit"] is False


async def test_missing_explicit_switch_is_fine_for_a_clean_episode(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(show_explicit_toggle=False))
    await _run(page, _request(video_file, explicit=False))

    assert len(_published_events(fake)) == 1


async def test_missing_explicit_switch_blocks_an_explicit_episode(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(show_explicit_toggle=False))
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file, explicit=True))

    assert info.value.step == "content-check"
    assert _published_events(fake) == []


async def test_waits_for_preview_ready_not_just_the_form(page_factory, video_file: Path) -> None:
    page, fake = await page_factory(FakeBehavior(upload_delay_ms=700))
    await _run(page, _request(video_file))

    types = [event["type"] for event in fake.events]
    assert types.index("preview_ready") < types.index("next_clicked")
    next_clicked = next(event for event in fake.events if event["type"] == "next_clicked")
    assert next_clicked["preview_ready"] is True


async def test_publish_is_not_reported_until_the_page_leaves_the_wizard(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(reject_publish=True))
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file), step_timeout_s=1.0)

    assert info.value.step == "publish"
    assert "publish_rejected" in [event["type"] for event in fake.events]
    assert _published_events(fake) == []


async def test_progress_is_monotonic_and_finishes_near_one(page_factory, video_file: Path) -> None:
    page, _ = await page_factory()
    seen: list[tuple[float, str]] = []

    def progress(fraction: float, detail: str) -> None:
        seen.append((fraction, detail))

    await _run(page, _request(video_file), on_progress=progress)

    fractions = [fraction for fraction, _ in seen]
    assert fractions
    assert all(0.0 <= fraction <= 1.0 for fraction in fractions)
    assert fractions == sorted(fractions)
    assert fractions[0] <= 0.1
    assert fractions[-1] >= 0.99


async def test_unfinished_show_setup_fails_fast_with_the_draft_id(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(setup_required=True))
    started = time.monotonic()
    with pytest.raises(ShowSetupError) as info:
        await _run(page, _request(video_file))

    assert time.monotonic() - started < 15
    draft = info.value.draft_episode_id
    assert draft is not None and draft.startswith("draft-")
    assert f"--resume-draft {draft}" in str(info.value)
    assert "setup_required" in [event["type"] for event in fake.events]
    assert _published_events(fake) == []


async def test_resume_reopens_the_draft_without_uploading_again(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory()
    result = await _run(page, _request(video_file), resume_episode_id="draft-abc123")

    types = [event["type"] for event in fake.events]
    assert "resumed" in types
    assert "file_set" not in types
    assert result.episode_id == "draft-abc123"
    published = _published_events(fake)
    assert len(published) == 1
    assert published[0]["episode_id"] == "draft-abc123"
    assert published[0]["title"] == "Test Episode"


async def test_a_custom_thumbnail_is_set_before_next(
    page_factory, video_file: Path, tmp_path: Path
) -> None:
    page, fake = await page_factory()
    thumbnail = _thumbnail_file(tmp_path)
    await _run(page, _request(video_file, thumbnail_path=thumbnail))

    types = [event["type"] for event in fake.events]
    assert types.count("file_set") == 1
    assert "thumbnail_deleted" not in types
    assert types.index("thumbnail_set") < types.index("next_clicked")
    thumbnail_event = next(event for event in fake.events if event["type"] == "thumbnail_set")
    assert thumbnail_event["filename"] == thumbnail.name
    assert thumbnail_event["width"] == 1280
    published = _published_events(fake)
    assert len(published) == 1
    assert published[0]["thumbnail"] == thumbnail.name
    assert published[0]["thumbnail_kind"] == "custom"


async def test_without_a_thumbnail_spotify_keeps_its_auto_frame(
    page_factory, video_file: Path
) -> None:
    page, fake = await page_factory()
    await _run(page, _request(video_file))

    types = [event["type"] for event in fake.events]
    assert "thumbnail_set" not in types
    assert "thumbnail_deleted" not in types
    published = _published_events(fake)
    assert len(published) == 1
    assert published[0]["thumbnail"] is None
    assert published[0]["thumbnail_kind"] == "auto"


async def test_a_thumbnail_that_never_uploads_blocks_next(
    page_factory, video_file: Path, tmp_path: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(thumbnail_fails=True))
    thumbnail = _thumbnail_file(tmp_path)
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file, thumbnail_path=thumbnail), step_timeout_s=1.0)

    assert info.value.step == "thumbnail"
    assert "still present" in str(info.value)
    types = [event["type"] for event in fake.events]
    assert "next_clicked" not in types
    assert _published_events(fake) == []


async def test_a_missing_thumbnail_section_blocks_the_upload(
    page_factory, video_file: Path, tmp_path: Path
) -> None:
    page, fake = await page_factory(FakeBehavior(thumbnail_ui=False))
    thumbnail = _thumbnail_file(tmp_path)
    with pytest.raises(UploadError) as info:
        await _run(page, _request(video_file, thumbnail_path=thumbnail), step_timeout_s=1.0)

    assert info.value.step == "thumbnail"
    types = [event["type"] for event in fake.events]
    assert "next_clicked" not in types
    assert _published_events(fake) == []


@pytest.mark.parametrize("existing", [False, True])
async def test_resume_with_a_thumbnail(
    page_factory, video_file: Path, tmp_path: Path, existing: bool
) -> None:
    page, fake = await page_factory(FakeBehavior(thumbnail_existing=existing))
    thumbnail = _thumbnail_file(tmp_path)
    result = await _run(
        page,
        _request(video_file, thumbnail_path=thumbnail),
        resume_episode_id="draft-abc123",
    )

    types = [event["type"] for event in fake.events]
    assert "file_set" not in types
    assert types.count("thumbnail_set") == 1
    assert types.index("thumbnail_set") < types.index("next_clicked")
    if existing:
        assert "thumbnail_deleted" in types
        assert types.index("thumbnail_deleted") < types.index("thumbnail_set")
    else:
        assert "thumbnail_deleted" not in types
    assert result.episode_id == "draft-abc123"
    published = _published_events(fake)
    assert len(published) == 1
    assert published[0]["episode_id"] == "draft-abc123"
    assert published[0]["thumbnail"] == thumbnail.name
    assert published[0]["thumbnail_kind"] == "custom"


async def test_a_dry_run_with_a_thumbnail_stops_before_publish(
    page_factory, video_file: Path, tmp_path: Path
) -> None:
    page, fake = await page_factory()
    thumbnail = _thumbnail_file(tmp_path)
    result = await _run(page, _request(video_file, thumbnail_path=thumbnail), publish=False)

    assert result == WizardResult(episode_id=None, url=None, processing=False)
    types = [event["type"] for event in fake.events]
    assert "thumbnail_set" in types
    assert "next_clicked" in types
    assert "publish_now_selected" in types
    assert "published" not in types


async def test_progress_is_monotonic_with_a_thumbnail(
    page_factory, video_file: Path, tmp_path: Path
) -> None:
    page, _ = await page_factory()
    thumbnail = _thumbnail_file(tmp_path)
    seen: list[tuple[float, str]] = []

    def progress(fraction: float, detail: str) -> None:
        seen.append((fraction, detail))

    await _run(page, _request(video_file, thumbnail_path=thumbnail), on_progress=progress)

    fractions = [fraction for fraction, _ in seen]
    assert fractions == sorted(fractions)
    assert any(detail == "thumbnail set" for _, detail in seen)
    assert fractions[-1] >= 0.99
