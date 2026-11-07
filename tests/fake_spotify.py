"""A local stand-in for creators.spotify.com.

The live wizard is not reachable from tests, so this module serves plain
HTML/vanilla JS on the real Spotify URLs and talks to the test through
``context.route``. It mirrors what was observed on the live site on 2026-11-06:
the details form appears while the video is still uploading, "Preview ready!"
arrives later, the wizard URL gains the episode id as soon as the file is chosen,
the explicit setting is a switch, the review step offers Now/Schedule, and a
cookie overlay intercepts pointer events. When the real UI changes, correct this
fixture with the selectors.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Request, Route

from fake_dashboard import DashboardState, dashboard_page

#: The show id every test uploads to; the fake only serves this show.
SHOW_ID = "show-test123"

_WIZARD_PATH = re.compile(r"^/pod/show/(?P<show>[^/]+)/episode/(?:(?P<episode>[^/]+)/)?wizard$")
_EPISODES_PATH = re.compile(r"^/pod/show/(?P<show>[^/]+)/episodes$")
_EPISODE_PATH = re.compile(r"^/pod/show/(?P<show>[^/]+)/episode/(?P<episode>[^/]+)$")


@dataclass(slots=True)
class FakeBehavior:
    """Knobs that make the fake exercise failure paths and React quirks."""

    expired_session: bool = False
    upload_fails: bool = False
    description_mode: str = "normal"
    show_explicit_toggle: bool = True
    existing_titles: tuple[str, ...] = ()
    existing_drafts: tuple[str, ...] = ()
    processing_after_publish: bool = False
    reject_publish: bool = False
    setup_required: bool = False
    consent_overlay: bool = True
    dashboard: DashboardState | None = None
    upload_delay_ms: int = 120
    #: Leave the upload tile in place after a thumbnail file is chosen.
    thumbnail_fails: bool = False
    #: False renders no Thumbnails section at all.
    thumbnail_ui: bool = True
    #: Start with a finished custom thumbnail (the state of a resumed draft).
    thumbnail_existing: bool = False


@dataclass(slots=True)
class FakeSpotify:
    """The state a test inspects after driving the uploader."""

    behavior: FakeBehavior
    events: list[dict[str, Any]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    network_leaks: list[str] = field(default_factory=list)

    @property
    def published_titles(self) -> list[str]:
        return [str(item["title"]) for item in self.published]


async def install_fake_spotify(
    context: BrowserContext,
    show_id: str,
    behavior: FakeBehavior | None = None,
) -> FakeSpotify:
    """Route Spotify traffic to local pages; return the shared event log."""
    fake = FakeSpotify(behavior=behavior if behavior is not None else FakeBehavior())
    ALL_FAKES.append(fake)
    # Registered first, so it has the lowest priority: anything the fake does not
    # serve is blocked and recorded instead of silently reaching the real internet.
    await context.route("**/*", _block_unrouted(fake))
    await context.route("https://creators.spotify.com/**", _creators_route(fake, show_id))
    await context.route("https://accounts.spotify.com/**", _accounts_route())
    return fake


#: Every fake created in a test run; conftest asserts none of them leaked to the network.
ALL_FAKES: list[FakeSpotify] = []


def _block_unrouted(fake: FakeSpotify) -> Callable[[Route], Awaitable[None]]:
    async def handle(route: Route) -> None:
        fake.network_leaks.append(route.request.url)
        await route.abort()

    return handle


def _creators_route(fake: FakeSpotify, show_id: str) -> Callable[[Route], Awaitable[None]]:
    async def handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path.rstrip("/") or "/"
        if request.method == "POST" and path == "/__event":
            _record_event(fake, request)
            await route.fulfill(status=204)
            return
        if fake.behavior.expired_session and (
            _WIZARD_PATH.match(path) or _EPISODES_PATH.match(path)
        ):
            # A fulfilled 302 is followed by the browser straight to the real network,
            # bypassing every route, so the redirect is done in-page. The target is
            # where real Spotify sent an unauthenticated session on 2026-11-06.
            await _fulfill_html(route, _EXPIRED_REDIRECT_PAGE)
            return
        if fake.behavior.dashboard is not None:
            dashboard_html = dashboard_page(path, fake.behavior.dashboard, show_id)
            if dashboard_html is not None:
                await _fulfill_html(route, dashboard_html)
                return
        wizard = _WIZARD_PATH.match(path)
        if wizard is not None and wizard.group("show") == show_id:
            await _fulfill_html(route, _wizard_page(fake, show_id, wizard.group("episode")))
            return
        episodes = _EPISODES_PATH.match(path)
        if episodes is not None and episodes.group("show") == show_id:
            await _fulfill_html(route, _episodes_page(fake, show_id))
            return
        episode = _EPISODE_PATH.match(path)
        if episode is not None and episode.group("show") == show_id:
            await _fulfill_html(route, _episode_page(episode.group("episode")))
            return
        await _fulfill_html(route, _not_found_page(path), status=404)

    return handle


def _accounts_route() -> Callable[[Route], Awaitable[None]]:
    async def handle(route: Route) -> None:
        path = urlparse(route.request.url).path.rstrip("/")
        if path.endswith("/login"):
            await _fulfill_html(route, _LOGIN_PAGE)
            return
        await _fulfill_html(route, _not_found_page(path), status=404)

    return handle


async def _fulfill_html(route: Route, body: str, *, status: int = 200) -> None:
    await route.fulfill(status=status, content_type="text/html; charset=utf-8", body=body)


def _record_event(fake: FakeSpotify, request: Request) -> None:
    body = request.post_data
    if not body:
        return
    try:
        payload = json.loads(body)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    fake.events.append(payload)
    if payload.get("type") == "published" and isinstance(payload.get("title"), str):
        fake.published.append(payload)


def _wizard_page(fake: FakeSpotify, show_id: str, resumed_episode: str | None) -> str:
    behavior = fake.behavior
    toggle = _EXPLICIT_TOGGLE if behavior.show_explicit_toggle else ""
    config = json.dumps(
        {
            "showId": show_id,
            "uploadFails": behavior.upload_fails,
            "uploadDelayMs": behavior.upload_delay_ms,
            "descriptionMode": behavior.description_mode,
            "processing": behavior.processing_after_publish,
            "rejectPublish": behavior.reject_publish,
            "setupRequired": behavior.setup_required,
            "resumedEpisode": resumed_episode,
            "consentOverlay": behavior.consent_overlay,
            "thumbnailFails": behavior.thumbnail_fails,
        }
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Create episode</title>"
        f"<style>{_WIZARD_STYLE}</style></head><body>"
        "<h1>Create episode</h1>"
        "<div id='uploadArea'>"
        "<input id='uploadAreaInput' type='file' accept='video/mp4,video/quicktime'"
        " style='display: none'>"
        "<span id='ready-twin' style='display: none'></span>"
        "<div id='uploadStatus'></div></div>"
        "<div id='details' style='display: none'>"
        "<h2>Details</h2>"
        "<label for='title-input'>Title</label>"
        "<input id='title-input' name='title' type='text'>"
        "<label for='description-editor'>Description</label>"
        "<div id='description-editor' name='description' contenteditable='true'"
        " data-slate-editor='true' role='textbox'></div>"
        f"{toggle}"
        f"{_thumbnail_section(behavior)}"
        "<button id='next' type='submit'>Next</button>"
        "</div>"
        f"{_REVIEW_STEP}"
        f"<script>const CONFIG = {config};{_WIZARD_JS}</script>"
        "</body></html>"
    )


def _episodes_page(fake: FakeSpotify, show_id: str) -> str:
    rows = [
        f"<li><a href='/pod/show/{escape(show_id, quote=True)}/episode/ep-{index}'>"
        f"{escape(title)}</a></li>"
        for index, title in enumerate(fake.behavior.existing_titles, start=1)
    ]
    rows.extend(
        f"<li><a href='/pod/show/{escape(show_id, quote=True)}/episode/draft-old-{index}/wizard'>"
        f"{escape(title)}</a> <span>Draft</span></li>"
        for index, title in enumerate(fake.behavior.existing_drafts, start=1)
    )
    for item in fake.published:
        status = " <span>Processing</span>" if fake.behavior.processing_after_publish else ""
        rows.append(
            f"<li><a href='/pod/show/{escape(show_id, quote=True)}"
            f"/episode/{escape(str(item.get('episode_id')), quote=True)}'>"
            f"{escape(str(item['title']))}</a>{status}</li>"
        )
    listing = f"<ul id='episode-list'>{''.join(rows)}</ul>" if rows else "<p>No episodes yet.</p>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Episodes</title>"
        "</head><body><h1>Episodes</h1>"
        f"{listing}"
        "<script>fetch('/__event', {method: 'POST',"
        " headers: {'content-type': 'application/json'},"
        " body: JSON.stringify({type: 'episodes_opened'})}).catch(() => {});</script>"
        "</body></html>"
    )


def _episode_page(episode_id: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Episode</title>"
        "</head><body>"
        f"<h1>Episode {escape(episode_id)}</h1>"
        "<p>This episode is live.</p>"
        "</body></html>"
    )


def _not_found_page(path: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Not found</title>"
        f"</head><body><h1>Not found</h1><p>{escape(path)}</p></body></html>"
    )


_EXPIRED_REDIRECT_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Login</title></head><body>"
    "<script>history.replaceState({}, '', '/pod/login');</script>"
    "<h1>Log in with your Spotify account</h1></body></html>"
)

_EXPLICIT_TOGGLE = """
<div id='content-checks'>
  <span>Explicit content</span>
  <input type='checkbox' name='podcastEpisodeIsExplicit' id='explicit-toggle'
         style='position: absolute; opacity: 0; width: 1px; height: 1px;'>
  <label id='explicit-label' for='explicit-toggle'>No</label>
</div>
"""

_REVIEW_STEP = """
<div id='review' style='display: none'>
  <h2>Review and publish</h2>
  <input id='publish-date-now' type='radio' name='wizardPublishDateSelection'
         style='position: absolute; opacity: 0; width: 1px; height: 1px;'>
  <label for='publish-date-now'>Now</label>
  <input id='publish-date-schedule' type='radio' name='wizardPublishDateSelection'
         style='position: absolute; opacity: 0; width: 1px; height: 1px;'>
  <label for='publish-date-schedule'>Schedule</label>
  <button id='publish' type='submit' disabled>Publish</button>
</div>
"""

_UPLOAD_TILE = (
    "<button role='button' type='button'>"
    "<input id='uploadAreaInput' type='file'"
    " accept='image/apng,image/avif,image/png,image/jpeg' style='display: none'>"
    "<svg width='24' height='24' aria-hidden='true' focusable='false'>"
    "<rect width='24' height='24' rx='4' fill='#b3b3b3'></rect></svg>"
    "<span>Upload</span></button>"
)

#: A 1x1 PNG: enough for the resumed draft's custom radio to have a loaded image.
_THUMBNAIL_DATA_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_CUSTOM_THUMBNAIL = (
    "<div id='thumbnail-custom' data-name='resumed-thumbnail.png'>"
    "<button role='radio' type='button' aria-checked='true'>"
    f"<img src='{_THUMBNAIL_DATA_URL}' alt='Custom thumbnail'"
    " style='width: 160px; height: 90px'></button>"
    "<button type='button' aria-label='Delete'>Delete</button></div>"
)


def _thumbnail_section(behavior: FakeBehavior) -> str:
    """Render the Details step's Thumbnails group as observed on 2026-11-07.

    The tile's input deliberately repeats the video input's ``#uploadAreaInput``
    id, exactly like the real page, so an unscoped selector resolves to the wrong
    input. The episode-art input must also exist outside the list: the wizard is
    never allowed to touch it.
    """
    episode_art = (
        "<input id='episode-art-input' type='file' accept='image/png,image/jpeg'"
        " style='display: none'>"
    )
    if not behavior.thumbnail_ui:
        return episode_art
    if behavior.thumbnail_existing:
        first_child = _CUSTOM_THUMBNAIL
        checked = (False, False, False)
    else:
        first_child = _UPLOAD_TILE
        checked = (True, False, False)
    radios = "".join(
        f"<div id='thumbnail-{index}'>"
        f"<button role='radio' type='button' aria-label='Thumbnail {index + 1}'"
        f" aria-checked='{str(is_checked).lower()}'>"
        "<canvas width='160' height='90'></canvas></button></div>"
        for index, is_checked in enumerate(checked)
    )
    return (
        "<div id='thumbnails' style='display: none'>"
        "<h3>Thumbnails</h3>"
        f"<div id='thumbnail-list' role='radiogroup'>{first_child}{radios}</div>"
        f"<template id='thumbnail-upload-template'>{_UPLOAD_TILE}</template>"
        "</div>"
        f"{episode_art}"
    )


_WIZARD_STYLE = """
body { font-family: sans-serif; margin: 2rem; }
#uploadStatus { margin: 1rem 0; }
#title-input { display: block; width: 30rem; margin-bottom: 1rem; }
#description-editor {
  min-height: 80px; width: 30rem; border: 1px solid #999; padding: 0.25rem;
  margin-bottom: 1rem;
}
#content-checks { margin-bottom: 1rem; position: relative; }
#onetrust-consent-sdk {
  position: fixed; inset: 0; z-index: 99999; background: rgba(0, 0, 0, 0.01);
}
"""

_WIZARD_JS = """
const fileInput = document.getElementById("uploadAreaInput");
const statusBox = document.getElementById("uploadStatus");
const details = document.getElementById("details");
const review = document.getElementById("review");
const editor = document.getElementById("description-editor");
const nextButton = document.getElementById("next");
const explicitToggle = document.getElementById("explicit-toggle");
let filename = "";
let episodeId = "";
let revertTimer = null;

const thumbnailList = document.getElementById("thumbnail-list");
const thumbnails = document.getElementById("thumbnails");
const uploadTileTemplate = document.getElementById("thumbnail-upload-template");
let customThumbnailName = null;
const existingThumbnail = document.getElementById("thumbnail-custom");
if (existingThumbnail) {
  customThumbnailName = existingThumbnail.getAttribute("data-name") || "custom-thumbnail";
}

function revealThumbnails() {
  if (thumbnails) thumbnails.style.display = "block";
}

function bindThumbnailInput() {
  if (!thumbnailList) return;
  const input = thumbnailList.querySelector("input[type=file]");
  if (!input || input.dataset.bound === "yes") return;
  input.dataset.bound = "yes";
  input.addEventListener("change", function (event) {
    const file = event.target.files && event.target.files[0];
    if (!file || CONFIG.thumbnailFails) return;
    const reader = new FileReader();
    reader.onload = function () {
      showCustomThumbnail(file.name, String(reader.result || ""));
    };
    reader.readAsDataURL(file);
  });
}

function showCustomThumbnail(name, src) {
  if (!thumbnailList) return;
  const tile = thumbnailList.querySelector("button[role=button]");
  if (tile) tile.remove();
  const previous = document.getElementById("thumbnail-custom");
  if (previous) previous.remove();
  const wrapper = document.createElement("div");
  wrapper.id = "thumbnail-custom";
  const radio = document.createElement("button");
  radio.type = "button";
  radio.setAttribute("role", "radio");
  radio.setAttribute("aria-checked", "true");
  const image = document.createElement("img");
  image.alt = "Custom thumbnail";
  image.style.width = "160px";
  image.style.height = "90px";
  image.addEventListener("load", function () {
    report({type: "thumbnail_set", filename: name, width: image.naturalWidth});
  });
  radio.appendChild(image);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.setAttribute("aria-label", "Delete");
  remove.textContent = "Delete";
  remove.addEventListener("click", deleteCustomThumbnail);
  wrapper.appendChild(radio);
  wrapper.appendChild(remove);
  thumbnailList.insertBefore(wrapper, thumbnailList.firstChild);
  for (const other of thumbnailList.querySelectorAll("button[role=radio]")) {
    if (other !== radio) other.setAttribute("aria-checked", "false");
  }
  customThumbnailName = name;
  image.src = src;
}

function deleteCustomThumbnail() {
  if (!thumbnailList) return;
  const custom = document.getElementById("thumbnail-custom");
  if (custom) custom.remove();
  if (uploadTileTemplate) {
    thumbnailList.insertAdjacentHTML("afterbegin", uploadTileTemplate.innerHTML);
  }
  const first = thumbnailList.querySelector("button[aria-label='Thumbnail 1']");
  if (first) first.setAttribute("aria-checked", "true");
  customThumbnailName = null;
  bindThumbnailInput();
  report({type: "thumbnail_deleted"});
}

const initialDelete = document.querySelector("#thumbnail-list button[aria-label='Delete']");
if (initialDelete) initialDelete.addEventListener("click", deleteCustomThumbnail);
bindThumbnailInput();

function report(payload) {
  return fetch("/__event", {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(payload),
  }).catch(function () {});
}

function injectConsentOverlay() {
  if (!CONFIG.consentOverlay || document.getElementById("onetrust-consent-sdk")) return;
  const overlay = document.createElement("div");
  overlay.id = "onetrust-consent-sdk";
  document.body.appendChild(overlay);
}

injectConsentOverlay();
report({type: "wizard_opened"});

if (CONFIG.resumedEpisode) {
  episodeId = CONFIG.resumedEpisode;
  filename = "(resumed draft)";
  details.style.display = "block";
  revealThumbnails();
  document.getElementById("ready-twin").textContent = "Preview ready!";
  statusBox.textContent = "Preview ready!";
  report({type: "resumed", episode_id: episodeId});
}

fileInput.addEventListener("change", function () {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  filename = file.name;
  episodeId = "draft-" + Date.now();
  history.pushState(
    {}, "", "/pod/show/" + CONFIG.showId + "/episode/" + episodeId + "/wizard"
  );
  report({type: "file_set", filename: filename, episode_id: episodeId});
  statusBox.textContent = "Uploading (0%)";
  details.style.display = "block";
  setTimeout(injectConsentOverlay, 60);
  setTimeout(function () { statusBox.textContent = "Uploading (60%)"; }, CONFIG.uploadDelayMs / 2);
  setTimeout(function () {
    if (CONFIG.uploadFails) {
      statusBox.textContent = "Upload failed. Please try again.";
      return;
    }
    document.getElementById("ready-twin").textContent = "Preview ready!";
    statusBox.textContent = "Preview ready!";
    revealThumbnails();
    report({type: "preview_ready"});
  }, CONFIG.uploadDelayMs);
});

if (CONFIG.descriptionMode !== "normal") {
  editor.addEventListener("input", function () {
    clearTimeout(revertTimer);
    revertTimer = setTimeout(function () {
      editor.textContent = "";
    }, 60);
  });
}

if (CONFIG.descriptionMode === "reverts" || CONFIG.descriptionMode === "fiber_only") {
  const fiberEditor = {
    start: function () { return []; },
    end: function () { return []; },
    select: function () {},
    insertText: function (text) {
      clearTimeout(revertTimer);
      editor.textContent = text;
      report({type: "fiber_insert"});
    },
  };
  editor["__reactFiber$test"] = {
    memoizedProps: {editor: fiberEditor},
    return: null,
  };
}

if (explicitToggle) {
  explicitToggle.addEventListener("change", function () {
    document.getElementById("explicit-label").textContent = explicitToggle.checked ? "Yes" : "No";
    report({type: "explicit_selected", explicit: explicitToggle.checked});
  });
}

const publishNow = document.getElementById("publish-date-now");
const publishButton = document.getElementById("publish");
publishNow.addEventListener("change", function () {
  publishButton.disabled = !publishNow.checked;
  if (publishNow.checked) report({type: "publish_now_selected"});
});

nextButton.addEventListener("click", function () {
  report({type: "next_clicked", preview_ready: statusBox.textContent === "Preview ready!"});
  details.style.display = "none";
  review.style.display = "block";
});

publishButton.addEventListener("click", async function () {
  if (CONFIG.setupRequired) {
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.innerHTML = "<h3>Finish setting up to publish</h3>" +
      "<p>You have tasks to complete before publishing your first episode.</p>";
    document.body.appendChild(dialog);
    report({type: "setup_required"});
    return;
  }
  if (CONFIG.rejectPublish) {
    report({type: "publish_rejected"});
    return;
  }
  await report({
    type: "published",
    title: document.getElementById("title-input").value,
    description: editor.innerText,
    explicit: explicitToggle ? explicitToggle.checked : null,
    filename: filename,
    episode_id: episodeId,
    processing: CONFIG.processing,
    thumbnail: customThumbnailName,
    thumbnail_kind: customThumbnailName ? "custom" : "auto",
  });
  location.assign("/pod/show/" + CONFIG.showId + "/episodes");
});
"""

_LOGIN_PAGE = """
<!doctype html><html><head><meta charset='utf-8'><title>Log in</title></head>
<body><h1>Log in to Spotify</h1><p>Your session has expired.</p></body></html>
"""
