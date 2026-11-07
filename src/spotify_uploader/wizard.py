"""The upload-wizard driver.

Only this module knows Spotify's step order. Every URL and selector comes from
:mod:`spotify_uploader.selectors`, so a live dry-run can repair the automation by
editing a single module. Failures carry the step name, which lets the caller save
per-step artifacts and surface an actionable message to the operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from spotify_uploader import selectors
from spotify_uploader.errors import AuthExpiredError, ShowSetupError, UploadError
from spotify_uploader.models import EpisodeRequest, ProgressCallback

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.15
#: Give the in-page wait a little longer than the Python deadline; Python cancels first.
_WAIT_MARGIN_MS = 500


class _SelectorNotFound(Exception):
    """Internal sentinel: no candidate reached the required state in time."""

    def __init__(self, candidates: Sequence[str], state: str, timeout_s: float) -> None:
        super().__init__(f"none of {list(candidates)!r} became {state} within {timeout_s:g}s")


async def first_visible(page: Page, candidates: Iterable[str], timeout_s: float) -> Locator:
    """Return the first candidate that becomes visible, or raise.

    Racing the candidates means a renamed selector costs one timeout instead of
    one timeout per candidate.
    """
    return await _first_in_state(page, candidates, timeout_s, "visible")


async def first_attached(page: Page, candidates: Iterable[str], timeout_s: float) -> Locator:
    """Return the first candidate that becomes attached (hidden file inputs never show)."""
    return await _first_in_state(page, candidates, timeout_s, "attached")


async def _first_in_state(
    page: Page,
    candidates: Iterable[str],
    timeout_s: float,
    state: Literal["visible", "attached"],
) -> Locator:
    options = tuple(candidates)
    try:
        locators = [_candidate(page, option, state) for option in options]
    except PlaywrightError as exc:
        raise _SelectorNotFound(options, state, timeout_s) from exc
    if not locators:
        raise _SelectorNotFound(options, state, timeout_s)

    tasks: dict[asyncio.Task[None], Locator] = {
        asyncio.create_task(
            locator.wait_for(state=state, timeout=max(timeout_s, 0.0) * 1000 + _WAIT_MARGIN_MS)
        ): locator
        for locator in locators
    }
    pending: set[asyncio.Task[None]] = set(tasks)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_s, 0.0)
    try:
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                if task.exception() is None:
                    return tasks[task]
        raise _SelectorNotFound(options, state, timeout_s)
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _candidate(page: Page, selector: str, state: Literal["visible", "attached"]) -> Locator:
    """Resolve a selector to one element, skipping invisible duplicates for "visible".

    Spotify renders some texts twice (for example "Preview ready!" has a zero-size
    twin first in the DOM), so plain ``.first`` can point at an element that never
    becomes visible.
    """
    locator = page.locator(selector)
    return (locator.filter(visible=True) if state == "visible" else locator).first


@dataclass(frozen=True, slots=True)
class WizardResult:
    """What the wizard observed when it finished; ids are best effort."""

    episode_id: str | None
    url: str | None
    processing: bool


class EpisodeWizard:
    """Drives the show-specific upload wizard, optionally stopping before Publish."""

    def __init__(
        self,
        page: Page,
        *,
        show_id: str,
        step_timeout_s: float,
        upload_timeout_s: float,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        self._page = page
        self._show_id = show_id
        self._step_timeout_s = step_timeout_s
        self._upload_timeout_s = upload_timeout_s
        self._on_progress = on_progress
        self._last_fraction = 0.0
        self._episode_id: str | None = None

    @property
    def episode_id(self) -> str | None:
        """The draft episode's id, known as soon as the file is chosen (or on resume)."""
        return self._episode_id

    async def run(
        self,
        request: EpisodeRequest,
        *,
        publish: bool = True,
        resume_episode_id: str | None = None,
    ) -> WizardResult:
        """Walk the wizard for one video, publishing immediately unless ``publish`` is False.

        With ``resume_episode_id`` the existing draft is reopened instead of uploading
        the video again, which is how a publish that Spotify refused is retried.
        """
        self._episode_id = resume_episode_id
        self._progress(0.02, f"opening the upload wizard for show {self._show_id}")
        async with self._step("open"):
            target_url = (
                selectors.draft_wizard_url(self._show_id, resume_episode_id)
                if resume_episode_id is not None
                else selectors.wizard_url(self._show_id)
            )
            await self._page.goto(target_url, wait_until="domcontentloaded")
            self._ensure_signed_in()
        self._progress(0.06, "wizard open")
        await self._remove_consent_overlays()

        async with self._step("upload"):
            if resume_episode_id is None:
                file_input = await first_attached(
                    self._page, selectors.FILE_INPUT, self._step_timeout_s
                )
                await file_input.set_input_files(str(request.video_path))
                self._progress(0.10, f"uploading {request.video_path.name}")
            else:
                self._progress(0.10, f"resuming draft {resume_episode_id}")
            await self._wait_for_upload()

        async with self._step("title"):
            title_input = await first_visible(
                self._page, selectors.TITLE_INPUT, self._step_timeout_s
            )
            await title_input.fill(request.title)
            if await title_input.input_value() != request.title:
                raise UploadError("the title field did not accept the text", step="title")
        self._progress(0.58, "title set")

        if request.description.strip():
            async with self._step("description"):
                await self._fill_description(request.description)
            self._progress(0.68, "description set")
        else:
            self._progress(0.68, "description empty; skipped")

        async with self._step("content-check"):
            await self._set_content_check(request.explicit)
        self._progress(0.74, "content setting applied")

        if request.thumbnail_path is not None:
            async with self._step("thumbnail"):
                await self._set_thumbnail(request.thumbnail_path)
            self._progress(0.77, "thumbnail set")

        await self._remove_consent_overlays()
        async with self._step("next"):
            next_button = await first_visible(
                self._page, selectors.NEXT_BUTTON, self._step_timeout_s
            )
            await next_button.click()
            await first_attached(self._page, selectors.PUBLISH_NOW_RADIO, self._step_timeout_s)
        self._progress(0.80, "details accepted")

        async with self._step("publish-now"):
            await self._select_publish_now()
        self._progress(0.86, "publish now selected")

        if not publish:
            self._progress(1.0, "dry run complete; publish button not clicked")
            return WizardResult(episode_id=None, url=None, processing=False)

        await self._remove_consent_overlays()
        self._progress(0.90, "publishing")
        async with self._step("publish"):
            await self._click_publish()
            await self._wait_for_publish_outcome()

        episode_id, url, processing = await self._read_outcome(request.title)
        detail = "published; Spotify is still processing" if processing else "published"
        self._progress(1.0, detail)
        return WizardResult(episode_id=episode_id, url=url, processing=processing)

    @contextlib.asynccontextmanager
    async def _step(self, name: str) -> AsyncIterator[None]:
        """Turn selector misses and Playwright errors into a step-tagged UploadError."""
        try:
            yield
        except (AuthExpiredError, UploadError):
            raise
        except _SelectorNotFound as exc:
            raise UploadError(f"{name} step failed: {exc}", step=name) from exc
        except PlaywrightError as exc:
            raise UploadError(f"{name} step failed: {exc}", step=name) from exc

    def _progress(self, fraction: float, detail: str) -> None:
        value = max(self._last_fraction, min(max(fraction, 0.0), 1.0))
        self._last_fraction = value
        if self._on_progress is not None:
            self._on_progress(value, detail)

    def _ensure_signed_in(self) -> None:
        if selectors.is_login_url(self._page.url):
            raise AuthExpiredError(
                "Spotify session expired; run `spotify-uploader login` and retry"
            )

    async def _any_visible(self, candidates: Sequence[str]) -> bool:
        return await self._first_visible_now(candidates) is not None

    async def _first_visible_now(self, candidates: Sequence[str]) -> Locator | None:
        for candidate in candidates:
            locator = _candidate(self._page, candidate, "visible")
            try:
                if await locator.is_visible(timeout=0):
                    return locator
            except PlaywrightError:
                continue
        return None

    async def _first_attached_now(self, candidates: Sequence[str]) -> Locator | None:
        for candidate in candidates:
            locator = self._page.locator(candidate).first
            try:
                if await locator.count() > 0:
                    return locator
            except PlaywrightError:
                continue
        return None

    async def _remove_consent_overlays(self) -> None:
        try:
            await self._page.evaluate(_REMOVE_OVERLAYS_JS, list(selectors.CONSENT_OVERLAY_IDS))
        except PlaywrightError:
            logger.debug("could not remove cookie-consent overlays", exc_info=True)

    async def _wait_for_upload(self) -> None:
        """Wait for Spotify's own "Preview ready!" signal.

        The details form (title, description) appears seconds after the file is
        chosen, while the video is still uploading and processing, so the form being
        visible says nothing about whether the video is done.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + self._upload_timeout_s
        next_report = started
        last_percent: int | None = None
        last_wait_note = started
        while True:
            self._ensure_signed_in()
            self._capture_episode_id()
            if await self._any_visible(selectors.UPLOAD_ERROR):
                raise UploadError("Spotify reported that the upload failed", step="upload")
            if await self._any_visible(selectors.UPLOAD_READY):
                self._progress(0.50, "video ready")
                return
            now = loop.time()
            if now >= deadline:
                raise UploadError(
                    f"upload did not finish within {self._upload_timeout_s:g}s", step="upload"
                )
            if now >= next_report:
                percent = await self._upload_percent()
                if percent is not None and percent != last_percent:
                    last_percent = percent
                    self._progress(0.10 + 0.40 * percent / 100, f"uploading video ({percent}%)")
                elif percent is None and now - last_wait_note >= 5.0:
                    last_wait_note = now
                    self._progress(
                        0.50, f"waiting for Spotify to process the video ({now - started:.0f}s)"
                    )
                next_report = now + 0.5
            await asyncio.sleep(_POLL_INTERVAL_S)

    def _capture_episode_id(self) -> None:
        episode_id = selectors.episode_id_from_url(self._page.url)
        if episode_id is not None:
            self._episode_id = episode_id

    async def _upload_percent(self) -> int | None:
        try:
            body = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return None
        match = selectors.UPLOAD_PROGRESS_RE.search(body) if isinstance(body, str) else None
        return min(int(match.group(1)), 100) if match else None

    async def _fill_description(self, text: str) -> None:
        editor = await first_visible(self._page, selectors.DESCRIPTION_EDITOR, self._step_timeout_s)
        expected = text.strip()
        select_all = "Meta+A" if sys.platform == "darwin" else "Control+A"
        await self._remove_consent_overlays()
        await editor.click()
        await self._page.keyboard.press(select_all)
        await self._page.keyboard.insert_text(text)
        await self._page.wait_for_timeout(300)
        if (await editor.inner_text()).strip() == expected:
            return

        logger.debug("keyboard input was discarded; trying the Slate fiber fallback")
        if await editor.evaluate(_FIBER_INSERT_JS, text):
            await self._page.wait_for_timeout(300)
            if (await editor.inner_text()).strip() == expected:
                return

        actual = (await editor.inner_text()).strip()
        raise UploadError(
            "description did not stick in the rich-text editor "
            f"(wanted {len(expected)} chars, editor holds {len(actual)})",
            step="description",
        )

    async def _set_content_check(self, explicit: bool) -> None:
        """Make the "explicit content" switch match the request, then read it back.

        The switch defaults to off. A missing switch is fine for a clean episode, but
        never for an explicit one: publishing it unflagged would break Spotify's rules.
        """
        toggle = await self._first_attached_now(selectors.EXPLICIT_TOGGLE)
        if toggle is None:
            if explicit:
                raise UploadError(
                    "the explicit-content switch was not found, so the episode cannot "
                    "be flagged explicit",
                    step="content-check",
                )
            logger.debug("explicit-content switch not found; leaving Spotify's default (off)")
            return
        if await toggle.is_checked() != explicit:
            await self._remove_consent_overlays()
            await toggle.evaluate("el => el.click()")
            await self._page.wait_for_timeout(300)
        if await toggle.is_checked() != explicit:
            raise UploadError(
                "the explicit-content switch did not change to the requested state",
                step="content-check",
            )

    async def _set_thumbnail(self, path: Path) -> None:
        """Upload a custom thumbnail before Next, because Next is what saves the form.

        A resumed draft may already carry a custom image: Spotify then shows that
        image with a Delete button instead of the upload tile. Clearing it first is
        defensive and has not been verified against the live site, unlike the
        upload itself (observed 2026-11-07).
        """
        await first_attached(self._page, selectors.THUMBNAIL_LIST, self._step_timeout_s)
        upload_input = await self._first_attached_now(selectors.THUMBNAIL_INPUT)
        if upload_input is None:
            delete_button = await self._first_attached_now(selectors.THUMBNAIL_DELETE)
            if delete_button is None:
                raise UploadError(
                    "the thumbnail section has neither an upload tile nor a Delete button",
                    step="thumbnail",
                )
            await self._remove_consent_overlays()
            await delete_button.click(timeout=self._step_timeout_s * 1000)
            upload_input = await first_attached(
                self._page, selectors.THUMBNAIL_INPUT, self._step_timeout_s
            )
        await upload_input.set_input_files(str(path))
        await self._wait_for_thumbnail(path)

    async def _wait_for_thumbnail(self, path: Path) -> None:
        """Confirm Spotify swapped the upload tile for a decoded custom image."""
        loop = asyncio.get_running_loop()
        timeout_s = self._step_timeout_s
        deadline = loop.time() + timeout_s
        input_locator = self._page.locator(selectors.THUMBNAIL_INPUT[0])
        image_locator = self._page.locator(selectors.THUMBNAIL_SELECTED_IMAGE[0])
        seen = "the thumbnail section disappeared"
        while True:
            try:
                input_count = await input_locator.count()
                image_count = await image_locator.count()
                src = ""
                natural_width = 0
                if image_count > 0:
                    details = await image_locator.first.evaluate(
                        "img => ({src: img.getAttribute('src') || '',"
                        " width: img.naturalWidth || 0})"
                    )
                    src = str(details.get("src", ""))
                    natural_width = int(details.get("width", 0))
            except PlaywrightError as exc:
                raise UploadError(
                    f"could not read the thumbnail state: {exc}", step="thumbnail"
                ) from exc
            if input_count == 0 and src.startswith("data:image") and natural_width > 0:
                logger.debug("thumbnail %s accepted at %dpx wide", path.name, natural_width)
                return
            seen = (
                f"upload input {'still present' if input_count else 'gone'}, "
                f"checked image is "
                + (
                    f"src {src[:24]!r} with naturalWidth {natural_width}"
                    if image_count
                    else "missing"
                )
            )
            if loop.time() >= deadline:
                raise UploadError(
                    f"Spotify did not accept the thumbnail {path.name!r} within "
                    f"{timeout_s:g}s ({seen})",
                    step="thumbnail",
                )
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _select_publish_now(self) -> None:
        if await self._publish_now_checked() is True:
            return
        await self._remove_consent_overlays()
        for candidate in selectors.PUBLISH_NOW_RADIO:
            locator = self._page.locator(candidate).first
            try:
                if await locator.count() == 0 or not await locator.is_visible(timeout=0):
                    continue
                await locator.click(timeout=self._step_timeout_s * 1000)
            except PlaywrightError:
                continue
            if await self._publish_now_checked() is not False:
                return

        clicked = await self._page.evaluate(
            _CLICK_PUBLISH_NOW_JS, list(selectors.PUBLISH_NOW_RADIO)
        )
        if not clicked:
            raise UploadError("could not find the 'publish now' control", step="publish-now")
        if await self._publish_now_checked() is False:
            raise UploadError("'publish now' did not become selected", step="publish-now")

    async def _publish_now_checked(self) -> bool | None:
        value = await self._page.evaluate(
            _PUBLISH_NOW_CHECKED_JS, list(selectors.PUBLISH_NOW_RADIO)
        )
        return value if isinstance(value, bool) else None

    async def _click_publish(self) -> None:
        button = await first_visible(self._page, selectors.PUBLISH_BUTTON, self._step_timeout_s)
        await button.click()

    async def _wait_for_publish_outcome(self) -> None:
        """Success means the page left the wizard, not that an episode id exists.

        The episode id is already in the wizard URL from the moment the upload
        starts, so it proves nothing about Publish having been accepted.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._step_timeout_s * 4
        while True:
            self._ensure_signed_in()
            if not selectors.is_wizard_url(self._page.url):
                return
            if await self._any_visible(selectors.SETUP_REQUIRED):
                draft = self._episode_id or "unknown"
                raise ShowSetupError(
                    "Spotify will not publish the first episode until the show's own setup is "
                    "finished (the dashboard checklist item 'Set up your show': name, "
                    f"description, cover art). The episode was saved as draft {draft}; after "
                    f"setup, retry with --resume-draft {draft}.",
                    draft_episode_id=self._episode_id,
                )
            if await self._any_visible(selectors.PUBLISH_SUCCESS):
                return
            if await self._any_visible(selectors.UPLOAD_ERROR):
                raise UploadError("Spotify reported an error after Publish", step="publish")
            if loop.time() >= deadline:
                raise UploadError(
                    f"Spotify did not accept Publish within {self._step_timeout_s * 4:g}s; "
                    "the page is still in the wizard",
                    step="publish",
                )
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _read_outcome(self, title: str) -> tuple[str | None, str | None, bool]:
        """Read the new episode's link and processing state from the episode list."""
        self._capture_episode_id()
        url: str | None = None
        processing = False
        try:
            await self._page.goto(
                selectors.episodes_url(self._show_id), wait_until="domcontentloaded"
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._step_timeout_s
            payload = {"title": title, "episodeId": self._episode_id}
            while True:
                href = await self._page.evaluate(_FIND_EPISODE_LINK_JS, payload)
                if isinstance(href, str) and href:
                    url = urljoin(selectors.BASE_URL, href)
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            processing = await self._any_visible(selectors.PROCESSING_HINT)
        except PlaywrightError:
            logger.debug("could not read the episode list for the new episode", exc_info=True)
        if self._episode_id is None and url is not None:
            self._episode_id = selectors.episode_id_from_url(url)
        return self._episode_id, url, processing


_REMOVE_OVERLAYS_JS = """
(ids) => {
  for (const id of ids) {
    const overlay = document.getElementById(id);
    if (overlay) overlay.remove();
  }
}
"""

_FIBER_INSERT_JS = """
(el, text) => {
  const key = Object.keys(el).find(
    (name) => name.startsWith("__reactFiber$") || name.startsWith("__reactInternalInstance$")
  );
  let fiber = key ? el[key] : null;
  for (let hops = 0; fiber && hops < 100; hops += 1) {
    const props = fiber.memoizedProps;
    const editor = props && props.editor;
    if (
      editor &&
      typeof editor.start === "function" &&
      typeof editor.end === "function" &&
      typeof editor.select === "function" &&
      typeof editor.insertText === "function"
    ) {
      editor.select({ anchor: editor.start([]), focus: editor.end([]) });
      editor.insertText(text);
      return true;
    }
    fiber = fiber.return;
  }
  return false;
}
"""

_PUBLISH_NOW_CHECKED_JS = """
(candidates) => {
  for (const selector of candidates) {
    let element = null;
    try {
      element = document.querySelector(selector);
    } catch (error) {
      continue;
    }
    if (!element) continue;
    const input = element.matches("input")
      ? element
      : element.control || element.querySelector("input");
    if (input) return !!input.checked;
  }
  return null;
}
"""

_CLICK_PUBLISH_NOW_JS = """
(candidates) => {
  for (const selector of candidates) {
    let element = null;
    try {
      element = document.querySelector(selector);
    } catch (error) {
      continue;
    }
    if (!element) continue;
    const input = element.matches("input")
      ? element
      : element.control || element.querySelector("input");
    if (!input) continue;
    if (!input.checked) input.click();
    return true;
  }
  return false;
}
"""

_FIND_EPISODE_LINK_JS = """
({title, episodeId}) => {
  const anchors = [...document.querySelectorAll("a[href]")];
  if (episodeId) {
    for (const anchor of anchors) {
      const href = anchor.getAttribute("href") || "";
      if (href.includes("/episode/" + episodeId)) return href;
    }
  }
  const wanted = title.trim();
  for (const anchor of anchors) {
    if ((anchor.textContent || "").trim().includes(wanted)) return anchor.getAttribute("href");
  }
  return null;
}
"""
