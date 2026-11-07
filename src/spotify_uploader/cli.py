"""Command-line entry point for the Spotify for Creators uploader.

Every error becomes one JSON line on stderr plus a stable exit code, so a server
cron job can branch without parsing human text. ``status`` never touches the
network; only ``check`` and ``upload`` open a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn, TextIO

from spotify_uploader import doctor, install_browser, shows
from spotify_uploader.config import UploaderConfig, save_show_id, validate_show_id
from spotify_uploader.errors import (
    AuthExpiredError,
    ConfigurationError,
    ShowSetupError,
    UploaderError,
    UploadError,
    ValidationError,
)
from spotify_uploader.ledger import Ledger
from spotify_uploader.login import interactive_login
from spotify_uploader.models import EpisodeRequest, PublishedEpisode
from spotify_uploader.prepare import prepare_for_spotify, probe_media, spec_issues
from spotify_uploader.session import SessionSummary, import_session, load_storage_state, summarize
from spotify_uploader.uploader import SpotifyCreatorsUploader

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_UPLOAD = 4


def _write_json(payload: dict[str, Any], stream: TextIO) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), file=stream)


class _ArgumentParser(argparse.ArgumentParser):
    """Report argparse failures in the same JSON shape as every other CLI error."""

    def error(self, message: str) -> NoReturn:
        _write_json(
            {"error": "ArgumentError", "message": message, "step": None, "artifacts": None},
            sys.stderr,
        )
        raise SystemExit(EXIT_CONFIG)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _summary_payload(
    show_id: str | None,
    state_path: Path,
    summary: SessionSummary,
) -> dict[str, Any]:
    return {
        "show_id": show_id,
        "state_path": str(state_path),
        "has_sp_dc": summary.has_sp_dc,
        "expires_at": _iso(summary.sp_dc_expires_at),
    }


def _uploader_config(args: argparse.Namespace) -> UploaderConfig:
    debug_dir = getattr(args, "debug_dir", None)
    return UploaderConfig.load(
        home=getattr(args, "home", None),
        show_id=getattr(args, "show_id", None),
        headless=not bool(getattr(args, "headed", False)),
        channel=getattr(args, "channel", None) or None,
        debug_dir=Path(str(debug_dir)).expanduser() if debug_dir else None,
    )


def _description_from(args: argparse.Namespace) -> str:
    file_value = getattr(args, "description_file", None)
    if file_value:
        path = Path(str(file_value)).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(f"cannot read description file {path}: {exc}") from exc
    value = getattr(args, "description", None)
    return "" if value is None else str(value)


def _cmd_login(args: argparse.Namespace) -> int:
    given_show = validate_show_id(str(args.show_id)) if args.show_id else None
    config = UploaderConfig.load(home=getattr(args, "home", None), show_id=given_show)

    def status(message: str) -> None:
        print(message, file=sys.stderr)

    result = asyncio.run(
        interactive_login(
            config,
            mode=str(args.mode),
            timeout_s=float(args.timeout),
            status=status,
        )
    )
    chosen_show = result.show_id
    if given_show is not None:
        chosen_show = given_show
        save_show_id(config, chosen_show)
    elif chosen_show is None:
        print(
            "No show id was found in the browser URL; pass --show-id SHOW_ID to save one.",
            file=sys.stderr,
        )
    _write_json(
        _summary_payload(chosen_show, result.state_path, result.summary),
        sys.stdout,
    )
    return EXIT_OK


def _cmd_import(args: argparse.Namespace) -> int:
    config = UploaderConfig.load(home=getattr(args, "home", None))
    summary = import_session(
        Path(str(args.source)).expanduser(),
        config.storage_state_path,
    )
    _write_json(
        _summary_payload(config.show_id, config.storage_state_path, summary),
        sys.stdout,
    )
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    config = UploaderConfig.load(home=getattr(args, "home", None))
    present = config.storage_state_path.is_file()
    summary = SessionSummary(has_sp_dc=False, sp_dc_expires_at=None, cookie_count=0)
    if present:
        try:
            summary = summarize(load_storage_state(config.storage_state_path))
        except ConfigurationError:
            # `status` is a diagnostic: report a broken session instead of failing.
            summary = SessionSummary(has_sp_dc=False, sp_dc_expires_at=None, cookie_count=0)
    _write_json(
        {
            "home": str(config.home),
            "session": {
                "present": present,
                "has_sp_dc": summary.has_sp_dc,
                "expires_at": _iso(summary.sp_dc_expires_at),
                "expired": summary.expired,
            },
            "show_id": config.show_id,
            "ledger_entries": len(Ledger(config.ledger_path).all()),
        },
        sys.stdout,
    )
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    config = _uploader_config(args)
    show_id = asyncio.run(SpotifyCreatorsUploader(config).check_session())
    _write_json({"signed_in": True, "show_id": show_id}, sys.stdout)
    return EXIT_OK


def _cmd_upload(args: argparse.Namespace) -> int:
    config = _uploader_config(args)
    upload_timeout = getattr(args, "upload_timeout", None)
    if upload_timeout is not None:
        config = replace(config, upload_timeout_s=float(upload_timeout))
    request = EpisodeRequest(
        video_path=Path(str(args.video)).expanduser(),
        title=str(args.title),
        description=_description_from(args),
        explicit=bool(getattr(args, "explicit", False)),
        thumbnail_path=(
            Path(str(args.thumbnail)).expanduser() if getattr(args, "thumbnail", None) else None
        ),
    )
    request.validate()

    def on_progress(fraction: float, detail: str) -> None:
        print(f"{fraction * 100:6.1f}% {detail}", file=sys.stderr)

    episode: PublishedEpisode = asyncio.run(
        SpotifyCreatorsUploader(config).publish(
            request,
            dry_run=bool(getattr(args, "dry_run", False)),
            force=bool(getattr(args, "force", False)),
            on_progress=on_progress,
            resume_episode_id=getattr(args, "resume_draft", None) or None,
        )
    )
    _write_json(episode.to_dict(), sys.stdout)
    return EXIT_OK


def _cmd_prepare(args: argparse.Namespace) -> int:
    source = Path(str(args.source)).expanduser()
    if args.check:
        info = probe_media(source)
        issues = spec_issues(info)
        _write_json(
            {"path": str(source), "compliant": not issues, "issues": issues, **info.to_dict()},
            sys.stdout,
        )
        return EXIT_OK if not issues else EXIT_CONFIG
    if args.output is None:
        raise ConfigurationError("give an output path: spotify-uploader prepare SRC DST")
    output = Path(str(args.output)).expanduser()
    print(f"re-encoding {source.name} for Spotify (this takes a while)...", file=sys.stderr)
    info = prepare_for_spotify(source, output)
    report = {"path": str(output), "compliant": True, "issues": [], **info.to_dict()}
    _write_json(report, sys.stdout)
    return EXIT_OK


def _emit_error(exc: UploaderError) -> None:
    step: str | None = None
    artifacts: Path | None = None
    if isinstance(exc, UploadError):
        step = exc.step
        artifacts = exc.artifacts
    payload: dict[str, Any] = {
        "error": type(exc).__name__,
        "message": str(exc),
        "step": step,
        "artifacts": str(artifacts) if artifacts is not None else None,
    }
    if isinstance(exc, ShowSetupError):
        payload["draft_episode_id"] = exc.draft_episode_id
    _write_json(payload, sys.stderr)


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "login": _cmd_login,
    "import-session": _cmd_import,
    "status": _cmd_status,
    "check": _cmd_check,
    "prepare": _cmd_prepare,
    "upload": _cmd_upload,
    "shows": shows.handle,
    "doctor": doctor.handle,
    "install-browser": install_browser.handle,
}


def _dispatch(args: argparse.Namespace) -> int:
    handler = _HANDLERS.get(str(getattr(args, "command", "")))
    if handler is None:
        raise ConfigurationError(f"unknown command {getattr(args, 'command', None)!r}")
    return handler(args)


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse tree; exposed so tests and docs can inspect the contract."""
    parser = _ArgumentParser(
        prog="spotify-uploader",
        description="Publish one video episode to Spotify for Creators.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        metavar="PATH",
        help="private state directory (default: ~/.spotify-uploader)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)

    login = subparsers.add_parser("login", help="sign in once and save the browser session")
    login.add_argument("--mode", choices=("auto", "chrome", "playwright"), default="auto")
    login.add_argument("--timeout", type=float, default=600.0, metavar="SECONDS")
    login.add_argument("--show-id", default=None)

    importer = subparsers.add_parser(
        "import-session",
        help="normalize a browser-exported cookie file into a Playwright session",
    )
    importer.add_argument("source", type=Path, metavar="PATH")

    subparsers.add_parser("status", help="show local session and ledger state (offline)")

    check = subparsers.add_parser("check", help="verify the saved session headlessly")
    check.add_argument("--show-id", default=None)
    check.add_argument("--headed", action="store_true")
    check.add_argument("--channel", default=None)

    prepare = subparsers.add_parser(
        "prepare", help="re-encode a video to Spotify's specs, or --check one"
    )
    prepare.add_argument("source", type=Path, metavar="SRC")
    prepare.add_argument("output", type=Path, nargs="?", default=None, metavar="DST")
    prepare.add_argument(
        "--check", action="store_true", help="only report spec problems; exit 2 if any"
    )

    upload = subparsers.add_parser("upload", help="publish one video episode")
    upload.add_argument("video", type=Path)
    upload.add_argument("--title", required=True)
    description = upload.add_mutually_exclusive_group(required=True)
    description.add_argument("--description")
    description.add_argument("--description-file", metavar="PATH")
    upload.add_argument("--explicit", action="store_true")
    upload.add_argument(
        "--thumbnail",
        metavar="IMAGE",
        default=None,
        help="custom 16:9 thumbnail (PNG or JPEG) instead of an auto-generated frame",
    )
    upload.add_argument("--dry-run", action="store_true")
    upload.add_argument("--force", action="store_true")
    upload.add_argument(
        "--resume-draft",
        metavar="EPISODE_ID",
        default=None,
        help="reopen an existing draft instead of uploading the video again",
    )
    upload.add_argument("--show-id", default=None)
    upload.add_argument("--headed", action="store_true")
    upload.add_argument("--channel", default=None)
    upload.add_argument("--debug-dir", metavar="DIR", default=None)
    upload.add_argument("--upload-timeout", type=float, metavar="SECONDS", default=None)
    shows.add_parser(subparsers)
    doctor.add_parser(subparsers)
    install_browser.add_parser(subparsers)
    return parser


def _version() -> str:
    try:
        return metadata.version("spotify-creators-uploader")
    except metadata.PackageNotFoundError:
        return "0+unknown"


def main(argv: Sequence[str] | None = None) -> int:
    """Run one CLI invocation and return its documented exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    try:
        return _dispatch(args)
    except AuthExpiredError as exc:
        _emit_error(exc)
        return EXIT_AUTH
    except (ConfigurationError, ValidationError) as exc:
        _emit_error(exc)
        return EXIT_CONFIG
    except UploaderError as exc:
        _emit_error(exc)
        return EXIT_UPLOAD


if __name__ == "__main__":
    raise SystemExit(main())
