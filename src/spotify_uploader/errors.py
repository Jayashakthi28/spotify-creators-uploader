"""Error types.

Callers branch on the type, never on message text:
``ConfigurationError`` and ``ValidationError`` are fixable by the operator before a
retry, ``AuthExpiredError`` needs a fresh interactive login, ``UploadError`` is a
wizard failure and carries where it happened.
"""

from __future__ import annotations

from pathlib import Path


class UploaderError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigurationError(UploaderError):
    """Missing or unusable local setup: session file, show id, Chrome, video file."""


class ShowSetupError(ConfigurationError):
    """Spotify refuses the first publish until the show's own setup is finished.

    Operator-fixable, not a wizard failure: the episode is saved as a draft whose id
    is in ``draft_episode_id`` so it can be resumed after the show is set up.
    """

    def __init__(self, message: str, *, draft_episode_id: str | None = None) -> None:
        super().__init__(message)
        self.draft_episode_id = draft_episode_id


class ValidationError(UploaderError):
    """The episode request is invalid. Raised before any browser work starts."""


class AuthExpiredError(UploaderError):
    """The saved session is no longer signed in; run ``spotify-uploader login``."""


class UploadError(UploaderError):
    """The wizard failed.

    ``step`` names the wizard step that failed and ``artifacts`` points at the
    directory holding the screenshot, page HTML and trace, when a debug dir is set.
    """

    def __init__(
        self, message: str, *, step: str | None = None, artifacts: Path | None = None
    ) -> None:
        super().__init__(message)
        self.step = step
        self.artifacts = artifacts
