"""Fake show dashboard pages for ``shows.py`` tests.

Mirrors what was observed on the live site on 2026-11-07: the sidebar arrives a
moment after the document is ready (a slow SPA), the launch checklist gates the
first publish on the show's own setup, and a live show's home shows stats instead
of the checklist. ``fake_spotify`` calls ``dashboard_page`` for every
creators.spotify.com path when ``FakeBehavior.dashboard`` is set, before it tries
the wizard/episodes routes; returning ``None`` means "not a dashboard path".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

#: The sidebar is inserted this long after DOMContentLoaded. The page text (checklist,
#: stats) is already there by then, and the show name is NOT in an <h1>: on the live site
#: the heading is a plain element, so only the sidebar card carries the name reliably.
_SIDEBAR_DELAY_MS = 300

_DASHBOARD_PATH = re.compile(r"^/home/show(?:/(?P<show_id>[A-Za-z0-9_-]+))?$")


@dataclass(frozen=True, slots=True)
class DashboardState:
    """What the fake dashboards say.

    ``setup``: "incomplete" renders the launch checklist with the "Set up your show"
    item; "complete" renders the checklist with "Setup complete"; "live" renders the
    analytics home shown once an episode is published (no checklist).
    ``other_shows`` are ``(show_id, name)`` pairs reachable from the sidebar.
    """

    name: str = "Medical Show"
    setup: str = "complete"
    other_shows: tuple[tuple[str, str], ...] = ()
    render_other_shows: bool = True
    sidebar: bool = True


def dashboard_page(path: str, state: DashboardState, show_id: str) -> str | None:
    """HTML for a dashboard path (``/home/show/<id>`` and friends), else ``None``."""
    match = _DASHBOARD_PATH.match(path.rstrip("/") or "/")
    if match is None:
        return None
    requested = match.group("show_id")
    if requested is None or requested == show_id:
        return _render(state.name, show_id, state.setup, _visible_others(state), state.sidebar)
    names = dict(state.other_shows)
    if requested not in names:
        return None
    # Another show has no "Your shows" list of its own here; its page only has to
    # answer the fetch_show_info call that follows discovery.
    return _render(names[requested], requested, "complete", (), state.sidebar)


def _visible_others(state: DashboardState) -> tuple[tuple[str, str], ...]:
    return state.other_shows if state.render_other_shows else ()


def _render(
    name: str, show_id: str, setup: str, others: tuple[tuple[str, str], ...], sidebar: bool
) -> str:
    safe_name = escape(name)
    sidebar_html = _sidebar(name, show_id, others) if sidebar else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{safe_name} | Spotify for Creators</title>"
        f"<script>{_bare_dashboard_redirect(show_id)}</script>"
        "</head><body>"
        "<div id='sidenav-slot'></div>"
        f"<template id='sidenav-template'>{sidebar_html}</template>"
        f"<main><div class='show-heading' style='font-size: 48px'>{safe_name}</div>"
        f"{_setup_section(setup)}</main>"
        f"<script>{_DELAYED_SIDEBAR_JS}</script>"
        "</body></html>"
    )


def _bare_dashboard_redirect(show_id: str) -> str:
    # The real site redirects /home/show to the last-used show; a fulfilled 302
    # escapes Playwright's routing, so the redirect happens in-page instead.
    target = escape(show_id, quote=True)
    return (
        "if (location.pathname.replace(/\\/$/, '') === '/home/show') {"
        f"history.replaceState({{}}, '', '/home/show/{target}');"
        "}"
    )


def _sidebar(name: str, show_id: str, others: tuple[tuple[str, str], ...]) -> str:
    safe_name = escape(name, quote=True)
    safe_id = escape(show_id, quote=True)
    return (
        "<nav data-testid='sidenav'>"
        f"<div data-testid='sidenav-header-show-card' aria-label=\"{safe_name}\"></div>"
        "<dl role='list'>"
        f"<dt>Home</dt><dd><a data-testid='home-link' href='/home/show/{safe_id}'>Home</a></dd>"
        f"<dt>Analytics</dt><dd><a data-testid='analytics-link' "
        f"href='/analytics/show/{safe_id}'>Analytics</a></dd>"
        f"<dt>Episodes</dt><dd><a data-testid='episodes-link' "
        f"href='/pod/show/{safe_id}/episodes'>Episodes</a></dd>"
        f"<dt>Interact</dt><dd><a data-testid='interact-link' "
        f"href='/interactivity/show/{safe_id}'>Interact</a></dd>"
        f"<dt>Monetization</dt><dd><a data-testid='monetization-link' "
        f"href='/monetization/show/{safe_id}'>Monetization</a></dd>"
        f"<dt>Settings</dt><dd><a data-testid='settings-link' "
        f"href='/pod/show/{safe_id}/podcast/edit'>Settings</a></dd>"
        f"<dd><a data-testid='create-link' "
        f"href='/pod/show/{safe_id}/episode/wizard'>Create</a></dd>"
        "</dl>"
        "<section data-testid='your-shows'>"
        "<div role='presentation' data-testid='podcast-count-title'>Your shows</div>"
        f"<ul role='list'>{_other_show_items(others)}</ul>"
        "</section>"
        "</nav>"
    )


def _other_show_items(others: tuple[tuple[str, str], ...]) -> str:
    return "".join(
        f"<li><a href='/home/show/{escape(other_id, quote=True)}'>{escape(other_name)}</a></li>"
        for other_id, other_name in others
    )


def _setup_section(setup: str) -> str:
    if setup == "incomplete":
        return (
            "<section data-testid='launch-checklist'>"
            "<h2>Launch your show</h2><p>1/3</p><ul>"
            "<li><h3>Set up your show</h3>"
            "<p>Add a show name, description, and more.</p></li>"
            "<li><h3>Publish an episode</h3></li>"
            "</ul></section>"
        )
    if setup == "complete":
        return (
            "<section data-testid='launch-checklist'>"
            "<h2>Launch your show</h2><p>2/3</p><ul>"
            "<li><h3>Setup complete</h3><p>Email address verified</p></li>"
            "<li><h3>Publish an episode</h3>"
            "<p>Add your first audio or video episode.</p></li>"
            "</ul></section>"
        )
    if setup == "live":
        return (
            "<section data-testid='home-stats'>"
            "<p>0 all-time plays &amp; downloads</p><p>0 followers</p>"
            "<p>Day 1 of 30</p>"
            "</section>"
        )
    return "<section data-testid='home-unknown'><p>Show home</p></section>"


#: Template content is inert, so selectors only match once the sidebar is inserted.
_DELAYED_SIDEBAR_JS = """
window.addEventListener("DOMContentLoaded", function () {
  setTimeout(function () {
    var slot = document.getElementById("sidenav-slot");
    var template = document.getElementById("sidenav-template");
    if (slot && template) slot.appendChild(template.content.cloneNode(true));
  }, DELAY_MS);
});
""".replace("DELAY_MS", str(_SIDEBAR_DELAY_MS))
