"""The documented import surface stays importable, complete and versioned."""

from __future__ import annotations

import subprocess
import sys

import spotify_uploader

REQUIRED_NAMES = {
    "AuthExpiredError",
    "ConfigurationError",
    "EpisodeRequest",
    "LoginResult",
    "MediaInfo",
    "ProgressCallback",
    "PublishStatus",
    "PublishedEpisode",
    "SessionSummary",
    "ShowInfo",
    "ShowSetupError",
    "SpotifyCreatorsUploader",
    "UploadError",
    "UploaderConfig",
    "UploaderError",
    "ValidationError",
    "interactive_login",
    "load_storage_state",
    "prepare_for_spotify",
    "probe_media",
    "spec_issues",
    "summarize",
}


def test_all_public_names_import_and_are_not_none() -> None:
    assert set(spotify_uploader.__all__) >= REQUIRED_NAMES
    for name in spotify_uploader.__all__:
        assert getattr(spotify_uploader, name) is not None, name


def test_version_is_a_string_from_installed_metadata() -> None:
    assert isinstance(spotify_uploader.__version__, str)
    assert spotify_uploader.__version__


def test_python_dash_m_prints_the_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spotify_uploader", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"spotify-uploader {spotify_uploader.__version__}"
