"""Public entry point: publish one video episode through a headless browser.

The uploader owns everything around the wizard: configuration and session checks,
SHA-256 based idempotency, the duplicate-title guard, failure artifacts and
closing the browser. It never schedules an episode: Spotify's "publish now"
choice is the only path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from spotify_uploader import selectors, shows
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
from spotify_uploader.wizard import EpisodeWizard

logger = logging.getLogger(__name__)

ContextSetup = Callable[[BrowserContext], Awaitable[None]]

#: The duplicate-title check is a safety net after a crashed run, not a page-load wait.
_DUPLICATE_GRACE_S = 2.0
_DUPLICATE_POLL_S = 0.15
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


class SpotifyCreatorsUploader:
    """Drive creators.spotify.com with a saved Playwright session; see the README."""

    def __init__(
        self,
        config: UploaderConfig,
        *,
        ledger: Ledger | None = None,
        context_setup: ContextSetup | None = None,
    ) -> None:
        self._config = config
        self._ledger = ledger if ledger is not None else Ledger(config.ledger_path)
        self._context_setup = context_setup

    async def check_session(self) -> str:
        """Open the show headlessly; return the show id, or raise AuthExpiredError."""
        show_id = self._require_show_id()
        self._require_session()
        async with self._open_page() as page:
            await self._load_episodes(page, show_id)
        return show_id

    async def inspect_show(self) -> ShowInfo:
        """Open the configured show's dashboard headlessly; return its name and setup state."""
        show_id = self._require_show_id()
        self._require_session()
        async with self._open_page() as page:
            info = await shows.fetch_show_info(page, show_id)
            _ensure_signed_in(page)
        return info

    async def list_shows(self) -> list[ShowInfo]:
        """List the shows the saved session can publish to. Needs no configured show id."""
        self._require_session()
        async with self._open_page() as page:
            listed = await shows.list_shows(page)
            _ensure_signed_in(page)
        return listed

    async def publish(
        self,
        request: EpisodeRequest,
        *,
        dry_run: bool = False,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
        resume_episode_id: str | None = None,
    ) -> PublishedEpisode:
        """Publish immediately, or rehearse every step without clicking Publish when dry.

        ``resume_episode_id`` reopens an existing draft instead of uploading the video
        again; use it to retry a publish that Spotify refused (the error names the id).
        """
        request.validate()
        if resume_episode_id is not None and not selectors.is_valid_id(resume_episode_id):
            raise ValidationError(f"invalid draft episode id {resume_episode_id!r}")
        show_id = self._require_show_id()
        session_path = self._require_session()

        sha256 = sha256_file(request.video_path)
        if not dry_run and not force:
            previous = self._ledger.get(sha256, show_id)
            if previous is not None and previous.status in (
                PublishStatus.PUBLISHED,
                PublishStatus.PROCESSING,
            ):
                return replace(previous, status=PublishStatus.ALREADY_PUBLISHED)

        debug_dir = self._config.debug_dir
        async with async_playwright() as playwright:
            browser = await self._launch(playwright)
            context: BrowserContext | None = None
            page: Page | None = None
            wizard: EpisodeWizard | None = None
            tracing = False
            try:
                context = await self._new_context(browser, session_path=session_path)
                if debug_dir is not None:
                    await context.tracing.start(screenshots=True, snapshots=True)
                    tracing = True
                if self._context_setup is not None:
                    await self._context_setup(context)
                page = await context.new_page()
                await self._load_episodes(page, show_id)

                info = await self._fetch_show_info(page, show_id)
                _notify(on_progress, 0.01, f"publishing to {_describe_show(info)}")
                if info.setup_complete is False:
                    if not dry_run:
                        raise ShowSetupError(
                            _setup_message(info, resume_episode_id),
                            draft_episode_id=resume_episode_id,
                        )
                    _notify(on_progress, 0.01, _setup_warning(info))

                if not dry_run and not force:
                    duplicate = await self._find_episode_on_list(page, request.title)
                    if duplicate is not None:
                        episode_id, url = duplicate
                        return PublishedEpisode(
                            status=PublishStatus.ALREADY_PUBLISHED,
                            title=request.title,
                            show_id=show_id,
                            sha256=sha256,
                            published_at=_utc_now(),
                            episode_id=episode_id,
                            url=url,
                        )

                wizard = EpisodeWizard(
                    page,
                    show_id=show_id,
                    step_timeout_s=self._config.step_timeout_s,
                    upload_timeout_s=self._config.upload_timeout_s,
                    on_progress=on_progress,
                )
                result = await wizard.run(
                    request, publish=not dry_run, resume_episode_id=resume_episode_id
                )

                if dry_run:
                    status = PublishStatus.DRY_RUN
                elif result.processing:
                    status = PublishStatus.PROCESSING
                else:
                    status = PublishStatus.PUBLISHED
                episode = PublishedEpisode(
                    status=status,
                    title=request.title,
                    show_id=show_id,
                    sha256=sha256,
                    published_at=_utc_now(),
                    episode_id=result.episode_id,
                    url=result.url,
                )
                if not dry_run:
                    self._ledger.record(episode)
                return episode
            except UploadError as exc:
                draft = wizard.episode_id if wizard is not None else None
                artifacts: Path | None = None
                if debug_dir is not None and context is not None:
                    try:
                        artifacts = await _capture_artifacts(context, page, debug_dir, exc.step)
                        tracing = False
                    except Exception:
                        logger.exception("could not capture debug artifacts")
                message = _with_draft_hint(str(exc), draft)
                if artifacts is None and message == str(exc):
                    raise
                raise UploadError(message, step=exc.step, artifacts=artifacts) from exc
            except ShowSetupError:
                if debug_dir is not None and context is not None:
                    try:
                        await _capture_artifacts(context, page, debug_dir, "show-setup")
                        tracing = False
                    except Exception:
                        logger.exception("could not capture debug artifacts")
                raise
            finally:
                if tracing and context is not None:
                    try:
                        await context.tracing.stop()
                    except Exception:
                        logger.debug("could not stop tracing", exc_info=True)
                await _close_quietly(page)
                await _close_quietly(context)
                await _close_quietly(browser)

    def _require_show_id(self) -> str:
        show_id = (self._config.show_id or "").strip()
        if not show_id:
            raise ConfigurationError("show id unknown: run login or pass --show-id")
        return show_id

    def _require_session(self) -> Path:
        path = self._config.storage_state_path
        if not path.is_file():
            raise ConfigurationError(
                "no saved session: run `spotify-uploader login` or `import-session`"
            )
        return path

    async def _launch(self, playwright: Playwright) -> Browser:
        return await playwright.chromium.launch(
            headless=self._config.headless,
            channel=self._config.channel,
            slow_mo=float(self._config.slow_mo_ms),
        )

    async def _new_context(
        self, browser: Browser, *, session_path: Path | None = None
    ) -> BrowserContext:
        context = await browser.new_context(
            storage_state=str(session_path or self._config.storage_state_path),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        context.set_default_timeout(self._config.step_timeout_s * 1000)
        return context

    @contextlib.asynccontextmanager
    async def _open_page(self) -> AsyncIterator[Page]:
        """Launch the browser, context and page, and close them quietly on every path."""
        async with async_playwright() as playwright:
            browser = await self._launch(playwright)
            context: BrowserContext | None = None
            page: Page | None = None
            try:
                context = await self._new_context(browser)
                if self._context_setup is not None:
                    await self._context_setup(context)
                page = await context.new_page()
                yield page
            finally:
                await _close_quietly(page)
                await _close_quietly(context)
                await _close_quietly(browser)

    async def _fetch_show_info(self, page: Page, show_id: str) -> ShowInfo:
        """Read the show's dashboard state without ever letting that read block a publish."""
        try:
            return await shows.fetch_show_info(page, show_id)
        except AuthExpiredError:
            raise
        except Exception:
            logger.debug("could not read the show's dashboard; continuing", exc_info=True)
            return ShowInfo(show_id)

    async def _load_episodes(self, page: Page, show_id: str) -> None:
        try:
            await page.goto(selectors.episodes_url(show_id), wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise UploadError(f"could not open the episodes page: {exc}", step="open") from exc
        if selectors.is_login_url(page.url):
            raise AuthExpiredError(
                "Spotify session expired; run `spotify-uploader login` and retry"
            )

    async def _find_episode_on_list(
        self, page: Page, title: str
    ) -> tuple[str | None, str | None] | None:
        """Look for an already published episode with this exact title (crash guard)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(self._config.step_timeout_s, _DUPLICATE_GRACE_S)
        while True:
            match = await page.evaluate(_FIND_TITLE_JS, title)
            if isinstance(match, dict) and match.get("found"):
                href = match.get("href")
                if isinstance(href, str) and href:
                    url = urljoin(selectors.BASE_URL, href)
                    return selectors.episode_id_from_url(url), url
                return None, None
            if selectors.is_login_url(page.url):
                raise AuthExpiredError(
                    "Spotify session expired; run `spotify-uploader login` and retry"
                )
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(_DUPLICATE_POLL_S)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_signed_in(page: Page) -> None:
    """A dashboard read that landed on the login page still means the session is dead."""
    if selectors.is_login_url(page.url):
        raise AuthExpiredError("Spotify session expired; run `spotify-uploader login` and retry")


def _notify(on_progress: ProgressCallback | None, fraction: float, detail: str) -> None:
    if on_progress is not None:
        on_progress(fraction, detail)


def _describe_show(info: ShowInfo) -> str:
    """Name a show for operator-facing messages, falling back to its id."""
    if info.name:
        return f"show '{info.name}' ({info.show_id})"
    return f"show {info.show_id}"


#: The dashboard checklist item Spotify gates the first publish on.
_SETUP_TASK = "the dashboard checklist item 'Set up your show' (name, description, cover art)"


def _setup_message(info: ShowInfo, draft: str | None) -> str:
    message = (
        f"Spotify refuses to publish to {_describe_show(info)} until the show's own setup "
        f"is finished: complete {_SETUP_TASK}."
    )
    if draft is not None:
        message += (
            f" The episode is saved as draft {draft}; after setup, retry with --resume-draft "
            f"{draft}."
        )
    return message


def _setup_warning(info: ShowInfo) -> str:
    return (
        f"warning: {_describe_show(info)} is not fully set up; Spotify will refuse a real "
        f"publish until {_SETUP_TASK} is finished"
    )


async def _capture_artifacts(
    context: BrowserContext,
    page: Page | None,
    debug_dir: Path,
    step: str | None,
) -> Path:
    """Save screenshot, HTML and trace for one failed step; never fail the caller."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = debug_dir / f"{stamp}-{_safe_component(step)}"
    target.mkdir(parents=True, exist_ok=True)

    if page is not None:
        try:
            await page.screenshot(path=str(target / "screenshot.png"), full_page=True)
        except Exception:
            logger.warning("could not capture failure screenshot", exc_info=True)
        try:
            (target / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            logger.warning("could not capture failure page HTML", exc_info=True)
    try:
        await context.tracing.stop(path=str(target / "trace.zip"))
    except Exception:
        logger.warning("could not capture failure trace", exc_info=True)
    return target


def _with_draft_hint(message: str, draft: str | None) -> str:
    """Name the saved draft in a failure so the operator can resume it, not re-upload."""
    if draft is None or draft in message:
        return message
    return f"{message} (draft episode {draft}; resume with --resume-draft {draft})"


def _safe_component(value: str | None) -> str:
    cleaned = _UNSAFE_PATH_CHARS.sub("-", value or "error").strip("-")
    return cleaned or "error"


async def _close_quietly(target: Page | BrowserContext | Browser | None) -> None:
    if target is None:
        return
    try:
        await target.close()
    except Exception:
        logger.debug("ignoring error while closing the browser", exc_info=True)


_FIND_TITLE_JS = """
(title) => {
  const wanted = title.trim();
  const rowText = (el) => {
    const row = el.closest("tr, [role=row], li") || el.parentElement;
    return (row ? row.innerText : el.innerText) || "";
  };
  for (const anchor of document.querySelectorAll("a[href]")) {
    const text = (anchor.textContent || "").trim();
    if (!text.includes(wanted)) continue;
    if (/\\bdraft\\b/i.test(rowText(anchor))) continue;
    return {found: true, href: anchor.getAttribute("href")};
  }
  return {found: false, href: null};
}
"""
