"""``install-browser`` contract tests. The process runner is always faked."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from spotify_uploader import cli, install_browser
from spotify_uploader.errors import ConfigurationError, UploaderError

Runner = Callable[..., subprocess.CompletedProcess[str]]
Calls = list[tuple[list[str], dict[str, Any]]]


def _fake_runner(returncode: int = 0, output: str = "") -> tuple[Runner, Calls]:
    calls: Calls = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, stdout=output)

    return runner, calls


def _handle_args(with_deps: bool = False) -> argparse.Namespace:
    return argparse.Namespace(with_deps=with_deps)


def _err_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def test_handle_installs_chromium_and_prints_one_json_line(monkeypatch, capsys) -> None:
    runner, calls = _fake_runner(output="Downloading Chromium 123...\n")
    monkeypatch.setattr(install_browser, "_run", runner)

    code = install_browser.handle(_handle_args())

    out = capsys.readouterr()
    assert code == 0
    assert [line for line in out.out.splitlines() if line.strip()] == [
        json.dumps({"installed": "chromium"})
    ]
    assert "Downloading Chromium 123..." in out.err
    assert calls[0][0] == [sys.executable, "-m", "playwright", "install", "chromium"]
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.STDOUT
    assert calls[0][1]["text"] is True
    assert calls[0][1]["check"] is False


def test_with_deps_is_forwarded_on_linux(monkeypatch) -> None:
    monkeypatch.setattr(install_browser.sys, "platform", "linux")
    runner, calls = _fake_runner()
    monkeypatch.setattr(install_browser, "_run", runner)

    assert install_browser.handle(_handle_args(with_deps=True)) == 0
    assert calls[0][0][-1] == "--with-deps"
    assert calls[0][0][:5] == [sys.executable, "-m", "playwright", "install", "chromium"]


def test_with_deps_outside_linux_is_rejected_without_running(monkeypatch) -> None:
    monkeypatch.setattr(install_browser.sys, "platform", "darwin")
    runner, calls = _fake_runner()
    monkeypatch.setattr(install_browser, "_run", runner)

    with pytest.raises(ConfigurationError, match="only supported on Linux"):
        install_browser.handle(_handle_args(with_deps=True))
    assert calls == []


def test_installer_failure_carries_the_output_tail(monkeypatch, capsys) -> None:
    long_output = "".join(f"installer line {index:03d}\n" for index in range(40))
    long_output += "FINAL: download failed\n"
    runner, _ = _fake_runner(returncode=1, output=long_output)
    monkeypatch.setattr(install_browser, "_run", runner)

    with pytest.raises(UploaderError) as raised:
        install_browser.handle(_handle_args())

    message = str(raised.value)
    tail = long_output.strip()[-300:]
    assert "exit code 1" in message
    assert message.endswith(tail)
    assert "FINAL: download failed" in message
    assert len(tail) == 300
    # The installer's own diagnostics still reach stderr before the error is raised.
    assert "FINAL: download failed" in capsys.readouterr().err


def test_missing_playwright_is_a_configuration_error(monkeypatch) -> None:
    runner, calls = _fake_runner()
    monkeypatch.setattr(install_browser, "_run", runner)
    monkeypatch.setattr(install_browser, "find_spec", lambda name: None)

    with pytest.raises(ConfigurationError, match="Playwright is not installed"):
        install_browser.handle(_handle_args())
    assert calls == []


def test_cli_parses_install_browser_end_to_end(monkeypatch, capsys) -> None:
    runner, _ = _fake_runner(output="browser installed\n")
    monkeypatch.setattr(install_browser, "_run", runner)

    code = cli.main(["install-browser"])

    out = capsys.readouterr()
    assert code == 0
    assert _err_lines(out.err) == ["browser installed"]
    assert json.loads(out.out) == {"installed": "chromium"}
