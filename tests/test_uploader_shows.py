"""Show preflight and show-discovery behaviour with the dashboard stubbed out.

The fake site serves the wizard and episode pages only, so every dashboard read is
monkeypatched here. These tests pin the contract that show state is advisory: it
never blocks a publish unless Spotify explicitly says the show's setup is
unfinished, and even then only for a real publish.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import BrowserContext

from fake_spotify import SHOW_ID, FakeBehavior, FakeSpotify, install_fake_spotify
from spotify_uploader import shows
from spotify_uploader.config import UploaderConfig
from spotify_uploader.errors import AuthExpiredError, ConfigurationError, ShowSetupError
from spotify_uploader.models import EpisodeRequest, PublishStatus
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


def _uploader(
    config: UploaderConfig, behavior: FakeBehavior | None = None
) -> tuple[SpotifyCreatorsUploader, dict[str, FakeSpotify]]:
    holder: dict[str, FakeSpotify] = {}

    async def setup(context: BrowserContext) -> None:
        holder["fake"] = await install_fake_spotify(context, SHOW_ID, behavior)

    return SpotifyCreatorsUploader(config, context_setup=setup), holder


def _incomplete_show(name: str = "My Show"):
    async def fetch(_page, show_id: str) -> ShowInfo:
        return ShowInfo(show_id, name, setup_complete=False)

    return fetch


async def test_incomplete_setup_blocks_a_real_publish_before_the_wizard(
    config: UploaderConfig, video_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    visited: list[str] = []

    async def fetch(page, show_id: str) -> ShowInfo:
        visited.append(page.url)
        return ShowInfo(show_id, "My Show", setup_complete=False)

    monkeypatch.setattr(shows, "fetch_show_info", fetch)
    uploader, holder = _uploader(config)
    progress: list[tuple[float, str]] = []

    with pytest.raises(ShowSetupError) as info:
        await uploader.publish(
            _request(video_file),
            resume_episode_id=None,
            on_progress=lambda fraction, detail: progress.append((fraction, detail)),
        )

    error = info.value
    assert error.draft_episode_id is None
    assert "show 'My Show'" in str(error)
    assert SHOW_ID in str(error)
    assert "Set up your show" in str(error)
    assert "name, description, cover art" in str(error)

    # The preflight runs on the episodes page, after the successful session check.
    assert visited == [f"https://creators.spotify.com/pod/show/{SHOW_ID}/episodes"]
    assert progress and progress[0] == (0.01, f"publishing to show 'My Show' ({SHOW_ID})")

    events = [event["type"] for event in holder["fake"].events]
    assert "wizard_opened" not in events
    assert "file_set" not in events


async def test_incomplete_setup_only_warns_on_a_dry_run(
    config: UploaderConfig, video_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shows, "fetch_show_info", _incomplete_show())
    uploader, holder = _uploader(config)
    progress: list[tuple[float, str]] = []

    episode = await uploader.publish(
        _request(video_file),
        dry_run=True,
        on_progress=lambda fraction, detail: progress.append((fraction, detail)),
    )

    assert episode.status == PublishStatus.DRY_RUN
    details = [detail for _, detail in progress]
    assert any("not fully set up" in detail for detail in details)
    assert any("warning" in detail for detail in details)
    events = [event["type"] for event in holder["fake"].events]
    assert "wizard_opened" in events
    assert "published" not in events
    assert [fraction for fraction, _ in progress] == sorted(fraction for fraction, _ in progress)


@pytest.mark.parametrize("failure", ["unknown", "raises"])
async def test_an_unknown_or_unreadable_show_state_never_blocks(
    config: UploaderConfig,
    video_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    if failure == "unknown":

        async def probe(_page, show_id: str) -> ShowInfo:
            return ShowInfo(show_id)
    else:

        async def probe(_page, show_id: str) -> ShowInfo:
            raise RuntimeError("the dashboard is unavailable")

    monkeypatch.setattr(shows, "fetch_show_info", probe)
    uploader, holder = _uploader(config)
    details: list[str] = []

    episode = await uploader.publish(
        _request(video_file),
        on_progress=lambda _fraction, detail: details.append(detail),
    )

    assert episode.status == PublishStatus.PUBLISHED
    assert "wizard_opened" in [event["type"] for event in holder["fake"].events]
    assert f"publishing to show {SHOW_ID}" in details


async def test_auth_expiry_from_the_preflight_propagates(
    config: UploaderConfig, video_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def expired(_page, _show_id: str) -> ShowInfo:
        raise AuthExpiredError("Spotify session expired")

    monkeypatch.setattr(shows, "fetch_show_info", expired)
    uploader, holder = _uploader(config)

    with pytest.raises(AuthExpiredError):
        await uploader.publish(_request(video_file))

    assert "wizard_opened" not in [event["type"] for event in holder["fake"].events]


async def test_resume_with_incomplete_setup_reports_the_draft_id(
    config: UploaderConfig, video_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shows, "fetch_show_info", _incomplete_show())
    uploader, holder = _uploader(config)

    with pytest.raises(ShowSetupError) as info:
        await uploader.publish(_request(video_file), resume_episode_id="draft-xyz")

    assert info.value.draft_episode_id == "draft-xyz"
    assert "draft-xyz" in str(info.value)
    events = [event["type"] for event in holder["fake"].events]
    assert "wizard_opened" not in events
    assert "file_set" not in events


async def test_inspect_show_returns_the_dashboard_state_and_closes(
    config: UploaderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fetch(_page, show_id: str) -> ShowInfo:
        return ShowInfo(show_id, "My Show", setup_complete=True)

    monkeypatch.setattr(shows, "fetch_show_info", fetch)
    closed: list[str] = []

    async def setup(context: BrowserContext) -> None:
        context.on("close", lambda *_: closed.append("context"))
        await install_fake_spotify(context, SHOW_ID)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)

    assert await uploader.inspect_show() == ShowInfo(SHOW_ID, "My Show", setup_complete=True)
    assert closed == ["context"]


async def test_inspect_show_propagates_an_expired_session(
    config: UploaderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def expired(_page, _show_id: str) -> ShowInfo:
        raise AuthExpiredError("Spotify session expired")

    monkeypatch.setattr(shows, "fetch_show_info", expired)
    uploader = SpotifyCreatorsUploader(config)

    with pytest.raises(AuthExpiredError):
        await uploader.inspect_show()


async def test_inspect_show_needs_a_configured_show_id(make_config) -> None:
    config = make_config(show_id=None)
    calls: list[str] = []

    async def setup(context: BrowserContext) -> None:
        calls.append("context_setup")

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)

    with pytest.raises(ConfigurationError, match="show id unknown"):
        await uploader.inspect_show()
    assert calls == []


async def test_list_shows_needs_no_show_id_and_closes(
    make_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(show_id=None)
    listed = [ShowInfo("show-a", "A"), ShowInfo("show-b", "B", setup_complete=True)]

    async def discover(_page) -> list[ShowInfo]:
        return listed

    monkeypatch.setattr(shows, "list_shows", discover)
    closed: list[str] = []

    async def setup(context: BrowserContext) -> None:
        context.on("close", lambda *_: closed.append("context"))
        await install_fake_spotify(context, SHOW_ID)

    uploader = SpotifyCreatorsUploader(config, context_setup=setup)

    assert await uploader.list_shows() == listed
    assert closed == ["context"]


async def test_list_shows_propagates_an_expired_session(
    config: UploaderConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def expired(_page) -> list[ShowInfo]:
        raise AuthExpiredError("Spotify session expired")

    monkeypatch.setattr(shows, "list_shows", expired)
    uploader = SpotifyCreatorsUploader(config)

    with pytest.raises(AuthExpiredError):
        await uploader.list_shows()
