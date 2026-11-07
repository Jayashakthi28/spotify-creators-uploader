"""CLI contract tests: exit codes, JSON shapes, offline status, and injected fakes.

The real uploader and login are always monkeypatched, so nothing here opens a
browser or a socket.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spotify_uploader import cli
from spotify_uploader.config import (
    UploaderConfig,
    read_json_object,
    save_show_id,
)
from spotify_uploader.errors import (
    AuthExpiredError,
    ConfigurationError,
    ShowSetupError,
    UploaderError,
    UploadError,
    ValidationError,
)
from spotify_uploader.ledger import Ledger
from spotify_uploader.login import LoginResult
from spotify_uploader.models import EpisodeRequest, PublishedEpisode, PublishStatus
from spotify_uploader.session import (
    SessionSummary,
    normalize_storage_state,
    save_storage_state,
)

SECRET = "super-secret-cookie-value"


class ExplodingUploader:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the uploader must not be constructed by this command")


def _json_doc(text: str) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected one JSON line, got {lines!r}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _run(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _extension_cookie(**overrides: Any) -> dict[str, Any]:
    cookie: dict[str, Any] = {
        "name": "sp_dc",
        "value": SECRET,
        "domain": ".spotify.com",
        "path": "/",
        "expirationDate": None,
        "httpOnly": True,
        "secure": True,
        "sameSite": "no_restriction",
        "session": True,
    }
    cookie.update(overrides)
    return cookie


def _video(tmp_path: Path, name: str = "video.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not really a video")
    return path


def _episode(**overrides: Any) -> PublishedEpisode:
    values: dict[str, Any] = {
        "status": PublishStatus.PUBLISHED,
        "title": "A title",
        "show_id": "show1",
        "sha256": "a" * 64,
        "published_at": "2026-01-01T00:00:00+00:00",
        "episode_id": "episode1",
        "url": "https://creators.spotify.com/pod/show/show1/episode/episode1",
    }
    values.update(overrides)
    return PublishedEpisode(**values)


def test_status_is_offline_and_reports_an_empty_home(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", ExplodingUploader)
    home = tmp_path / "home"

    code, out, err = _run(capsys, ["--home", str(home), "status"])

    assert code == 0
    assert err == ""
    assert _json_doc(out) == {
        "home": str(home),
        "session": {
            "present": False,
            "has_sp_dc": False,
            "expires_at": None,
            "expired": False,
        },
        "show_id": None,
        "ledger_entries": 0,
    }


def test_status_reports_session_show_and_ledger(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", ExplodingUploader)
    home = tmp_path / "home"
    config = UploaderConfig(home=home)
    expiry = datetime.now(UTC) + timedelta(days=3)
    state = normalize_storage_state(
        [_extension_cookie(expirationDate=expiry.timestamp(), session=False)]
    )
    save_storage_state(config.storage_state_path, state)
    save_show_id(config, "show1")
    Ledger(config.ledger_path).record(_episode())

    code, out, _ = _run(capsys, ["--home", str(home), "status"])
    payload = _json_doc(out)

    assert code == 0
    assert payload["session"]["present"] is True
    assert payload["session"]["has_sp_dc"] is True
    assert payload["session"]["expired"] is False
    assert payload["session"]["expires_at"].endswith("+00:00")
    assert payload["show_id"] == "show1"
    assert payload["ledger_entries"] == 1


def test_status_tolerates_a_corrupt_session_file(tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "storage_state.json").write_text("{not json", encoding="utf-8")

    code, out, _ = _run(capsys, ["--home", str(home), "status"])
    payload = _json_doc(out)

    assert code == 0
    assert payload["session"] == {
        "present": True,
        "has_sp_dc": False,
        "expires_at": None,
        "expired": False,
    }


def test_login_prints_summary_and_passes_options(tmp_path, capsys, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_login(config, *, mode, timeout_s, status):
        captured.update(config=config, mode=mode, timeout_s=timeout_s)
        status("sign in in the browser")
        return LoginResult(
            show_id="found_show",
            state_path=config.storage_state_path,
            summary=SessionSummary(has_sp_dc=True, sp_dc_expires_at=None, cookie_count=2),
        )

    monkeypatch.setattr(cli, "interactive_login", fake_login)
    home = tmp_path / "home"

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(home),
            "login",
            "--mode",
            "playwright",
            "--timeout",
            "5",
        ],
    )

    assert code == 0
    assert _json_doc(out) == {
        "show_id": "found_show",
        "state_path": str(home / "storage_state.json"),
        "has_sp_dc": True,
        "expires_at": None,
    }
    assert err == "sign in in the browser\n"
    assert captured["mode"] == "playwright"
    assert captured["timeout_s"] == 5.0
    assert SECRET not in out


def test_login_saves_an_explicit_show_id(tmp_path, capsys, monkeypatch) -> None:
    async def fake_login(config, *, mode, timeout_s, status):
        del mode, timeout_s, status
        return LoginResult(
            show_id="found_show",
            state_path=config.storage_state_path,
            summary=SessionSummary(has_sp_dc=True, sp_dc_expires_at=None, cookie_count=1),
        )

    monkeypatch.setattr(cli, "interactive_login", fake_login)
    home = tmp_path / "home"

    code, out, _ = _run(capsys, ["--home", str(home), "login", "--show-id", "chosen_show"])

    assert code == 0
    assert _json_doc(out)["show_id"] == "chosen_show"
    assert read_json_object(home / "config.json")["show_id"] == "chosen_show"


def test_login_hints_show_id_when_the_chooser_has_none(tmp_path, capsys, monkeypatch) -> None:
    async def fake_login(config, *, mode, timeout_s, status):
        del mode, timeout_s, status
        return LoginResult(
            show_id=None,
            state_path=config.storage_state_path,
            summary=SessionSummary(has_sp_dc=True, sp_dc_expires_at=None, cookie_count=1),
        )

    monkeypatch.setattr(cli, "interactive_login", fake_login)

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "login"])

    assert code == 0
    assert _json_doc(out)["show_id"] is None
    assert "--show-id" in err


def test_login_configuration_error_returns_two(tmp_path, capsys, monkeypatch) -> None:
    async def fake_login(config, *, mode, timeout_s, status):
        del config, mode, timeout_s, status
        raise ConfigurationError("Google Chrome was not found")

    monkeypatch.setattr(cli, "interactive_login", fake_login)

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "login"])

    assert code == 2
    assert out == ""
    payload = _json_doc(err)
    assert payload["error"] == "ConfigurationError"
    assert payload["step"] is None
    assert payload["artifacts"] is None


def test_import_session_normalizes_a_browser_export(tmp_path, capsys) -> None:
    home = tmp_path / "home"
    source = tmp_path / "cookies.json"
    source.write_text(json.dumps([_extension_cookie()]), encoding="utf-8")

    code, out, err = _run(capsys, ["--home", str(home), "import-session", str(source)])

    assert code == 0
    assert err == ""
    assert _json_doc(out) == {
        "show_id": None,
        "state_path": str(home / "storage_state.json"),
        "has_sp_dc": True,
        "expires_at": None,
    }
    saved = home / "storage_state.json"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert json.loads(saved.read_text(encoding="utf-8"))["cookies"][0]["sameSite"] == "None"


def test_check_uses_headless_config_and_prints_signed_in(tmp_path, capsys, monkeypatch) -> None:
    configs: list[UploaderConfig] = []

    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            configs.append(config)

        async def check_session(self) -> str:
            return "show9"

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)
    home = tmp_path / "home"

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(home),
            "check",
            "--headed",
            "--channel",
            "chrome",
            "--show-id",
            "show9",
        ],
    )

    assert code == 0
    assert err == ""
    assert _json_doc(out) == {"signed_in": True, "show_id": "show9"}
    assert configs[0].home == home
    assert configs[0].headless is False
    assert configs[0].channel == "chrome"
    assert configs[0].show_id == "show9"


def test_check_auth_expired_returns_three(tmp_path, capsys, monkeypatch) -> None:
    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            del config

        async def check_session(self) -> str:
            raise AuthExpiredError("saved session is signed out")

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)

    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "check"])

    assert code == 3
    assert out == ""
    assert _json_doc(err) == {
        "error": "AuthExpiredError",
        "message": "saved session is signed out",
        "step": None,
        "artifacts": None,
    }


def test_upload_success_prints_episode_and_progress(tmp_path, capsys, monkeypatch) -> None:
    captured: dict[str, Any] = {}
    episode = _episode()

    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            captured["config"] = config

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            captured.update(request=request, dry_run=dry_run, force=force)
            if on_progress is not None:
                on_progress(0.25, "uploading")
            return episode

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)
    video = _video(tmp_path)
    debug_dir = tmp_path / "debug"
    home = tmp_path / "home"

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(home),
            "upload",
            str(video),
            "--title",
            "A title",
            "--description",
            "A description",
            "--explicit",
            "--dry-run",
            "--force",
            "--show-id",
            "show1",
            "--headed",
            "--channel",
            "chrome",
            "--debug-dir",
            str(debug_dir),
            "--upload-timeout",
            "42",
        ],
    )

    assert code == 0
    assert _json_doc(out) == episode.to_dict()
    assert " 25.0% uploading" in err
    request = captured["request"]
    assert isinstance(request, EpisodeRequest)
    assert request.video_path == video
    assert request.title == "A title"
    assert request.description == "A description"
    assert request.explicit is True
    assert captured["dry_run"] is True
    assert captured["force"] is True
    config = captured["config"]
    assert config.headless is False
    assert config.channel == "chrome"
    assert config.show_id == "show1"
    assert config.debug_dir == debug_dir
    assert config.upload_timeout_s == 42.0


def test_upload_reads_description_file(tmp_path, capsys, monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            del config

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            del dry_run, force, on_progress
            captured["request"] = request
            return _episode()

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)
    video = _video(tmp_path)
    description = tmp_path / "description.txt"
    description.write_text("line one\nline two\n", encoding="utf-8")

    code, out, _ = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(video),
            "--title",
            "A title",
            "--description-file",
            str(description),
        ],
    )

    assert code == 0
    assert _json_doc(out)["status"] == "published"
    assert captured["request"].description == "line one\nline two\n"


def test_upload_rejects_missing_video_before_building_the_uploader(
    tmp_path, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", ExplodingUploader)

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(tmp_path / "absent.mp4"),
            "--title",
            "A title",
            "--description",
            "A description",
        ],
    )

    assert code == 2
    assert out == ""
    assert _json_doc(err)["error"] == "ValidationError"


def test_upload_rejects_missing_description_file(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", ExplodingUploader)

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(_video(tmp_path)),
            "--title",
            "A title",
            "--description-file",
            str(tmp_path / "absent.txt"),
        ],
    )

    assert code == 2
    assert out == ""
    assert _json_doc(err)["error"] == "ConfigurationError"


def test_upload_requires_exactly_one_description_source(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", ExplodingUploader)
    video = _video(tmp_path)

    missing = _run(
        capsys,
        ["--home", str(tmp_path / "home"), "upload", str(video), "--title", "A title"],
    )
    assert missing[0] == 2
    assert _json_doc(missing[2])["error"] == "ArgumentError"

    both = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(video),
            "--title",
            "A title",
            "--description",
            "one",
            "--description-file",
            str(tmp_path / "description.txt"),
        ],
    )
    assert both[0] == 2
    assert both[1] == ""
    assert _json_doc(both[2])["error"] == "ArgumentError"


@pytest.mark.parametrize(
    "error",
    [
        UploadError("publish failed", step="publish", artifacts=Path("/tmp/debug")),
        UploaderError("something else"),
    ],
)
def test_upload_errors_return_four(tmp_path, capsys, monkeypatch, error) -> None:
    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            del config

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            del request, dry_run, force, on_progress
            raise error

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)

    code, out, err = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(_video(tmp_path)),
            "--title",
            "A title",
            "--description",
            "A description",
        ],
    )

    assert code == 4
    assert out == ""
    payload = _json_doc(err)
    assert payload["error"] == type(error).__name__
    assert payload["message"] == str(error)
    if isinstance(error, UploadError):
        assert payload["step"] == "publish"
        assert payload["artifacts"] == "/tmp/debug"
    else:
        assert payload["step"] is None
        assert payload["artifacts"] is None


def test_unknown_command_is_an_argparse_error(tmp_path, capsys) -> None:
    code, out, err = _run(capsys, ["--home", str(tmp_path / "home"), "nonsense"])

    assert code == 2
    assert out == ""
    assert _json_doc(err)["error"] == "ArgumentError"


def test_no_subcommand_is_an_argparse_error(capsys) -> None:
    code, out, err = _run(capsys, [])

    assert code == 2
    assert out == ""
    assert _json_doc(err)["error"] == "ArgumentError"


def test_help_exits_zero(capsys) -> None:
    code, out, _ = _run(capsys, ["--help"])

    assert code == 0
    assert "usage: spotify-uploader" in out


def test_validation_error_maps_to_two(tmp_path, capsys, monkeypatch) -> None:
    class FakeUploader:
        def __init__(self, config: UploaderConfig) -> None:
            del config

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            del request, dry_run, force, on_progress
            raise ValidationError("title must not be empty")

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)

    code, _, err = _run(
        capsys,
        [
            "--home",
            str(tmp_path / "home"),
            "upload",
            str(_video(tmp_path)),
            "--title",
            "A title",
            "--description",
            "A description",
        ],
    )

    assert code == 2
    assert _json_doc(err)["error"] == "ValidationError"


def test_upload_passes_resume_draft_through(tmp_path, capsys, monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeUploader:
        def __init__(self, config) -> None:
            self.config = config

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            seen["resume"] = resume_episode_id
            return PublishedEpisode(
                status=PublishStatus.PUBLISHED,
                title=request.title,
                show_id="show1",
                sha256="abc",
                published_at="2026-01-01T00:00:00+00:00",
                episode_id=resume_episode_id,
            )

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    code = cli.main(
        [
            "--home", str(tmp_path / "home"),
            "upload", str(video),
            "--title", "T", "--description", "D",
            "--show-id", "show1",
            "--resume-draft", "draft-1",
        ]
    )  # fmt: skip

    assert code == 0
    assert seen["resume"] == "draft-1"
    assert json.loads(capsys.readouterr().out)["episode_id"] == "draft-1"


def test_show_setup_error_exits_2_and_names_the_draft(tmp_path, capsys, monkeypatch) -> None:
    class FakeUploader:
        def __init__(self, config) -> None:
            pass

        async def publish(self, request, *, dry_run, force, on_progress, resume_episode_id=None):
            raise ShowSetupError("finish setup first", draft_episode_id="draft-9")

    monkeypatch.setattr(cli, "SpotifyCreatorsUploader", FakeUploader)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")

    code = cli.main(
        [
            "--home", str(tmp_path / "home"),
            "upload", str(video),
            "--title", "T", "--description", "D",
            "--show-id", "show1",
        ]
    )  # fmt: skip

    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "ShowSetupError"
    assert error["draft_episode_id"] == "draft-9"
