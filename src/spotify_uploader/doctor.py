"""``spotify-uploader doctor``: find out why uploads would fail, before they do.

Every question a support request would ask — Python version, Chromium, ffmpeg,
session freshness, show id, sign-in, show setup — becomes one :class:`Check`. A
diagnosable problem is reported as data, never raised, so the command always
finishes with a full picture instead of stopping at the first surprise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from spotify_uploader.config import UploaderConfig, default_home
from spotify_uploader.errors import (
    AuthExpiredError,
    ConfigurationError,
    ShowSetupError,
    UploaderError,
    UploadError,
    ValidationError,
)
from spotify_uploader.models import EpisodeRequest
from spotify_uploader.session import load_storage_state, summarize

if TYPE_CHECKING:
    from spotify_uploader.uploader import SpotifyCreatorsUploader

EXIT_OK = 0
EXIT_CONFIG = 2

CheckStatus = Literal["ok", "warn", "fail", "skipped"]

_LOGIN_FIX = "run: spotify-uploader login"
_SHOW_ID_FIX = "spotify-uploader shows --select ID_OR_NAME or --show-id ID"
_SHOW_SETUP_FIX = (
    "finish the show's setup in Spotify for Creators (name, description, cover art); "
    "Spotify refuses the first publish until then"
)
_UI_CHANGED_FIX = (
    "Spotify may have changed its UI: update the package "
    "(pip install -U spotify-creators-uploader) or report the artifacts"
)
_REHEARSAL_TITLE = "spotify-uploader doctor rehearsal"
_REHEARSAL_DESCRIPTION = "Automated rehearsal; safe to delete."
_REHEARSAL_UPLOAD_TIMEOUT_S = 180.0
_REHEARSAL_CLIP_SECONDS = 8
_CLIP_TIMEOUT_S = 120.0
_SESSION_WARN_DAYS = timedelta(days=14)

_STATUS_LABELS: dict[CheckStatus, str] = {
    "ok": "[ ok ]",
    "warn": "[warn]",
    "fail": "[FAIL]",
    "skipped": "[skip]",
}

#: What a failed wizard step most likely says about a real (not fake) Spotify page.
_STEP_EXPLANATIONS: dict[str, str] = {
    "open": "the episodes/wizard page did not load",
    "upload": "the file input or the upload progress never became usable",
    "title": "the title field did not accept the text",
    "description": "the rich-text description editor did not accept the text",
    "content-check": "the explicit-content switch was missing or would not change",
    "next": "the Next button did not advance to the review step",
    "publish-now": "the Now/Schedule choice did not become selected",
    "thumbnail": "the thumbnail control did not accept the image",
}


@dataclass(frozen=True, slots=True)
class Check:
    """One diagnosis: what was looked at, what was found, and what to do.

    Problems travel as checks rather than exceptions so that one broken piece
    (say, a missing browser) never hides the state of everything else.
    """

    name: str
    status: CheckStatus
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser("doctor", help="diagnose the install, session and show")
    parser.add_argument("--show-id", default=None)
    parser.add_argument(
        "--offline", action="store_true", help="skip every check that uses the network"
    )
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="also walk the whole upload wizard as a dry run (leaves a draft on Spotify)",
    )
    parser.add_argument("--video", metavar="PATH", default=None)
    parser.add_argument("--thumbnail", metavar="PATH", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--channel", default=None)


def handle(args: argparse.Namespace) -> int:
    """Run the checks, render them as text or JSON, and return 0 or 2."""
    try:
        checks = asyncio.run(_run_checks(args))
    except Exception as exc:
        checks = [Check("doctor", "fail", f"the doctor itself crashed: {_one_line(str(exc))}")]
    if bool(getattr(args, "as_json", False)):
        _print_json(checks)
    else:
        _print_human(checks)
    return EXIT_OK if not _any_failed(checks) else EXIT_CONFIG


async def _run_checks(args: argparse.Namespace) -> list[Check]:
    config, config_error = _resolve_config(args)
    checks = [
        _check_python(),
        await _check_browser(),
        _check_ffmpeg(),
        _check_session(config),
        _check_show_id(config, config_error),
    ]
    rehearsal_requested = bool(getattr(args, "rehearse", False))

    if bool(getattr(args, "offline", False)):
        reason = "offline mode (--offline)"
        checks.append(Check("signed-in", "skipped", reason))
        checks.append(Check("show", "skipped", reason))
        if rehearsal_requested:
            checks.append(Check("rehearsal", "skipped", reason))
        return checks

    if checks[3].status == "fail" or checks[4].status == "fail":
        session_failed = checks[3].status == "fail"
        reason = "needs a saved session" if session_failed else "needs a configured show id"
        checks.append(Check("signed-in", "skipped", reason))
        checks.append(Check("show", "skipped", reason))
        if rehearsal_requested:
            checks.append(
                Check("rehearsal", "skipped", "the session and show checks must pass first")
            )
        return checks

    try:
        uploader = _make_uploader(config)
    except Exception as exc:
        message = f"could not create the uploader: {_one_line(str(exc))}"
        checks.append(Check("signed-in", "fail", message))
        checks.append(Check("show", "skipped", "the uploader could not be created"))
        if rehearsal_requested:
            checks.append(Check("rehearsal", "skipped", "the uploader could not be created"))
        return checks

    checks.append(await _check_signed_in(uploader))
    checks.append(await _check_show(uploader))
    if rehearsal_requested:
        if _any_failed(checks):
            checks.append(Check("rehearsal", "skipped", "an earlier online check failed"))
        else:
            checks.append(await _check_rehearsal(config, args))
    return checks


def _resolve_config(args: argparse.Namespace) -> tuple[UploaderConfig, str | None]:
    """Load settings, keeping a usable config even when the stored show id is broken."""
    home = getattr(args, "home", None)
    headless = not bool(getattr(args, "headed", False))
    channel = getattr(args, "channel", None) or None
    try:
        config = UploaderConfig.load(
            home=home,
            show_id=getattr(args, "show_id", None),
            headless=headless,
            channel=channel,
        )
    except ConfigurationError as exc:
        fallback_home = Path(str(home)).expanduser() if home else default_home()
        fallback = UploaderConfig(home=fallback_home, headless=headless, channel=channel)
        return fallback, _one_line(str(exc))
    return config, None


def _make_uploader(config: UploaderConfig) -> SpotifyCreatorsUploader:
    """Build the real uploader; a module seam so tests can inject a fake site."""
    from spotify_uploader.uploader import SpotifyCreatorsUploader as Uploader

    return Uploader(config)


def _check_python() -> Check:
    major, minor = _python_version_info()
    version = platform.python_version()
    if (major, minor) >= (3, 11):
        return Check("python", "ok", f"Python {version}")
    return Check(
        "python",
        "fail",
        f"Python {version} is too old; this package needs 3.11 or newer",
        fix="install Python 3.11 or newer and reinstall the package",
    )


def _python_version_info() -> tuple[int, int]:
    """The running interpreter's version, indirectly so tests can force a failure."""
    return sys.version_info.major, sys.version_info.minor


async def _check_browser() -> Check:
    try:
        await _launch_probe()
    except Exception as exc:
        return Check(
            "browser",
            "fail",
            f"headless Chromium could not start: {_one_line(str(exc))}",
            fix="run: spotify-uploader install-browser",
        )
    return Check("browser", "ok", "headless Chromium starts")


async def _launch_probe() -> None:
    """Launch and close Chromium; kept separate so tests never start a browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await browser.close()


def _check_ffmpeg() -> Check:
    missing = [name for name in ("ffmpeg", "ffprobe") if _which(name) is None]
    if missing:
        return Check(
            "ffmpeg",
            "warn",
            f"{' and '.join(missing)} not on PATH; only needed for `prepare` "
            "and for --rehearse without --video",
            fix="install ffmpeg (e.g. brew install ffmpeg or apt install ffmpeg)",
        )
    return Check("ffmpeg", "ok", "ffmpeg and ffprobe are on PATH")


def _which(command: str) -> str | None:
    """Locate an executable; indirection keeps ffmpeg checks injectable in tests."""
    return shutil.which(command)


def _check_session(config: UploaderConfig) -> Check:
    path = config.storage_state_path
    if not path.is_file():
        return Check(
            "session",
            "fail",
            f"no saved session at {path}",
            fix=_LOGIN_FIX,
        )
    try:
        summary = summarize(load_storage_state(path))
    except ConfigurationError as exc:
        # The message never carries a cookie value; load_storage_state is careful.
        return Check("session", "fail", _one_line(str(exc)), fix=_LOGIN_FIX)
    if not summary.has_sp_dc:
        return Check("session", "fail", "the saved session has no sp_dc cookie", fix=_LOGIN_FIX)
    if summary.expired:
        expires = summary.sp_dc_expires_at
        when = f" on {expires:%Y-%m-%d}" if expires is not None else ""
        return Check("session", "fail", f"the sp_dc cookie expired{when}", fix=_LOGIN_FIX)
    expires = summary.sp_dc_expires_at
    if expires is None:
        return Check(
            "session",
            "ok",
            f"{summary.cookie_count} cookie(s); sp_dc present, no expiry recorded",
        )
    remaining = expires - datetime.now(UTC)
    if remaining <= _SESSION_WARN_DAYS:
        days = max(remaining.days, 0)
        return Check(
            "session",
            "warn",
            f"sp_dc expires in {'less than a day' if days == 0 else f'{days} day(s)'} "
            f"on {expires:%Y-%m-%d}; re-run login before it does",
            fix=_LOGIN_FIX,
        )
    return Check(
        "session",
        "ok",
        f"{summary.cookie_count} cookie(s); sp_dc expires {expires:%Y-%m-%d}",
    )


def _check_show_id(config: UploaderConfig, config_error: str | None) -> Check:
    if config_error is not None:
        return Check("show-id", "fail", config_error, fix=_SHOW_ID_FIX)
    if not config.show_id:
        return Check("show-id", "fail", "no show id configured", fix=_SHOW_ID_FIX)
    return Check("show-id", "ok", f"using show id {config.show_id}")


async def _check_signed_in(uploader: SpotifyCreatorsUploader) -> Check:
    try:
        show_id = await uploader.check_session()
    except AuthExpiredError:
        return Check("signed-in", "fail", "the saved session is signed out", fix=_LOGIN_FIX)
    except UploaderError as exc:
        return Check("signed-in", "fail", _one_line(str(exc)))
    except Exception as exc:
        return Check(
            "signed-in", "fail", f"the browser could not open Spotify: {_one_line(str(exc))}"
        )
    return Check("signed-in", "ok", f"signed in; the episode list for {show_id} opened")


async def _check_show(uploader: SpotifyCreatorsUploader) -> Check:
    try:
        info = await uploader.inspect_show()
    except AuthExpiredError:
        return Check("show", "fail", "the saved session is signed out", fix=_LOGIN_FIX)
    except NotImplementedError:
        return Check(
            "show",
            "warn",
            "this build cannot read the show dashboard yet; the wizard checks still apply",
        )
    except UploaderError as exc:
        return Check("show", "fail", _one_line(str(exc)))
    except Exception as exc:
        return Check("show", "fail", f"could not inspect the show: {_one_line(str(exc))}")
    label = info.name or "unnamed show"
    if info.setup_complete is False:
        return Check(
            "show",
            "fail",
            f"{label} ({info.show_id}) has not finished its setup",
            fix=_SHOW_SETUP_FIX,
        )
    if info.setup_complete is None:
        return Check(
            "show",
            "warn",
            f"could not tell whether {label} ({info.show_id}) finished its setup",
        )
    return Check("show", "ok", f"{label} ({info.show_id}) is ready to publish")


async def _check_rehearsal(config: UploaderConfig, args: argparse.Namespace) -> Check:
    workdir = _make_workdir()
    keep_workdir = False
    try:
        video_arg = getattr(args, "video", None)
        if video_arg:
            video = Path(str(video_arg)).expanduser()
        else:
            ffmpeg = _which("ffmpeg")
            if ffmpeg is None:
                return Check(
                    "rehearsal",
                    "skipped",
                    "ffmpeg is not on PATH and no --video was given, "
                    "so there is no clip to rehearse with",
                    fix="install ffmpeg or pass --video PATH",
                )
            try:
                video = _generate_test_clip(ffmpeg, workdir)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                return Check(
                    "rehearsal",
                    "fail",
                    f"could not create the rehearsal clip: {_one_line(str(exc))}",
                )

        thumbnail_arg = getattr(args, "thumbnail", None)
        thumbnail = Path(str(thumbnail_arg)).expanduser() if thumbnail_arg else None
        rehearsal_config = replace(
            config,
            debug_dir=workdir / "debug",
            upload_timeout_s=_REHEARSAL_UPLOAD_TIMEOUT_S,
        )
        try:
            uploader = _make_uploader(rehearsal_config)
        except Exception as exc:
            return Check(
                "rehearsal", "fail", f"could not create the uploader: {_one_line(str(exc))}"
            )
        request = EpisodeRequest(
            video_path=video,
            title=_REHEARSAL_TITLE,
            description=_REHEARSAL_DESCRIPTION,
            thumbnail_path=thumbnail,
        )
        try:
            await uploader.publish(request, dry_run=True, force=True)
        except AuthExpiredError:
            return Check(
                "rehearsal",
                "fail",
                "the saved session is signed out during the rehearsal",
                fix=_LOGIN_FIX,
            )
        except ShowSetupError as exc:
            keep_workdir = True
            return Check("rehearsal", "fail", _one_line(str(exc)), fix=_SHOW_SETUP_FIX)
        except UploadError as exc:
            keep_workdir = exc.artifacts is not None
            return _rehearsal_failure(exc)
        except ValidationError as exc:
            return Check(
                "rehearsal",
                "fail",
                _one_line(str(exc)),
                fix="check the --video and --thumbnail paths; the video must be .mp4 or .mov",
            )
        except UploaderError as exc:
            return Check("rehearsal", "fail", _one_line(str(exc)), fix=_UI_CHANGED_FIX)
        except Exception as exc:
            return Check(
                "rehearsal",
                "fail",
                f"the rehearsal crashed: {_one_line(str(exc))}",
                fix=_UI_CHANGED_FIX,
            )
        return Check(
            "rehearsal",
            "ok",
            f"the wizard walked to the review step; Spotify keeps a DRAFT titled "
            f"{_REHEARSAL_TITLE!r} that should be deleted in the Episodes list",
        )
    finally:
        if not keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def _make_workdir() -> Path:
    """Scratch space for the test clip and failure artifacts; kept only on failure."""
    return Path(tempfile.mkdtemp(prefix="spotify-uploader-doctor-"))


def _generate_test_clip(ffmpeg: str, target_dir: Path) -> Path:
    """Render a small synthetic clip so a rehearsal never needs a real episode."""
    target_dir.mkdir(parents=True, exist_ok=True)
    output = target_dir / "doctor-rehearsal.mp4"
    seconds = _REHEARSAL_CLIP_SECONDS
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=1920x1080:rate=24:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=_CLIP_TIMEOUT_S, check=False
    )
    if completed.returncode != 0 or not output.is_file():
        lines = (completed.stderr or completed.stdout).strip().splitlines()
        reason = lines[-1] if lines else f"ffmpeg exited with code {completed.returncode}"
        raise RuntimeError(f"ffmpeg could not render the test clip: {reason}")
    return output


def _rehearsal_failure(exc: UploadError) -> Check:
    step = exc.step or "unknown"
    explanation = _STEP_EXPLANATIONS.get(step, "the wizard failed somewhere unexpected")
    detail = f"the rehearsal failed at step {step!r}: {explanation} ({_one_line(str(exc))})"
    if exc.artifacts is not None:
        detail = f"{detail}; artifacts: {exc.artifacts}"
    return Check("rehearsal", "fail", detail, fix=_UI_CHANGED_FIX)


def _any_failed(checks: Sequence[Check]) -> bool:
    return any(check.status == "fail" for check in checks)


def _print_human(checks: Sequence[Check]) -> None:
    for check in checks:
        print(f"{_STATUS_LABELS[check.status]} {check.name}: {check.detail}")
        if check.fix:
            print(f"  fix: {check.fix}")
    print(_summary_line(checks))


def _summary_line(checks: Sequence[Check]) -> str:
    counts = {
        status: sum(1 for check in checks if check.status == status)
        for status in ("ok", "warn", "fail", "skipped")
    }
    return (
        f"doctor: {counts['ok']} ok, {counts['warn']} warning(s), "
        f"{counts['fail']} failure(s), {counts['skipped']} skipped"
    )


def _print_json(checks: Sequence[Check]) -> None:
    payload = {"ok": not _any_failed(checks), "checks": [check.to_dict() for check in checks]}
    print(json.dumps(payload, ensure_ascii=False))


def _one_line(text: str, *, limit: int = 300) -> str:
    """Collapse provider error blobs to one readable line per check."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."
