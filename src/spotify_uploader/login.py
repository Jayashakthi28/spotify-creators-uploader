"""One-time browser login that saves a reusable Playwright session.

Google rejects sign-in inside Playwright-launched browsers ("this browser may
not be secure"), so the default mode starts the operator's real Chrome with a
dedicated profile and attaches over CDP only to read cookies. The saved session
is then reused by the headless uploader; no credentials ever pass through here.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, async_playwright

from spotify_uploader import selectors
from spotify_uploader.config import UploaderConfig, ensure_private_dir, save_show_id
from spotify_uploader.errors import ConfigurationError
from spotify_uploader.session import (
    KEY_COOKIE,
    SessionSummary,
    is_spotify_domain,
    normalize_storage_state,
    save_storage_state,
    summarize,
)

CHROME_ENV = "SPOTIFY_UPLOADER_CHROME"
CDP_HOST = "127.0.0.1"
CDP_READY_TIMEOUT_S = 30.0
SIGN_IN_POLL_S = 1.0
SHOW_ID_TIMEOUT_S = 15.0

StatusCallback = Callable[[str], None]
CdpFetch = Callable[[str], object]

_MAC_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_LINUX_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
_CREATORS_HOST = urlparse(selectors.BASE_URL).hostname


@dataclass(frozen=True, slots=True)
class LoginResult:
    """What a completed login found; ``show_id`` is None on a show-chooser page."""

    show_id: str | None
    state_path: Path
    summary: SessionSummary


def _ignore_status(message: str) -> None:
    """Default progress sink for callers that want a silent login."""
    del message


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_chrome(candidates: Sequence[Path | str] | None = None) -> Path | None:
    """Locate a chrome/chromium binary; ``candidates`` keeps tests hermetic.

    ``$SPOTIFY_UPLOADER_CHROME`` is authoritative when set, and inside the
    project's test suite the explicit candidates short-circuit platform probing.
    """
    override = os.environ.get(CHROME_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        return path if _is_executable(path) else None
    if candidates is not None:
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if _is_executable(path):
                return path
        return None
    paths: list[Path] = []
    if sys.platform == "darwin":
        paths.append(_MAC_CHROME)
    elif os.name == "nt":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name, "").strip()
            if base:
                paths.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    for name in _LINUX_CHROME_NAMES:
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    for path in paths:
        if _is_executable(path):
            return path
    return None


def build_chrome_args(chrome: Path, profile_dir: Path, port: int) -> list[str]:
    """Chrome 136+ ignores remote debugging on the default profile, so pass a separate one."""
    return [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        selectors.BASE_URL,
    ]


def _fetch_cdp_version(url: str) -> None:
    with urllib.request.urlopen(url, timeout=1.0) as response:
        response.read(1)


async def wait_for_cdp(
    port: int,
    timeout_s: float,
    fetch: CdpFetch | None = None,
) -> None:
    """Poll Chrome's DevTools endpoint until it accepts connections.

    ``fetch`` is injectable so tests never open a socket; it may be sync or
    async and must raise when Chrome is not ready yet.
    """
    fetcher = fetch or _fetch_cdp_version
    url = f"http://{CDP_HOST}:{port}/json/version"
    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    while True:
        try:
            result = fetcher(url)
            if inspect.isawaitable(result):
                result = await result
            if result is not False:
                return
        except (OSError, ValueError) as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConfigurationError(
                f"Chrome did not expose its debugging port {port} within {timeout_s:.0f}s; "
                "close other Chrome windows and retry, or use --mode playwright"
            ) from last_error
        await asyncio.sleep(min(0.1, remaining))


def _is_creators_url(url: str) -> bool:
    return urlparse(url).hostname == _CREATORS_HOST


def is_signed_in(cookies: Sequence[Mapping[str, Any]], page_urls: Sequence[str]) -> bool:
    """Signed in means an ``sp_dc`` cookie plus a creators page that is not the login bounce."""
    has_cookie = any(
        cookie.get("name") == KEY_COOKIE
        and isinstance(cookie.get("value"), str)
        and bool(cookie.get("value"))
        and isinstance(cookie.get("domain"), str)
        and is_spotify_domain(cookie["domain"])
        for cookie in cookies
    )
    if not has_cookie:
        return False
    return any(_is_creators_url(url) and not selectors.is_login_url(url) for url in page_urls)


def _page_urls(context: BrowserContext) -> list[str]:
    return [page.url for page in context.pages]


async def _wait_until_signed_in(
    context: BrowserContext,
    timeout_s: float,
    status: StatusCallback,
) -> None:
    status("Waiting for you to sign in to Spotify for Creators in the opened browser...")
    deadline = time.monotonic() + timeout_s
    while True:
        if is_signed_in(await context.cookies(), _page_urls(context)):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConfigurationError("login timed out")
        await asyncio.sleep(min(SIGN_IN_POLL_S, remaining))


async def _wait_for_show_id(context: BrowserContext, timeout_s: float) -> str | None:
    """A show-chooser page has no show id; return None once the short wait runs out."""
    deadline = time.monotonic() + timeout_s
    while True:
        for url in _page_urls(context):
            if not _is_creators_url(url):
                continue
            show_id = selectors.show_id_from_url(url)
            if show_id:
                return show_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(SIGN_IN_POLL_S, remaining))


async def _complete_login(
    config: UploaderConfig,
    context: BrowserContext,
    timeout_s: float,
    status: StatusCallback,
) -> LoginResult:
    await _wait_until_signed_in(context, timeout_s, status)
    show_id = await _wait_for_show_id(context, SHOW_ID_TIMEOUT_S)
    state = normalize_storage_state(await context.storage_state())
    save_storage_state(config.storage_state_path, state)
    if show_id is not None:
        save_show_id(config, show_id)
    status(f"Session saved to {config.storage_state_path}")
    return LoginResult(
        show_id=show_id,
        state_path=config.storage_state_path,
        summary=summarize(state),
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop only the Chrome process this login started; never touch other Chrome windows."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def _login_via_chrome(
    config: UploaderConfig,
    timeout_s: float,
    cdp_port: int,
    status: StatusCallback,
    chrome: Path | None = None,
) -> LoginResult:
    executable = chrome if chrome is not None else find_chrome()
    if executable is None:
        raise ConfigurationError(
            "Google Chrome was not found; install Chrome, set "
            f"{CHROME_ENV}, or pass --mode playwright"
        )
    ensure_private_dir(config.chrome_profile_dir)
    status(f"Opening Google Chrome to sign in (profile {config.chrome_profile_dir})")
    process = subprocess.Popen(
        build_chrome_args(executable, config.chrome_profile_dir, cdp_port),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await wait_for_cdp(cdp_port, CDP_READY_TIMEOUT_S)
        async with async_playwright() as playwright:
            browser: Browser = await playwright.chromium.connect_over_cdp(
                f"http://{CDP_HOST}:{cdp_port}"
            )
            if not browser.contexts:
                raise ConfigurationError(
                    "Chrome exposed no browser context; close other Chrome debug sessions and retry"
                )
            return await _complete_login(config, browser.contexts[0], timeout_s, status)
    finally:
        _terminate(process)


async def _login_via_playwright(
    config: UploaderConfig,
    timeout_s: float,
    status: StatusCallback,
) -> LoginResult:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, channel=config.channel)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(selectors.BASE_URL)
            return await _complete_login(config, context, timeout_s, status)
        finally:
            await browser.close()


async def interactive_login(
    config: UploaderConfig,
    *,
    mode: str = "auto",
    timeout_s: float = 600.0,
    cdp_port: int = 9223,
    status: StatusCallback = _ignore_status,
) -> LoginResult:
    """Open a visible browser, wait for the operator to sign in, and save the session.

    ``auto`` prefers attaching to real Chrome because Google blocks Playwright
    sign-ins; ``playwright`` exists for hosts without Chrome.
    """
    if mode not in {"auto", "chrome", "playwright"}:
        raise ConfigurationError(f"unknown login mode {mode!r}; use auto, chrome or playwright")
    if mode == "auto":
        chrome = find_chrome()
        if chrome is not None:
            return await _login_via_chrome(config, timeout_s, cdp_port, status, chrome)
        return await _login_via_playwright(config, timeout_s, status)
    if mode == "chrome":
        return await _login_via_chrome(config, timeout_s, cdp_port, status)
    return await _login_via_playwright(config, timeout_s, status)
