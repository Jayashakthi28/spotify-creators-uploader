"""URL parsing and builder tests; the wizard URL must never look like an episode."""

from __future__ import annotations

import pytest

from spotify_uploader import selectors


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.spotify.com/en/login",
        "https://accounts.spotify.com/login?continue=x",
        "https://creators.spotify.com/login",
        "https://creators.spotify.com/login/",
    ],
)
def test_is_login_url_true(url: str) -> None:
    assert selectors.is_login_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://creators.spotify.com/pod/show/show1/episodes",
        "https://creators.spotify.com/pod/show/show1/episode/episode1",
        "https://example.com/anything",
        "about:blank",
    ],
)
def test_is_login_url_false(url: str) -> None:
    assert selectors.is_login_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://creators.spotify.com/pod/show/show1",
        "https://creators.spotify.com/pod/show/show1/",
        "https://creators.spotify.com/pod/show/show1/episodes",
        "https://creators.spotify.com/pod/show/show1/episode/wizard",
        "https://creators.spotify.com/pod/show/show1/episode/episode1?ref=mail",
        "https://creators.spotify.com/home/show/show1",
        "https://creators.spotify.com/home/show/show1/",
    ],
)
def test_show_id_from_url(url: str) -> None:
    assert selectors.show_id_from_url(url) == "show1"


@pytest.mark.parametrize(
    "url",
    [
        "https://creators.spotify.com/",
        "https://creators.spotify.com/pod",
        "https://accounts.spotify.com/en/login",
        "about:blank",
    ],
)
def test_show_id_from_url_no_match(url: str) -> None:
    assert selectors.show_id_from_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://creators.spotify.com/pod/show/show1/episode/episode1",
        "https://creators.spotify.com/pod/show/show1/episode/episode1/",
        "https://creators.spotify.com/pod/show/show1/episode/episode1?ref=mail",
    ],
)
def test_episode_id_from_url(url: str) -> None:
    assert selectors.episode_id_from_url(url) == "episode1"


@pytest.mark.parametrize(
    "url",
    [
        "https://creators.spotify.com/pod/show/show1/episode/wizard",
        "https://creators.spotify.com/pod/show/show1/episode/wizard/",
        "https://creators.spotify.com/pod/show/show1/episode/wizard?step=2",
        "https://creators.spotify.com/pod/show/show1/episodes",
        "https://creators.spotify.com/pod/show/show1",
        "https://creators.spotify.com/pod/show/show1/episode/",
        "https://creators.spotify.com/pod/show/show1/episode/wizard#top",
    ],
)
def test_episode_id_from_url_never_returns_the_wizard(url: str) -> None:
    assert selectors.episode_id_from_url(url) is None


def test_url_builders() -> None:
    assert selectors.wizard_url("show1") == (
        "https://creators.spotify.com/pod/show/show1/episode/wizard"
    )
    assert selectors.episodes_url("show1") == (
        "https://creators.spotify.com/pod/show/show1/episodes"
    )


def test_wizard_url_round_trips_through_show_id() -> None:
    assert selectors.show_id_from_url(selectors.wizard_url("show-1")) == "show-1"


def test_thumbnail_selectors_are_scoped_to_the_thumbnail_list() -> None:
    assert selectors.THUMBNAIL_LIST == ("#thumbnail-list",)
    assert selectors.THUMBNAIL_INPUT == ("#thumbnail-list input[type=file]",)
    assert selectors.THUMBNAIL_DELETE == ("#thumbnail-list button[aria-label='Delete']",)
    assert selectors.THUMBNAIL_SELECTED_IMAGE == (
        "#thumbnail-list [role=radio][aria-checked='true'] img",
    )
    for candidates in (
        selectors.THUMBNAIL_INPUT,
        selectors.THUMBNAIL_DELETE,
        selectors.THUMBNAIL_SELECTED_IMAGE,
    ):
        assert all(candidate.startswith("#thumbnail-list") for candidate in candidates)


def test_the_thumbnail_input_is_never_the_duplicated_bare_id() -> None:
    assert selectors.THUMBNAIL_INPUT[0] != selectors.FILE_INPUT[0]
    assert "#uploadAreaInput" not in selectors.THUMBNAIL_INPUT
