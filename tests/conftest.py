"""Shared browser, session and video fixtures for the uploader tests.

Chromium is launched for real; everything Spotify-shaped is served by
``fake_spotify`` through ``context.route`` so no test touches the network.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from fake_spotify import ALL_FAKES, SHOW_ID, FakeBehavior, FakeSpotify, install_fake_spotify
from spotify_uploader.config import UploaderConfig

VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake video payload" * 32

#: ``(behavior) -> (page, fake)``; the fixture closes every context at teardown.
PageFactory = Callable[[FakeBehavior | None], Awaitable[tuple[Page, FakeSpotify]]]


def _probe_chromium() -> None:
    async def probe() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            await browser.close()

    asyncio.run(probe())


@pytest.fixture(scope="session")
def require_chromium() -> None:
    """Skip the browser-backed tests when Chromium cannot launch on this host."""
    try:
        _probe_chromium()
    except Exception as exc:
        pytest.skip(f"Chromium cannot launch: {exc}")


@pytest.fixture(autouse=True)
def no_network_leaks() -> Iterator[None]:
    """Fail any test whose browser tried to reach something the fake does not serve."""
    ALL_FAKES.clear()
    yield
    leaks = [url for fake in ALL_FAKES for url in fake.network_leaks]
    ALL_FAKES.clear()
    assert leaks == [], f"the browser reached outside the fake Spotify: {leaks}"


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    path = tmp_path / "episode.mp4"
    path.write_bytes(VIDEO_BYTES)
    return path


@pytest.fixture
def uploader_home(tmp_path: Path) -> Path:
    """A private-looking home with one saved session cookie and no config file."""
    home = tmp_path / "home"
    home.mkdir()
    state = {
        "cookies": [
            {
                "name": "sp_dc",
                "value": "fake-session-value",
                "domain": ".spotify.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    (home / "storage_state.json").write_text(json.dumps(state), encoding="utf-8")
    return home


@pytest.fixture
def make_config(uploader_home: Path) -> Callable[..., UploaderConfig]:
    def factory(**overrides: object) -> UploaderConfig:
        values: dict[str, object] = {
            "home": uploader_home,
            "show_id": SHOW_ID,
            "headless": True,
            "step_timeout_s": 5.0,
            "upload_timeout_s": 10.0,
        }
        values.update(overrides)
        return UploaderConfig(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def config(make_config: Callable[..., UploaderConfig]) -> UploaderConfig:
    return make_config()


@pytest.fixture
async def browser(require_chromium: None) -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


@pytest.fixture
async def page_factory(browser: Browser) -> AsyncIterator[PageFactory]:
    contexts: list[BrowserContext] = []

    async def factory(behavior: FakeBehavior | None = None) -> tuple[Page, FakeSpotify]:
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        fake = await install_fake_spotify(context, SHOW_ID, behavior)
        contexts.append(context)
        page = await context.new_page()
        return page, fake

    try:
        yield factory
    finally:
        for context in contexts:
            await context.close()
