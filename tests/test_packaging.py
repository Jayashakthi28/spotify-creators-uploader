"""The published artifacts are complete, private-free and installable.

Builds with ``uv`` (skipped where uv is absent) and inspects the wheel and sdist.
The fresh-venv install uses uv's offline cache; if the Playwright dependency is
not cached, that last check skips so the artifact assertions still run.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not installed")

#: Never ship local state, environments or caches, whatever is lying in the tree.
FORBIDDEN_PARTS = (
    "/.venv/",
    "/.pytest_cache/",
    "/.mypy_cache/",
    "/.ruff_cache/",
    "/__pycache__/",
    "/debug/",
    "/chrome-profile/",
)
FORBIDDEN_SUFFIXES = (".pyc", "storage_state.json", "ledger.json", "config.json")


@pytest.fixture(scope="session")
def dist_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    assert UV is not None
    target = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [UV, "build", "--out-dir", str(target)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return target


def _artifact(dist_dir: Path, pattern: str) -> Path:
    matches = sorted(dist_dir.glob(pattern))
    assert len(matches) == 1, f"expected one {pattern}, found {matches!r}"
    return matches[0]


def _assert_no_local_state(names: list[str]) -> None:
    for name in names:
        assert not name.endswith(FORBIDDEN_SUFFIXES), name
        assert not any(part in f"/{name}" for part in FORBIDDEN_PARTS), name


def test_wheel_contains_the_typed_package_and_no_tests(dist_dir: Path) -> None:
    wheel = _artifact(dist_dir, "*.whl")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    assert "spotify_uploader/py.typed" in names
    assert "spotify_uploader/cli.py" in names
    assert not any(name.startswith("tests/") or "/tests/" in name for name in names)
    _assert_no_local_state(names)


def test_sdist_contains_docs_tests_and_no_local_state(dist_dir: Path) -> None:
    sdist = _artifact(dist_dir, "*.tar.gz")

    with tarfile.open(sdist) as archive:
        names = archive.getnames()

    assert any(name.endswith("/LICENSE") for name in names)
    assert any(name.endswith("/README.md") for name in names)
    assert any(name.endswith("/CHANGELOG.md") for name in names)
    assert any(name.endswith("/tests/test_cli.py") for name in names)
    assert any(name.endswith("/pyproject.toml") for name in names)
    _assert_no_local_state(names)


def test_wheel_installs_in_a_fresh_venv_and_runs(dist_dir: Path, tmp_path: Path) -> None:
    assert UV is not None
    wheel = _artifact(dist_dir, "*.whl")
    venv = tmp_path / "venv"

    created = subprocess.run(
        [UV, "venv", "--python", sys.executable, str(venv)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert created.returncode == 0, created.stderr

    python = venv / "bin" / "python"
    installed = subprocess.run(
        [UV, "pip", "install", "--python", str(python), "--offline", str(wheel)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if installed.returncode != 0:
        pytest.skip(f"cannot install the wheel and its dependencies offline: {installed.stderr}")

    script = venv / "bin" / "spotify-uploader"
    version = subprocess.run(
        [str(script), "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("spotify-uploader ")
    assert version.stdout.strip().split()[-1] != "0+unknown"
