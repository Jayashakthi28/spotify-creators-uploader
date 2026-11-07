"""Hermetic tests for show discovery: fake dashboard, fetch/list and the CLI.

The browser tests serve everything from ``fake_dashboard`` through the fake site's
routes; the CLI tests monkeypatch the public uploader class, so nothing here ever
opens a socket to Spotify.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from playwright.async_api import Page, Route

from fake_dashboard import DashboardState, dashboard_page
from fake_spotify import SHOW_ID, FakeBehavior
from spotify_uploader import cli, shows
from spotify_uploader.config import (
    HOME_ENV,
    SHOW_ID_ENV,
    UploaderConfig,
    read_json_object,
    save_show_id,
)
from spotify_uploader.errors import AuthExpiredError
from spotify_uploader.ledger import Ledger
from spotify_uploader.models import PublishedEpisode, PublishStatus
from spotify_uploader.shows import ShowInfo

browser = pytest.mark.usefixtures("require_chromium")


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SHOW_ID_ENV, raising=False)
    monkeypatch.delenv(HOME_ENV, raising=False)


_DEAD_SESSION_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Log in</title></head><body>"
    "<script>history.replaceState({}, '', '/pod/login');</script>"
    "<h1>Log in with your Spotify account</h1>"
    "</body></html>"
)

_UNRECOGNISED_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Nope</title></head>"
    "<body><h1>Something else entirely</h1></body></html>"
)

_HEADING_ONLY_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'></head><body>"
    "<h1>Heading Only</h1><section><p>Setup complete</p></section>"
    "</body></html>"
)


async def _override_dashboard(page: Page, body: str) -> None:
    """Serve every dashboard request from a hand-written page (newest route wins)."""

    async def handle(route: Route) -> None:
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

    await page.route(re.compile(r"https://creators\.spotify\.com/home/show.*"), handle)


def _episode(show_id: str, sha256: str = "a" * 64) -> PublishedEpisode:
    return PublishedEpisode(
        status=PublishStatus.PUBLISHED,
        title="Episode",
        show_id=show_id,
        sha256=sha256,
        published_at="2026-01-01T00:00:00+00:00",
        episode_id="episode1",
        url=f"https://creators.spotify.com/pod/show/{show_id}/episode/episode1",
    )


def _patch_uploader(
    monkeypatch: pytest.MonkeyPatch, listed: list[ShowInfo]
) -> list[UploaderConfig]:
    configs: list[UploaderConfig] = []

    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            configs.append(config)

        async def list_shows(self) -> list[ShowInfo]:
            return list(listed)

    monkeypatch.setattr(shows, "SpotifyCreatorsUploader", FakeUploader)
    return configs


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _payload(out: str) -> dict[str, Any]:
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one JSON line, got {lines!r}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _entries(out: str) -> list[dict[str, Any]]:
    entries = _payload(out)["shows"]
    assert isinstance(entries, list)
    return entries


# --------------------------------------------------------------------------- fake


def test_dashboard_page_only_serves_home_show_paths() -> None:
    state = DashboardState()
    assert dashboard_page("/home/show", state, SHOW_ID) is not None
    assert dashboard_page(f"/home/show/{SHOW_ID}", state, SHOW_ID) is not None
    assert dashboard_page("/home/shows", state, SHOW_ID) is None
    assert dashboard_page(f"/pod/show/{SHOW_ID}/episodes", state, SHOW_ID) is None
    assert dashboard_page(f"/home/show/{SHOW_ID}/extra", state, SHOW_ID) is None


# -------------------------------------------------------------------- fetch_show_info


@browser
@pytest.mark.parametrize(("setup", "expected"), [("incomplete", False), ("live", True)])
async def test_fetch_show_info_reads_the_dashboard_copy(
    page_factory: Any, setup: str, expected: bool
) -> None:
    page, _fake = await page_factory(
        FakeBehavior(dashboard=DashboardState(name="Medical Show", setup=setup))
    )

    assert await shows.fetch_show_info(page, SHOW_ID) == ShowInfo(SHOW_ID, "Medical Show", expected)


@browser
async def test_fetch_show_info_reads_a_complete_live_dashboard(page_factory: Any) -> None:
    page, _fake = await page_factory(
        FakeBehavior(dashboard=DashboardState(name="Medical Show", setup="complete"))
    )

    assert await shows.fetch_show_info(page, SHOW_ID) == ShowInfo(SHOW_ID, "Medical Show", True)


@browser
async def test_fetch_show_info_leaves_an_unknown_setup_unknown(page_factory: Any) -> None:
    page, _fake = await page_factory(FakeBehavior(dashboard=DashboardState(setup="mystery")))

    info = await shows.fetch_show_info(page, SHOW_ID)

    assert info.name == "Medical Show"
    assert info.setup_complete is None


@browser
async def test_fetch_show_info_serves_other_show_dashboards(page_factory: Any) -> None:
    state = DashboardState(other_shows=(("othershow1", "Other Show"),))
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    assert await shows.fetch_show_info(page, "othershow1") == ShowInfo(
        "othershow1", "Other Show", True
    )


@browser
async def test_fetch_show_info_falls_back_to_the_heading(page_factory: Any) -> None:
    page, _fake = await page_factory()
    await _override_dashboard(page, _HEADING_ONLY_PAGE)

    assert await shows.fetch_show_info(page, SHOW_ID) == ShowInfo(SHOW_ID, "Heading Only", True)


@browser
async def test_fetch_show_info_never_raises_for_an_unrecognised_page(
    page_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shows, "_DASHBOARD_WAIT_MS", 300.0)
    page, _fake = await page_factory()
    await _override_dashboard(page, _UNRECOGNISED_PAGE)

    assert await shows.fetch_show_info(page, SHOW_ID) == ShowInfo(SHOW_ID)


@browser
async def test_fetch_show_info_returns_unknown_fast_for_an_http_error(page_factory: Any) -> None:
    # No dashboard behaviour: the fake serves its 404 for the dashboard path.
    page, _fake = await page_factory()
    started = time.monotonic()

    info = await shows.fetch_show_info(page, SHOW_ID)

    assert info == ShowInfo(SHOW_ID)
    assert time.monotonic() - started < 10


@browser
async def test_fetch_show_info_raises_when_the_session_is_dead(page_factory: Any) -> None:
    page, _fake = await page_factory()
    await _override_dashboard(page, _DEAD_SESSION_PAGE)

    with pytest.raises(AuthExpiredError, match="login"):
        await shows.fetch_show_info(page, SHOW_ID)


# ------------------------------------------------------------------------ list_shows


@browser
async def test_the_fake_bare_dashboard_redirects_to_the_last_used_show(page_factory: Any) -> None:
    page, _fake = await page_factory(FakeBehavior(dashboard=DashboardState()))

    await page.goto(shows.DASHBOARD_URL, wait_until="domcontentloaded")

    assert page.url == f"{shows.DASHBOARD_URL}/{SHOW_ID}"


@browser
async def test_list_shows_returns_the_current_show_when_nothing_else_is_listed(
    page_factory: Any,
) -> None:
    state = DashboardState(name="Medical Show", setup="complete", render_other_shows=False)
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    assert await shows.list_shows(page) == [ShowInfo(SHOW_ID, "Medical Show", True)]


@browser
async def test_list_shows_returns_nothing_fast_when_the_dashboard_errors(page_factory: Any) -> None:
    # No dashboard behaviour: the fake serves its 404 for the bare dashboard URL.
    page, _fake = await page_factory()
    started = time.monotonic()

    assert await shows.list_shows(page) == []
    assert time.monotonic() - started < 10


@browser
async def test_list_shows_puts_the_current_show_first_and_fills_other_names(
    page_factory: Any,
) -> None:
    state = DashboardState(
        name="Medical Show",
        setup="live",
        other_shows=(("othershow1", "Other Show"), ("third-show", "Third Show")),
    )
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    assert await shows.list_shows(page) == [
        ShowInfo(SHOW_ID, "Medical Show", True),
        ShowInfo("othershow1", "Other Show", True),
        ShowInfo("third-show", "Third Show", True),
    ]


@browser
async def test_list_shows_deduplicates_ids_from_many_links(page_factory: Any) -> None:
    state = DashboardState(other_shows=(("othershow1", "Other Show"), ("othershow1", "Other Show")))
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    result = await shows.list_shows(page)

    assert [info.show_id for info in result] == [SHOW_ID, "othershow1"]


@browser
async def test_list_shows_caps_discovery(
    page_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shows, "MAX_SHOWS", 3)
    state = DashboardState(
        other_shows=tuple((f"othershow{index}", f"Other {index}") for index in range(1, 6))
    )
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    result = await shows.list_shows(page)

    assert [info.show_id for info in result] == [SHOW_ID, "othershow1", "othershow2"]


@browser
async def test_list_shows_ignores_an_unrendered_your_shows_list(page_factory: Any) -> None:
    state = DashboardState(other_shows=(("othershow1", "Other Show"),), render_other_shows=False)
    page, _fake = await page_factory(FakeBehavior(dashboard=state))

    assert await shows.list_shows(page) == [ShowInfo(SHOW_ID, "Medical Show", True)]


@browser
async def test_list_shows_raises_when_the_session_is_dead(page_factory: Any) -> None:
    page, _fake = await page_factory()
    await _override_dashboard(page, _DEAD_SESSION_PAGE)

    with pytest.raises(AuthExpiredError, match="login"):
        await shows.list_shows(page)


# ------------------------------------------------------------------------------- CLI


def test_shows_prints_one_json_object_and_passes_options(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = [ShowInfo("show-a", "Alpha", True), ShowInfo("show-b", "Beta", None)]
    configs = _patch_uploader(monkeypatch, listed)
    home = tmp_path / "home"

    code, out, err = _run(capsys, ["--home", str(home), "shows", "--headed", "--channel", "chrome"])

    assert code == 0
    assert err == ""
    assert _payload(out) == {
        "shows": [
            {
                "show_id": "show-a",
                "name": "Alpha",
                "setup_complete": True,
                "default": False,
                "source": "spotify",
            },
            {
                "show_id": "show-b",
                "name": "Beta",
                "setup_complete": None,
                "default": False,
                "source": "spotify",
            },
        ],
        "note": None,
    }
    assert configs[0].home == home
    assert configs[0].headless is False
    assert configs[0].channel == "chrome"


def test_shows_explains_how_to_add_an_undiscovered_show(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", True)])

    code, out, _ = _run(capsys, ["--home", str(tmp_path / "home"), "shows"])

    assert code == 0
    note = _payload(out)["note"]
    assert isinstance(note, str)
    assert "did not list other shows" in note
    assert "creators.spotify.com/home/show/<ID>" in note
    assert "--show-id" in note


def test_shows_with_nothing_discovered_reports_an_empty_list(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_uploader(monkeypatch, [])

    code, out, _ = _run(capsys, ["--home", str(tmp_path / "home"), "shows"])

    assert code == 0
    payload = _payload(out)
    assert payload["shows"] == []
    assert isinstance(payload["note"], str)


def test_shows_merges_the_configured_default_and_ledger_ids(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config = UploaderConfig(home=home)
    save_show_id(config, "local-default")
    Ledger(config.ledger_path).record(_episode("ledger-show", "b" * 64))
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", True)])

    code, out, _ = _run(capsys, ["--home", str(home), "shows"])

    assert code == 0
    entries = _entries(out)
    assert [entry["show_id"] for entry in entries] == ["show-a", "local-default", "ledger-show"]
    assert entries[1] == {
        "show_id": "local-default",
        "name": None,
        "setup_complete": None,
        "default": True,
        "source": "local",
    }
    assert entries[2]["source"] == "local"
    assert entries[2]["default"] is False


def test_shows_marks_a_discovered_configured_default_without_duplicating_it(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    save_show_id(UploaderConfig(home=home), "show-a")
    _patch_uploader(
        monkeypatch, [ShowInfo("show-a", "Alpha", True), ShowInfo("show-b", "Beta", True)]
    )

    code, out, _ = _run(capsys, ["--home", str(home), "shows"])

    assert code == 0
    entries = _entries(out)
    assert [entry["show_id"] for entry in entries] == ["show-a", "show-b"]
    assert entries[0]["default"] is True
    assert entries[0]["source"] == "spotify"


def test_shows_select_by_id_is_case_insensitive_and_saves_the_default(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_uploader(
        monkeypatch, [ShowInfo("show-a", "Alpha", True), ShowInfo("show-b", "Beta", True)]
    )

    code, out, err = _run(capsys, ["--home", str(home), "shows", "--select", "SHOW-B"])

    assert code == 0
    assert err == ""
    payload = _payload(out)
    assert payload["selected"] == {
        "show_id": "show-b",
        "name": "Beta",
        "setup_complete": True,
        "default": True,
        "source": "spotify",
    }
    assert [entry["default"] for entry in payload["shows"]] == [False, True]
    assert read_json_object(home / "config.json")["show_id"] == "show-b"


@pytest.mark.parametrize("query", ["beta", "Bet", "et"])
def test_shows_select_by_name_prefix_or_substring(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    _patch_uploader(
        monkeypatch, [ShowInfo("show-a", "Alpha", True), ShowInfo("show-b", "Beta", True)]
    )

    code, out, _ = _run(capsys, ["--home", str(tmp_path / "home"), "shows", "--select", query])

    assert code == 0
    assert _payload(out)["selected"]["show_id"] == "show-b"


def test_shows_select_rejects_an_ambiguous_name(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_uploader(
        monkeypatch, [ShowInfo("show-a", "Alpha", True), ShowInfo("show-b", "Alpine", True)]
    )

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "shows", "--select", "al"])

    assert code == 2
    assert out == ""
    payload = json.loads(err.strip())
    assert payload["error"] == "ConfigurationError"
    assert "ambiguous" in payload["message"]
    assert "show-a" in payload["message"]
    assert "show-b" in payload["message"]


def test_shows_select_reports_when_nothing_matches(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", True)])

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "shows", "--select", "nope"])

    assert code == 2
    assert out == ""
    payload = json.loads(err.strip())
    assert payload["error"] == "ConfigurationError"
    assert "matches no show" in payload["message"]
    assert "Alpha (show-a)" in payload["message"]


def test_shows_select_warns_about_an_unfinished_show(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", False)])

    code, out, _ = _run(capsys, ["--home", str(home), "shows", "--select", "alpha"])

    assert code == 0
    payload = _payload(out)
    selected = payload["selected"]
    assert selected["setup_complete"] is False
    assert "not fully set up" in selected["warning"]
    assert "warning" not in payload["shows"][0]
    assert read_json_object(home / "config.json")["show_id"] == "show-a"


def test_shows_select_can_pick_a_locally_known_show(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config = UploaderConfig(home=home)
    save_show_id(config, "local-default")
    Ledger(config.ledger_path).record(_episode("ledger-show", "b" * 64))
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", True)])

    code, out, _ = _run(capsys, ["--home", str(home), "shows", "--select", "ledger-show"])

    assert code == 0
    selected = _payload(out)["selected"]
    assert selected["show_id"] == "ledger-show"
    assert selected["source"] == "local"
    assert selected["setup_complete"] is None
    assert "warning" not in selected
    assert read_json_object(home / "config.json")["show_id"] == "ledger-show"


def test_shows_never_prints_session_values(
    uploader_home: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_uploader(monkeypatch, [ShowInfo("show-a", "Alpha", True)])

    code, out, err = _run(capsys, ["--home", str(uploader_home), "shows"])

    assert code == 0
    assert "fake-session-value" not in out + err


def test_shows_auth_expiry_returns_three(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExpiredUploader:
        def __init__(self, config: UploaderConfig) -> None:
            del config

        async def list_shows(self) -> list[ShowInfo]:
            raise AuthExpiredError("Spotify session expired")

    monkeypatch.setattr(shows, "SpotifyCreatorsUploader", ExpiredUploader)

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "shows"])

    assert code == 3
    assert out == ""
    assert json.loads(err.strip())["error"] == "AuthExpiredError"


async def test_name_is_read_even_when_the_sidebar_renders_after_the_page_copy(
    page_factory: Any,
) -> None:
    """Live regression: the setup text is present before the sidebar card with the name."""
    page, _ = await page_factory(
        FakeBehavior(dashboard=DashboardState(name="Medical Show", setup="complete"))
    )
    info = await shows.fetch_show_info(page, SHOW_ID)

    assert info.name == "Medical Show"
    assert info.setup_complete is True


async def test_name_comes_from_the_big_title_when_the_sidebar_never_renders(
    page_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live: Spotify's sidebar sometimes crashes at startup; the title still has the name."""
    monkeypatch.setattr(shows, "_DASHBOARD_WAIT_MS", 1500.0)
    page, _ = await page_factory(
        FakeBehavior(dashboard=DashboardState(name="Medical Show", sidebar=False))
    )
    started = time.monotonic()
    info = await shows.fetch_show_info(page, SHOW_ID)

    assert info.name == "Medical Show"
    assert info.setup_complete is True
    assert time.monotonic() - started < 10
