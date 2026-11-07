"""End-to-end uploader behaviour against a routed fake Spotify site."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Request

from fake_spotify import SHOW_ID, FakeBehavior, FakeSpotify, install_fake_spotify
from spotify_uploader import shows
from spotify_uploader.config import UploaderConfig
from spotify_uploader.errors import (
    AuthExpiredError,
    ConfigurationError,
    ShowSetupError,
    UploadError,
    ValidationError,
)
from spotify_uploader.ledger import Ledger
from spotify_uploader.models import (
    EpisodeRequest,
    ProgressCallback,
    PublishedEpisode,
    PublishStatus,
    sha256_file,
)
from spotify_uploader.shows import ShowInfo
from spotify_uploader.uploader import SpotifyCreatorsUploader

pytestmark = pytest.mark.usefixtures("require_chromium")


def _request(video_file: Path, **overrides: object) -> EpisodeRequest:
    values: dict[str, object] = {
        "video_path": video_file,
        "title": "Test Episode",
        "description": "A test description.",
    }
    values.update(overrides)
    return EpisodeRequest(**values)  # type: ignore[arg-type]


async def _publish(
    config: UploaderConfig,
    request: EpisodeRequest,
    behavior: FakeBehavior | None = None,
    *,
    show_id: str = SHOW_ID,
    dry_run: bool = False,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
    resume_episode_id: str | None = None,
) -> tuple[PublishedEpisode, FakeSpotify]:
    holder: dict[str, FakeSpotify] = {}

    async def setup(context: BrowserContext) -> None:
        holder["fake"] = await install_fake_spotify(context, show_id, behavior)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    episode = await uploader.publish(
        request,
        dry_run=dry_run,
        force=force,
        on_progress=on_progress,
        resume_episode_id=resume_episode_id,
    )
    return episode, holder["fake"]


def _seed_ledger(config: UploaderConfig, sha256: str) -> PublishedEpisode:
    previous = PublishedEpisode(
        status=PublishStatus.PUBLISHED,
        title="Original Episode",
        show_id=SHOW_ID,
        sha256=sha256,
        published_at="2026-01-01T00:00:00+00:00",
        episode_id="ep-old",
        url=f"https://creators.spotify.com/pod/show/{SHOW_ID}/episode/ep-old",
    )
    Ledger(config.ledger_path).record(previous)
    return previous


async def test_publish_happy_path_records_ledger(config: UploaderConfig, video_file: Path) -> None:
    request = _request(video_file)
    progress: list[float] = []

    episode, fake = await _publish(
        config, request, on_progress=lambda fraction, _detail: progress.append(fraction)
    )

    assert episode.status == PublishStatus.PUBLISHED
    assert episode.title == request.title
    assert episode.show_id == SHOW_ID
    assert episode.sha256 == sha256_file(video_file)
    assert episode.episode_id is not None
    assert episode.url is not None
    assert episode.episode_id in episode.url
    assert episode.published_at

    stored = Ledger(config.ledger_path).get(episode.sha256)
    assert stored is not None
    assert stored.status == PublishStatus.PUBLISHED
    assert stored.episode_id == episode.episode_id

    published = [event for event in fake.events if event["type"] == "published"]
    assert len(published) == 1
    assert published[0]["title"] == request.title
    assert published[0]["description"] == request.description
    assert published[0]["filename"] == "episode.mp4"

    assert progress
    assert progress == sorted(progress)
    assert progress[-1] >= 0.99


async def test_dry_run_never_publishes_or_records(config: UploaderConfig, video_file: Path) -> None:
    episode, fake = await _publish(config, _request(video_file), dry_run=True)

    assert episode.status == PublishStatus.DRY_RUN
    assert episode.episode_id is None
    assert episode.url is None
    assert Ledger(config.ledger_path).get(episode.sha256) is None
    assert "published" not in [event["type"] for event in fake.events]


async def test_dry_run_ignores_ledger_and_leaves_it_untouched(
    config: UploaderConfig, video_file: Path
) -> None:
    previous = _seed_ledger(config, sha256_file(video_file))
    episode, fake = await _publish(config, _request(video_file), dry_run=True)

    assert episode.status == PublishStatus.DRY_RUN
    assert "published" not in [event["type"] for event in fake.events]
    stored = Ledger(config.ledger_path).get(previous.sha256)
    assert stored is not None
    assert stored.status == PublishStatus.PUBLISHED
    assert stored.published_at == previous.published_at


async def test_expired_session_raises_auth_error(config: UploaderConfig, video_file: Path) -> None:
    with pytest.raises(AuthExpiredError):
        await _publish(config, _request(video_file), FakeBehavior(expired_session=True))


async def test_upload_failure_saves_artifacts(
    make_config, tmp_path: Path, video_file: Path
) -> None:
    config = make_config(debug_dir=tmp_path / "debug")
    with pytest.raises(UploadError) as info:
        await _publish(config, _request(video_file), FakeBehavior(upload_fails=True))

    error = info.value
    assert error.step == "upload"
    assert error.artifacts is not None
    assert error.artifacts.name.endswith("-upload")
    for name in ("screenshot.png", "page.html", "trace.zip"):
        assert (error.artifacts / name).is_file(), name


async def test_debug_dir_does_not_save_artifacts_on_success(
    make_config, tmp_path: Path, video_file: Path
) -> None:
    debug_dir = tmp_path / "debug"
    config = make_config(debug_dir=debug_dir)
    episode, _ = await _publish(config, _request(video_file))

    assert episode.status == PublishStatus.PUBLISHED
    assert not debug_dir.exists() or not any(debug_dir.iterdir())


async def test_existing_title_is_already_published(
    config: UploaderConfig, video_file: Path
) -> None:
    behavior = FakeBehavior(existing_titles=("Test Episode",))
    episode, fake = await _publish(config, _request(video_file), behavior)

    assert episode.status == PublishStatus.ALREADY_PUBLISHED
    assert episode.episode_id == "ep-1"
    assert episode.url == f"https://creators.spotify.com/pod/show/{SHOW_ID}/episode/ep-1"
    assert "wizard_opened" not in [event["type"] for event in fake.events]


async def test_a_draft_with_the_same_title_is_not_a_published_duplicate(
    config: UploaderConfig, video_file: Path
) -> None:
    behavior = FakeBehavior(existing_drafts=("Test Episode",))
    episode, fake = await _publish(config, _request(video_file), behavior)

    assert episode.status == PublishStatus.PUBLISHED
    assert "wizard_opened" in [event["type"] for event in fake.events]


async def test_ledger_hit_skips_browser(config: UploaderConfig, video_file: Path) -> None:
    previous = _seed_ledger(config, sha256_file(video_file))
    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    episode = await uploader.publish(_request(video_file))

    assert calls == []
    assert episode.status == PublishStatus.ALREADY_PUBLISHED
    assert episode.episode_id == previous.episode_id
    assert episode.published_at == previous.published_at


async def test_force_bypasses_ledger_and_title_guard(
    config: UploaderConfig, video_file: Path
) -> None:
    _seed_ledger(config, sha256_file(video_file))
    holder: dict[str, FakeSpotify] = {}
    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")
        holder["fake"] = await install_fake_spotify(
            context, SHOW_ID, FakeBehavior(existing_titles=("Test Episode",))
        )

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    episode = await uploader.publish(_request(video_file), force=True)

    assert calls == ["context_setup"]
    assert episode.status == PublishStatus.PUBLISHED
    assert "wizard_opened" in [event["type"] for event in holder["fake"].events]


async def test_processing_hint_sets_processing_status(
    config: UploaderConfig, video_file: Path
) -> None:
    behavior = FakeBehavior(processing_after_publish=True)
    episode, _ = await _publish(config, _request(video_file), behavior)

    assert episode.status == PublishStatus.PROCESSING
    stored = Ledger(config.ledger_path).get(episode.sha256)
    assert stored is not None
    assert stored.status == PublishStatus.PROCESSING

    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    again = await SpotifyCreatorsUploader(config, context_setup=setup).publish(_request(video_file))
    assert calls == []
    assert again.status == PublishStatus.ALREADY_PUBLISHED


async def test_publishing_the_same_video_to_two_shows_is_not_skipped(
    make_config, video_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unknown_show(_page, show_id: str) -> ShowInfo:
        return ShowInfo(show_id)

    monkeypatch.setattr(shows, "fetch_show_info", unknown_show)
    request = _request(video_file)
    first_config = make_config(show_id="show-one")
    second_config = make_config(show_id="show-two")

    first, first_fake = await _publish(first_config, request, show_id="show-one")
    second, second_fake = await _publish(second_config, request, show_id="show-two")

    assert first.status == PublishStatus.PUBLISHED
    assert second.status == PublishStatus.PUBLISHED
    assert first.show_id == "show-one"
    assert second.show_id == "show-two"
    assert "wizard_opened" in [event["type"] for event in first_fake.events]
    assert "wizard_opened" in [event["type"] for event in second_fake.events]

    sha256 = sha256_file(video_file)
    ledger = Ledger(first_config.ledger_path)
    assert ledger.get(sha256, "show-one") is not None
    assert ledger.get(sha256, "show-two") is not None
    assert len(ledger.all()) == 2


async def test_every_browser_context_uses_the_english_locale(config: UploaderConfig) -> None:
    accept_language: list[str] = []

    async def setup(context: BrowserContext) -> None:
        def remember(request: Request) -> None:
            accept_language.append(request.headers.get("accept-language", ""))

        context.on("request", remember)
        await install_fake_spotify(context, SHOW_ID)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)

    assert await uploader.check_session() == SHOW_ID
    assert accept_language
    assert any(value.lower().startswith("en-us") for value in accept_language)


@pytest.mark.parametrize("case", ["missing", "suffix", "title"])
async def test_invalid_request_fails_before_browser(
    config: UploaderConfig, video_file: Path, tmp_path: Path, case: str
) -> None:
    if case == "missing":
        request = _request(tmp_path / "does-not-exist.mp4")
    elif case == "suffix":
        bad_video = tmp_path / "clip.avi"
        bad_video.write_bytes(b"not a video")
        request = _request(bad_video)
    else:
        request = _request(video_file, title="   ")

    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    with pytest.raises(ValidationError):
        await SpotifyCreatorsUploader(config, context_setup=setup).publish(request)
    assert calls == []


async def test_missing_show_id_is_configuration_error(make_config, video_file: Path) -> None:
    config = make_config(show_id=None)
    with pytest.raises(ConfigurationError, match="show id unknown"):
        await SpotifyCreatorsUploader(config).publish(_request(video_file))


async def test_missing_session_file_is_configuration_error(
    config: UploaderConfig, video_file: Path
) -> None:
    config.storage_state_path.unlink()
    with pytest.raises(ConfigurationError, match="no saved session"):
        await SpotifyCreatorsUploader(config).publish(_request(video_file))


async def test_check_session_ok(config: UploaderConfig) -> None:
    async def setup(context: BrowserContext) -> None:
        await install_fake_spotify(context, SHOW_ID)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    assert await uploader.check_session() == SHOW_ID


async def test_check_session_expired(config: UploaderConfig) -> None:
    async def setup(context: BrowserContext) -> None:
        await install_fake_spotify(context, SHOW_ID, FakeBehavior(expired_session=True))

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    with pytest.raises(AuthExpiredError):
        await uploader.check_session()


async def test_a_refused_publish_names_the_draft_to_resume(
    make_config, video_file: Path, tmp_path: Path
) -> None:
    config = make_config(step_timeout_s=1.0, debug_dir=tmp_path / "debug")
    with pytest.raises(UploadError) as info:
        await _publish(config, _request(video_file), FakeBehavior(reject_publish=True))

    assert info.value.step == "publish"
    assert "--resume-draft draft-" in str(info.value)
    assert info.value.artifacts is not None


async def test_unfinished_show_setup_surfaces_as_a_configuration_error(
    config: UploaderConfig, video_file: Path, tmp_path: Path
) -> None:
    with pytest.raises(ShowSetupError) as info:
        await _publish(config, _request(video_file), FakeBehavior(setup_required=True))

    assert isinstance(info.value, ConfigurationError)
    assert info.value.draft_episode_id is not None
    assert Ledger(config.ledger_path).all() == []


async def test_resume_publishes_the_draft_and_records_it(
    config: UploaderConfig, video_file: Path
) -> None:
    episode, fake = await _publish(config, _request(video_file), resume_episode_id="draft-xyz")

    assert episode.status == PublishStatus.PUBLISHED
    assert episode.episode_id == "draft-xyz"
    assert "file_set" not in [event["type"] for event in fake.events]
    stored = Ledger(config.ledger_path).get(episode.sha256)
    assert stored is not None and stored.episode_id == "draft-xyz"


async def test_a_bad_resume_id_is_rejected_before_any_browser_work(
    config: UploaderConfig, video_file: Path
) -> None:
    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    with pytest.raises(ValidationError, match="draft episode id"):
        await uploader.publish(_request(video_file), resume_episode_id="../evil")

    assert calls == []


async def test_an_unreachable_site_is_an_upload_error_not_a_raw_playwright_error(
    config: UploaderConfig, video_file: Path
) -> None:
    async def setup(context: BrowserContext) -> None:
        async def refuse(route) -> None:
            await route.abort("connectionrefused")

        await context.route("https://creators.spotify.com/**", refuse)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    with pytest.raises(UploadError) as info:
        await uploader.publish(_request(video_file))

    assert info.value.step == "open"


async def test_the_expired_session_fake_stays_inside_the_fixture(
    config: UploaderConfig,
) -> None:
    holder: dict[str, FakeSpotify] = {}

    async def setup(context: BrowserContext) -> None:
        holder["fake"] = await install_fake_spotify(
            context, SHOW_ID, FakeBehavior(expired_session=True)
        )

    with pytest.raises(AuthExpiredError):
        await SpotifyCreatorsUploader(config, context_setup=setup).check_session()

    assert holder["fake"].network_leaks == []


def _png(width: int, height: int) -> bytes:
    """A real solid-black PNG; the browser must be able to decode it."""

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


def _thumbnail_file(tmp_path: Path) -> Path:
    path = tmp_path / "thumbnail.png"
    path.write_bytes(_png(1280, 720))
    return path


async def test_publish_with_a_thumbnail_end_to_end(
    config: UploaderConfig, video_file: Path, tmp_path: Path
) -> None:
    thumbnail = _thumbnail_file(tmp_path)
    request = _request(video_file, thumbnail_path=thumbnail)

    episode, fake = await _publish(config, request)

    assert episode.status == PublishStatus.PUBLISHED
    types = [event["type"] for event in fake.events]
    assert types.index("thumbnail_set") < types.index("published")
    published = [event for event in fake.events if event["type"] == "published"]
    assert len(published) == 1
    assert published[0]["thumbnail"] == thumbnail.name
    assert published[0]["thumbnail_kind"] == "custom"


async def test_a_bad_thumbnail_is_rejected_before_the_browser(
    config: UploaderConfig, video_file: Path, tmp_path: Path
) -> None:
    square = tmp_path / "square.png"
    square.write_bytes(_png(1080, 1080))
    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)
    with pytest.raises(ValidationError, match="16:9"):
        await uploader.publish(_request(video_file, thumbnail_path=square))

    assert calls == []
