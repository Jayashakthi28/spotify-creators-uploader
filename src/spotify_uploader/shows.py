"""Show discovery: which shows an account owns and whether one is ready to publish.

The dashboard is a different set of pages from the upload wizard, so its selectors
live here instead of ``selectors.py``. Everything read from it is advisory except a
dead session: an unreadable dashboard yields ``ShowInfo`` with ``None`` fields and
never blocks a publish.

Observed on the live site on 2026-11-07 (English UI): there is no all-shows page.
``/home/show`` redirects to the last-used show's dashboard and ``/home/shows`` is a
404. The only list of other shows is a best-effort "Your shows" sidebar section that
headless loads usually leave empty; the reliable way to add a show is its dashboard
URL. Visiting a dashboard updates Spotify's server-side "last-used show", so
``list_shows`` changes what a later bare ``/home/show`` opens.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from spotify_uploader import selectors
from spotify_uploader.config import UploaderConfig, save_show_id
from spotify_uploader.errors import AuthExpiredError, ConfigurationError, UploadError
from spotify_uploader.ledger import Ledger

#: The bare dashboard redirects to the last-used show; a show's own page appends its id.
DASHBOARD_URL = f"{selectors.BASE_URL}/home/show"
#: Discovery is best effort; never open an unbounded number of dashboards.
MAX_SHOWS = 25
#: Long enough for a busy SPA, short enough that an unreadable page cannot hang a
#: publish for minutes. Tests shorten it to keep timeout paths fast.
_DASHBOARD_WAIT_MS = 60_000.0

_NO_OTHER_SHOWS_NOTE = (
    "Spotify's dashboard did not list other shows; copy a show id from its dashboard URL "
    "creators.spotify.com/home/show/<ID> and pass --show-id"
)

_AUTH_EXPIRED = "Spotify session expired; run `spotify-uploader login` and retry"

_SIDE_NAV = 'nav[data-testid="sidenav"]'
_SHOW_CARD = '[data-testid="sidenav-header-show-card"]'
#: Extra time for the sidebar card after the page already looks like a dashboard.
_CARD_WAIT_MS = 20_000.0

#: Every path that carries a show id, in the sidebar or in a "Your shows" list.
_SHOW_LINK_RE = re.compile(
    r"/(?:home|pod|analytics|interactivity)/show/(?P<show_id>[A-Za-z0-9_-]+)"
)

#: The SPA renders the sidebar only after the document is ready, so wait for the
#: card (or any recognisable dashboard copy) instead of sleeping a fixed time.
_DASHBOARD_READY_JS = """
() => {
  if (location.hostname === 'accounts.spotify.com') return true;
  const path = location.pathname;
  if (path.endsWith('/login') || path.endsWith('/login/')) return true;
  if (document.querySelector('[data-testid="sidenav-header-show-card"]')) return true;
  const text = document.body ? document.body.innerText : '';
  return /Launch your show|Set up your show|Setup complete|all-time plays/.test(text);
}
"""

_READ_DASHBOARD_JS = """
() => {
  const card = document.querySelector('[data-testid="sidenav-header-show-card"]');
  const heading = document.querySelector('h1');
  // Spotify's sidebar sometimes never starts (the page logs "reading 'TenantFeatures'"),
  // but the dashboard always opens with the show name as its first large title.
  let title = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length !== 0) continue;
    if (el.closest('nav, [role=dialog], #onetrust-consent-sdk')) continue;
    const text = (el.textContent || '').trim();
    if (!text || text.length > 100) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    if (parseFloat(getComputedStyle(el).fontSize) >= 28) { title = text; break; }
  }
  return {
    name: card ? card.getAttribute('aria-label') : null,
    heading: heading ? heading.innerText : null,
    title: title,
    text: document.body ? document.body.innerText : '',
  };
}
"""

_HOME_LINK_JS = """
() => {
  const link = document.querySelector('a[data-testid="home-link"]');
  return link ? link.getAttribute('href') : null;
}
"""

_LINK_HREFS_JS = """
() => Array.from(document.querySelectorAll('a[href]'), (a) => a.getAttribute('href'))
"""


@dataclass(frozen=True, slots=True)
class ShowInfo:
    """What the dashboard says about one show.

    ``None`` means "could not tell", never "no": callers must not block a publish
    on an unknown value, only on an explicit ``setup_complete is False``.
    """

    show_id: str
    name: str | None = None
    setup_complete: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {"show_id": self.show_id, "name": self.name, "setup_complete": self.setup_complete}


# ``uploader`` imports ``ShowInfo`` at import time and the package imports ``shows``
# first, so this import has to follow the class or the cycle deadlocks on import.
from spotify_uploader.uploader import SpotifyCreatorsUploader  # noqa: E402


async def fetch_show_info(page: Page, show_id: str) -> ShowInfo:
    """Open the show's dashboard and read its name and setup state.

    The page is a slow SPA: waits are bounded and an unrecognised page becomes
    ``ShowInfo(show_id)`` (unknown) rather than an exception, because the caller
    treats unknown as non-blocking. A dead session is the one thing that raises.
    When the caller was on an episodes listing, it is put back there afterwards so
    the uploader's duplicate-title guard still sees that listing. Visiting the
    dashboard also updates Spotify's server-side last-used show.
    """
    previous_url = page.url if _is_episodes_url(page.url) else None
    try:
        response = await page.goto(f"{DASHBOARD_URL}/{show_id}", wait_until="domcontentloaded")
    except PlaywrightError:
        _raise_if_login(page)
        return await _leave(page, previous_url, ShowInfo(show_id))
    _raise_if_login(page)
    if response is not None and response.status >= 400:
        # An HTTP error page is never a dashboard; do not sit out the SPA timeout.
        return await _leave(page, previous_url, ShowInfo(show_id))
    ready = await _wait_for_dashboard(page)
    _raise_if_login(page)
    if not ready:
        return await _leave(page, previous_url, ShowInfo(show_id))
    name, text = await _read_dashboard(page)
    if name is None:
        # The checklist/stats copy arrives before the sidebar card that carries the
        # name, so "the page looks like a dashboard" does not mean the name is there yet.
        await _wait_for_show_card(page)
        name, text = await _read_dashboard(page)
    return await _leave(page, previous_url, ShowInfo(show_id, name, _setup_state(text)))


async def list_shows(page: Page) -> list[ShowInfo]:
    """List every show the signed-in account can publish to, current show first.

    There is no all-shows page and Spotify's "Your shows" sidebar is usually empty
    headless, so anything beyond the current show is best effort. Visiting each
    dashboard updates Spotify's server-side last-used show, which is why the bare
    ``/home/show`` URL used here opens whichever show was inspected last.
    """
    try:
        response = await page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        raise UploadError(f"could not open the Spotify dashboard: {exc}") from exc
    _raise_if_login(page)
    if response is not None and response.status >= 400:
        # Let the caller merge locally known shows instead of hanging on a broken page.
        return []
    ready = await _wait_for_dashboard(page)
    _raise_if_login(page)
    if ready:
        await _wait_for_sidebar(page)
    linked = await _linked_show_ids(page)
    current = selectors.show_id_from_url(page.url) or await _home_link_id(page)
    if current is None and linked:
        current = linked[0]
    if current is None:
        return []
    ordered = _dedupe([current, *(show_id for show_id in linked if show_id != current)])
    return [await fetch_show_info(page, show_id) for show_id in ordered[:MAX_SHOWS]]


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("shows", help="list the shows on the account")
    parser.add_argument(
        "--select",
        metavar="ID_OR_NAME",
        default=None,
        help="make this show the default (matched by id or case-insensitive name)",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--channel", default=None)


def handle(args: argparse.Namespace) -> int:
    """Print the account's shows as JSON, merging ids already known locally.

    Discovery needs the network; the configured default show id and the ledger
    still contribute shows when Spotify's dashboard refuses to list them, so a
    known show is never hidden just because the sidebar did not render.
    """
    config = UploaderConfig.load(
        home=getattr(args, "home", None),
        headless=not bool(getattr(args, "headed", False)),
        channel=getattr(args, "channel", None) or None,
    )
    discovered = asyncio.run(SpotifyCreatorsUploader(config).list_shows())
    entries, note = _merge_shows(config, discovered)
    payload: dict[str, object] = {"shows": entries, "note": note}
    query = getattr(args, "select", None)
    if query is not None:
        chosen = _select_show(entries, str(query))
        save_show_id(config, str(chosen["show_id"]))
        for entry in entries:
            entry["default"] = entry["show_id"] == chosen["show_id"]
        selected = dict(chosen)
        warning = _setup_warning(selected)
        if warning is not None:
            selected["warning"] = warning
        payload["selected"] = selected
    _write_json(payload)
    return 0


async def _wait_for_dashboard(page: Page) -> bool:
    """Wait until the page is recognisably a dashboard; never raise on a timeout."""
    try:
        await page.wait_for_function(_DASHBOARD_READY_JS, timeout=_DASHBOARD_WAIT_MS)
    except PlaywrightError:
        return False
    return True


async def _wait_for_show_card(page: Page) -> None:
    """Give the sidebar card a bounded extra wait; a missing card just leaves the name unknown."""
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_selector(
            _SHOW_CARD, state="attached", timeout=min(_DASHBOARD_WAIT_MS, _CARD_WAIT_MS)
        )


async def _wait_for_sidebar(page: Page) -> None:
    """Wait for the SPA's sidebar so best-effort "Your shows" links are not missed."""
    with contextlib.suppress(PlaywrightError):
        await page.wait_for_selector(_SIDE_NAV, state="attached", timeout=_DASHBOARD_WAIT_MS)


async def _read_dashboard(page: Page) -> tuple[str | None, str]:
    """Read the show name (sidebar card, else h1, else first big title) and the body text."""
    try:
        raw = await page.evaluate(_READ_DASHBOARD_JS)
    except PlaywrightError:
        return None, ""
    if not isinstance(raw, dict):
        return None, ""
    name = (
        _clean_text(raw.get("name"))
        or _clean_text(raw.get("heading"))
        or _clean_text(raw.get("title"))
    )
    text = raw.get("text")
    return name, text if isinstance(text, str) else ""


async def _home_link_id(page: Page) -> str | None:
    """Read the current show id from the sidebar when the URL still has no id."""
    try:
        href = await page.evaluate(_HOME_LINK_JS)
    except PlaywrightError:
        return None
    return _show_id_from_href(href)


async def _linked_show_ids(page: Page) -> list[str]:
    """Collect distinct show ids in DOM order from every suitable link on the page."""
    try:
        hrefs = await page.evaluate(_LINK_HREFS_JS)
    except PlaywrightError:
        return []
    if not isinstance(hrefs, list):
        return []
    ids = (_show_id_from_href(href) for href in hrefs)
    return _dedupe([show_id for show_id in ids if show_id is not None])


def _show_id_from_href(href: object) -> str | None:
    if not isinstance(href, str):
        return None
    match = _SHOW_LINK_RE.search(urlparse(href).path)
    return match.group("show_id") if match else None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _setup_state(text: str) -> bool | None:
    """Turn dashboard copy into the tri-state setup flag the rest of the package uses."""
    if "Set up your show" in text and "Setup complete" not in text:
        return False
    if "Setup complete" in text or "all-time plays" in text:
        return True
    return None


def _is_episodes_url(url: str) -> bool:
    """True when the browser is parked on a show's episode listing."""
    return urlparse(url).path.rstrip("/").endswith("/episodes")


async def _leave(page: Page, previous_url: str | None, info: ShowInfo) -> ShowInfo:
    """Restore the caller's page before returning a dashboard read.

    The uploader checks the episode listing for a duplicate title right after
    preflight, so leaving the dashboard loaded would make that guard miss an
    existing episode. Navigation is best effort: a failure only loses the restore.
    """
    if previous_url is not None and page.url != previous_url:
        with contextlib.suppress(PlaywrightError):
            await page.goto(previous_url, wait_until="domcontentloaded")
    return info


def _raise_if_login(page: Page) -> None:
    """A dashboard read that landed on the login page means the session is dead."""
    if selectors.is_login_url(page.url):
        raise AuthExpiredError(_AUTH_EXPIRED)


def _merge_shows(
    config: UploaderConfig, discovered: list[ShowInfo]
) -> tuple[list[dict[str, object]], str | None]:
    """Discovered shows first, then ids already known locally, each listed once."""
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for info in discovered:
        if not info.show_id or info.show_id in seen:
            continue
        seen.add(info.show_id)
        entries.append(_entry(info.show_id, info.name, info.setup_complete, source="spotify"))
    for show_id in _local_show_ids(config):
        if show_id in seen:
            continue
        seen.add(show_id)
        entries.append(_entry(show_id, None, None, source="local"))
    for entry in entries:
        entry["default"] = entry["show_id"] == config.show_id
    note = _NO_OTHER_SHOWS_NOTE if len(discovered) <= 1 else None
    return entries, note


def _local_show_ids(config: UploaderConfig) -> list[str]:
    """Ids known without a browser: the configured default and anything ever published."""
    ids: list[str] = []
    if config.show_id:
        ids.append(config.show_id)
    for episode in Ledger(config.ledger_path).all():
        if episode.show_id and episode.show_id not in ids:
            ids.append(episode.show_id)
    return ids


def _entry(
    show_id: str, name: str | None, setup_complete: bool | None, *, source: str
) -> dict[str, object]:
    return {
        "show_id": show_id,
        "name": name,
        "setup_complete": setup_complete,
        "default": False,
        "source": source,
    }


def _select_show(entries: list[dict[str, object]], query: str) -> dict[str, object]:
    """Resolve ``--select`` by id, exact name, then unique name prefix/substring."""
    wanted = query.strip()
    if not wanted:
        raise ConfigurationError("--select needs a show id or name")
    lowered = wanted.lower()
    tiers: tuple[Callable[[dict[str, object]], bool], ...] = (
        lambda entry: _text(entry, "show_id").lower() == lowered,
        lambda entry: _text(entry, "name").lower() == lowered,
        lambda entry: _text(entry, "name").lower().startswith(lowered),
        lambda entry: lowered in _text(entry, "name").lower(),
    )
    for tier in tiers:
        matches = [entry for entry in entries if tier(entry)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ConfigurationError(
                f"--select {wanted!r} is ambiguous; candidates: {_describe_entries(matches)}"
            )
    known = _describe_entries(entries) if entries else "none"
    raise ConfigurationError(f"--select {wanted!r} matches no show; known shows: {known}")


def _text(entry: dict[str, object], key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _describe_entries(entries: Iterable[dict[str, object]]) -> str:
    return ", ".join(
        f"{_text(entry, 'name') or '?'} ({_text(entry, 'show_id')})" for entry in entries
    )


def _setup_warning(entry: dict[str, object]) -> str | None:
    if entry.get("setup_complete") is not False:
        return None
    name = _text(entry, "name") or _text(entry, "show_id")
    return (
        f"show '{name}' is not fully set up; Spotify refuses a real publish until the "
        "dashboard checklist item 'Set up your show' is finished"
    )


def _write_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), file=sys.stdout)
