"""``spotify-uploader install-browser``: pip cannot install Chromium, so this does.

Playwright ships its browsers separately from the wheel, so a fresh install needs
one extra download. Running the installer in the active interpreter keeps that
browser discoverable by the same Playwright version the uploader imports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from importlib.util import find_spec
from typing import Any

from spotify_uploader.errors import ConfigurationError, UploaderError

#: Enough of a failed installer's output to identify the cause, without pasting pages.
_OUTPUT_TAIL_CHARS = 300

#: Module-level so tests can replace the runner and never download anything.
_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run


def add_parser(subparsers: argparse._SubParsersAction[Any]) -> None:
    parser = subparsers.add_parser(
        "install-browser", help="download the Chromium build Playwright drives"
    )
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help="also install Linux system libraries (needs root; Linux only)",
    )


def _output_tail(output: str) -> str:
    return output.strip()[-_OUTPUT_TAIL_CHARS:]


def handle(args: argparse.Namespace) -> int:
    """Download Chromium for the interpreter running this command."""
    with_deps = bool(getattr(args, "with_deps", False))
    if with_deps and not sys.platform.startswith("linux"):
        raise ConfigurationError("--with-deps is only supported on Linux")
    if find_spec("playwright") is None:
        raise ConfigurationError("Playwright is not installed; reinstall spotify-creators-uploader")

    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    if with_deps:
        command.append("--with-deps")
    completed = _run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = completed.stdout or ""
    # Forward the installer's own progress, keeping stdout for the one JSON result.
    if output:
        print(output, file=sys.stderr, end="" if output.endswith("\n") else "\n")
    if completed.returncode != 0:
        message = f"playwright install failed with exit code {completed.returncode}"
        detail = _output_tail(output)
        raise UploaderError(f"{message}: {detail}" if detail else message)
    print(json.dumps({"installed": "chromium"}))
    return 0
