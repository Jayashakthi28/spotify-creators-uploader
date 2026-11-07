"""Headless Playwright uploader for Spotify for Creators video episodes."""

from importlib import metadata

from spotify_uploader.config import UploaderConfig
from spotify_uploader.errors import (
    AuthExpiredError,
    ConfigurationError,
    ShowSetupError,
    UploaderError,
    UploadError,
    ValidationError,
)
from spotify_uploader.models import (
    EpisodeRequest,
    ProgressCallback,
    PublishedEpisode,
    PublishStatus,
)
from spotify_uploader.shows import ShowInfo
from spotify_uploader.uploader import SpotifyCreatorsUploader

try:
    __version__ = metadata.version("spotify-creators-uploader")
except metadata.PackageNotFoundError:
    # A source checkout that has not been installed still has to import.
    __version__ = "0+unknown"

__all__ = [
    "AuthExpiredError",
    "ConfigurationError",
    "EpisodeRequest",
    "ProgressCallback",
    "PublishStatus",
    "PublishedEpisode",
    "ShowInfo",
    "ShowSetupError",
    "SpotifyCreatorsUploader",
    "UploadError",
    "UploaderConfig",
    "UploaderError",
    "ValidationError",
    "__version__",
]
