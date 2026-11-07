"""Every Spotify for Creators URL, selector and text pattern in one place.

The wizard is a React SPA that Spotify changes without notice, so nothing outside
this module may hard-code a selector. Each candidate tuple is tried in order and
the first visible match wins; put the most stable (id, role, visible text) first.

Verified against the live site on 2026-11-06 (headless, saved session): the
upload input, ``#title-input``, the Slate description editor, the explicit toggle,
the "Preview ready!" signal, the Next button, the Now/Schedule radios and the
Publish button. Not yet observed: what the page does after Publish is clicked, so
``PUBLISH_SUCCESS`` and ``PROCESSING_HINT`` are still guesses.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

BASE_URL = "https://creators.spotify.com"
LOGIN_HOST = "accounts.spotify.com"

#: The dashboard lives at ``/home/show/<id>``; the wizard and episode pages at ``/pod/show/<id>``.
SHOW_PATH_RE = re.compile(r"/(?:pod|home)/show/(?P<show_id>[A-Za-z0-9_-]+)")
EPISODE_PATH_RE = re.compile(
    r"/pod/show/(?P<show_id>[A-Za-z0-9_-]+)/episode/(?!wizard\b)(?P<episode_id>[A-Za-z0-9_-]+)"
)


def wizard_url(show_id: str) -> str:
    """Always show-specific: the bare wizard URL silently targets the last-used show."""
    return f"{BASE_URL}/pod/show/{show_id}/episode/wizard"


def episodes_url(show_id: str) -> str:
    return f"{BASE_URL}/pod/show/{show_id}/episodes"


def draft_wizard_url(show_id: str, episode_id: str) -> str:
    """Reopen a draft episode in the wizard (its video, title and description are kept)."""
    return f"{BASE_URL}/pod/show/{show_id}/episode/{episode_id}/wizard"


def is_valid_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def is_login_url(url: str) -> bool:
    """True when the browser was bounced to a sign-in page, i.e. the session is dead."""
    parsed = urlparse(url)
    return parsed.hostname == LOGIN_HOST or parsed.path.rstrip("/").endswith("/login")


def show_id_from_url(url: str) -> str | None:
    match = SHOW_PATH_RE.search(urlparse(url).path)
    return match.group("show_id") if match else None


def episode_id_from_url(url: str) -> str | None:
    """The episode id from ``.../episode/<id>`` or ``.../episode/<id>/wizard``.

    Spotify allocates the id the moment the file is chosen, so it is present in the
    wizard URL long before anything is published.
    """
    match = EPISODE_PATH_RE.search(urlparse(url).path)
    return match.group("episode_id") if match else None


def is_wizard_url(url: str) -> bool:
    """True while the browser is still inside the upload wizard."""
    return urlparse(url).path.rstrip("/").endswith("/wizard")


UPLOAD_PROGRESS_RE = re.compile(r"Uploading \((\d{1,3})%\)")

FILE_INPUT = ("#uploadAreaInput", "input[type=file]")
UPLOAD_READY = ("text=Preview ready!",)
TITLE_INPUT = ("#title-input", "input[name=title]", "role=textbox[name=/title/i]")
DESCRIPTION_EDITOR = (
    "div[contenteditable='true'][data-slate-editor='true']",
    "[role=textbox][contenteditable=true]",
)
#: The Details step's "Thumbnails" radio group. Its upload tile reuses the video
#: step's ``#uploadAreaInput`` id, so every thumbnail lookup is scoped to the list;
#: a bare ``#uploadAreaInput`` would target the video input instead.
THUMBNAIL_LIST = ("#thumbnail-list",)
THUMBNAIL_INPUT = ("#thumbnail-list input[type=file]",)
THUMBNAIL_DELETE = ("#thumbnail-list button[aria-label='Delete']",)
THUMBNAIL_SELECTED_IMAGE = ("#thumbnail-list [role=radio][aria-checked='true'] img",)
NEXT_BUTTON = ("role=button[name=/^next$/i]", "button:has-text('Next')")
#: The label comes first: the radio input itself is visually hidden and flaky to click.
PUBLISH_NOW_RADIO = ("label[for='publish-date-now']", "#publish-date-now")
PUBLISH_BUTTON = ("role=button[name=/^publish$/i]", "button:has-text('Publish')")
#: A switch, not a pair of yes/no labels; checked means "explicit".
EXPLICIT_TOGGLE = ("input[name=podcastEpisodeIsExplicit]",)
UPLOAD_ERROR = ("text=/upload failed|something went wrong|couldn.t upload/i",)
#: The dialog Spotify shows on Publish while the show's own setup is unfinished.
SETUP_REQUIRED = ("text=/finish setting up to publish/i",)
PUBLISH_SUCCESS = ("text=/your episode is (live|published|processing)|episode published/i",)
PROCESSING_HINT = ("text=/processing|we.ll email you/i",)

#: Cookie-consent overlays that intercept clicks; removed from the DOM before clicking.
CONSENT_OVERLAY_IDS = ("onetrust-consent-sdk", "onetrust-banner-sdk", "onetrust-pc-dark-filter")
