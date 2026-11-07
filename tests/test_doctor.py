"""Doctor contract tests: every check's ok/fail path, rendering, exit codes.

Browser probes and the uploader are injected everywhere except the two
rehearsal tests, which drive the real uploader through ``fake_spotify`` so no
test ever reaches Spotify or the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import BrowserContext

from fake_spotify import SHOW_ID, FakeBehavior, FakeSpotify, install_fake_spotify
from spotify_uploader import cli, doctor
from spotify_uploader.errors import (
    AuthExpiredError,
    ShowSetupError,
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

SECRET = "doctor-secret-sp-dc-b7e1"
TITLE = "spotify-uploader doctor rehearsal"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never launch Chromium or build the real uploader unless a test replaces this."""

    async def probe() -> None:
        return None

    def factory(config: object) -> object:
        raise AssertionError("this test must inject doctor._make_uploader")

    monkeypatch.setattr(doctor, "_launch_probe", probe)
    monkeypatch.setattr(doctor, "_make_uploader", factory)
    monkeypatch.setattr(doctor, "_which", lambda name: f"/usr/bin/{name}")


def _home(
    tmp_path: Path,
    *,
    expires: float | None = None,
    secret: str = SECRET,
    cookie_name: str = "sp_dc",
) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    cookie = {
        "name": cookie_name,
        "value": secret,
        "domain": ".spotify.com",
        "path": "/",
        "expires": -1 if expires is None else expires,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    state = {"cookies": [cookie], "origins": []}
    (home / "storage_state.json").write_text(json.dumps(state), encoding="utf-8")
    return home


def _tiny_video(tmp_path: Path) -> Path:
    path = tmp_path / "tiny.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"doctor" * 8)
    return path


def _run(
    capsys: pytest.CaptureFixture[str],
    home: Path,
    extra: tuple[str, ...] = (),
    *,
    show_id: str | None = SHOW_ID,
    json_mode: bool = True,
) -> tuple[int, dict[str, Any], str, str]:
    argv = ["--home", str(home), "doctor"]
    if json_mode:
        argv.append("--json")
    if show_id is not None:
        argv.extend(["--show-id", show_id])
    argv.extend(extra)
    code = cli.main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if json_mode else {}
    return code, payload, captured.out, captured.err


def _by_name(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["name"]: check for check in payload["checks"]}


class StubUploader:
    """Stand-in for the real uploader; each method can be told to fail."""

    def __init__(
        self,
        *,
        session_error: Exception | None = None,
        show: ShowInfo | None = None,
        show_error: Exception | None = None,
        publish_error: Exception | None = None,
    ) -> None:
        self.session_error = session_error
        self.show = show if show is not None else ShowInfo(SHOW_ID, "Test show", True)
        self.show_error = show_error
        self.publish_error = publish_error
        self.publish_calls: list[dict[str, Any]] = []

    async def check_session(self) -> str:
        if self.session_error is not None:
            raise self.session_error
        return SHOW_ID

    async def inspect_show(self) -> ShowInfo:
        if self.show_error is not None:
            raise self.show_error
        return self.show

    async def publish(
        self,
        request: EpisodeRequest,
        *,
        dry_run: bool = False,
        force: bool = False,
        on_progress: ProgressCallback | None = None,
        resume_episode_id: str | None = None,
    ) -> PublishedEpisode:
        del on_progress
        self.publish_calls.append(
            {"request": request, "dry_run": dry_run, "force": force, "resume": resume_episode_id}
        )
        if self.publish_error is not None:
            raise self.publish_error
        return PublishedEpisode(
            status=PublishStatus.DRY_RUN,
            title=request.title,
            show_id=SHOW_ID,
            sha256="0" * 64,
            published_at="2026-01-01T00:00:00+00:00",
        )


def _inject(monkeypatch: pytest.MonkeyPatch, uploader: object) -> None:
    monkeypatch.setattr(doctor, "_make_uploader", lambda config: uploader)


# --- offline local checks ---------------------------------------------------


def test_python_check_ok(tmp_path, capsys) -> None:
    code, payload, out, err = _run(capsys, _home(tmp_path), ("--offline",))

    assert code == 0
    assert err == ""
    python = _by_name(payload)["python"]
    assert python["status"] == "ok"
    assert python["detail"].startswith("Python 3.")
    assert python["fix"] is None


def test_python_check_fails_on_an_old_interpreter(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_python_version_info", lambda: (3, 10))

    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    python = _by_name(payload)["python"]
    assert python["status"] == "fail"
    assert "3.11" in python["detail"]
    assert python["fix"] is not None
    assert code == 2


def test_browser_check_ok(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    assert code == 0
    assert _by_name(payload)["browser"]["status"] == "ok"


def test_browser_check_fails_with_install_browser_fix(tmp_path, capsys, monkeypatch) -> None:
    async def broken_probe() -> None:
        raise RuntimeError("Executable doesn't exist at ~/Library/Caches/ms-playwright")

    monkeypatch.setattr(doctor, "_launch_probe", broken_probe)

    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    browser = _by_name(payload)["browser"]
    assert browser["status"] == "fail"
    assert "could not start" in browser["detail"]
    assert browser["fix"] == "run: spotify-uploader install-browser"
    assert code == 2


def test_ffmpeg_check_warns_when_missing(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_which", lambda name: None)

    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    ffmpeg = _by_name(payload)["ffmpeg"]
    assert ffmpeg["status"] == "warn"
    assert "only needed for `prepare`" in ffmpeg["detail"]
    assert "--rehearse without --video" in ffmpeg["detail"]
    assert code == 0


def test_ffmpeg_check_ok_when_both_binaries_exist(tmp_path, capsys) -> None:
    _, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    assert _by_name(payload)["ffmpeg"]["status"] == "ok"


# --- session ---------------------------------------------------------------


def test_session_check_ok_and_never_prints_the_cookie(tmp_path, capsys) -> None:
    code, payload, out, err = _run(capsys, _home(tmp_path), ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "ok"
    assert "sp_dc present" in session["detail"]
    assert SECRET not in out
    assert SECRET not in err
    assert code == 0


def test_session_check_warns_when_it_expires_soon(tmp_path, capsys) -> None:
    expires = (datetime.now(UTC) + timedelta(days=5, hours=1)).timestamp()

    _, payload, _, _ = _run(capsys, _home(tmp_path, expires=expires), ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "warn"
    assert "5 day(s)" in session["detail"]
    assert session["fix"] == "run: spotify-uploader login"


def test_session_check_fails_when_already_expired(tmp_path, capsys) -> None:
    expires = (datetime.now(UTC) - timedelta(days=1)).timestamp()

    code, payload, _, _ = _run(capsys, _home(tmp_path, expires=expires), ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "fail"
    assert "expired" in session["detail"]
    assert session["fix"] == "run: spotify-uploader login"
    assert code == 2


def test_session_check_fails_when_the_file_is_missing(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, tmp_path / "empty-home", ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "fail"
    assert "no saved session" in session["detail"]
    assert code == 2


def test_session_check_fails_without_the_sp_dc_cookie(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, _home(tmp_path, cookie_name="sp_t"), ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "fail"
    assert "sp_dc" in session["detail"]
    assert code == 2


def test_session_check_fails_on_a_corrupt_file_without_raising(tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "storage_state.json").write_text("{not json", encoding="utf-8")

    code, payload, _, _ = _run(capsys, home, ("--offline",))

    session = _by_name(payload)["session"]
    assert session["status"] == "fail"
    assert "not valid JSON" in session["detail"]
    assert code == 2


# --- show id ---------------------------------------------------------------


def test_show_id_check_ok_from_the_flag(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",))

    show_id = _by_name(payload)["show-id"]
    assert show_id["status"] == "ok"
    assert SHOW_ID in show_id["detail"]
    assert code == 0


def test_show_id_check_fails_when_not_configured(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline",), show_id=None)

    show_id = _by_name(payload)["show-id"]
    assert show_id["status"] == "fail"
    assert "no show id" in show_id["detail"]
    assert show_id["fix"] == "spotify-uploader shows --select ID_OR_NAME or --show-id ID"
    assert code == 2


def test_show_id_check_reports_a_broken_stored_config_instead_of_raising(tmp_path, capsys) -> None:
    home = _home(tmp_path)
    (home / "config.json").write_text(json.dumps({"show_id": "bad id!"}), encoding="utf-8")

    code, payload, _, _ = _run(capsys, home, ("--offline",), show_id=None)

    show_id = _by_name(payload)["show-id"]
    assert show_id["status"] == "fail"
    assert "invalid show id" in show_id["detail"]
    assert code == 2


# --- online checks ---------------------------------------------------------


def test_offline_skips_network_checks_and_never_builds_the_uploader(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--offline", "--rehearse"))

    checks = _by_name(payload)
    assert checks["signed-in"]["status"] == "skipped"
    assert checks["show"]["status"] == "skipped"
    assert checks["rehearsal"]["status"] == "skipped"
    assert "offline" in checks["signed-in"]["detail"]
    assert code == 0


def test_online_checks_are_skipped_when_the_session_is_missing(tmp_path, capsys) -> None:
    code, payload, _, _ = _run(capsys, tmp_path / "empty-home")

    checks = _by_name(payload)
    assert checks["signed-in"]["status"] == "skipped"
    assert checks["show"]["status"] == "skipped"
    assert "needs a saved session" in checks["signed-in"]["detail"]
    assert code == 2


def test_signed_in_and_show_checks_ok(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader())

    code, payload, _, _ = _run(capsys, _home(tmp_path))

    checks = _by_name(payload)
    assert checks["signed-in"]["status"] == "ok"
    assert SHOW_ID in checks["signed-in"]["detail"]
    assert checks["show"]["status"] == "ok"
    assert "Test show" in checks["show"]["detail"]
    assert code == 0


def test_signed_in_check_fails_when_the_session_expired(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(session_error=AuthExpiredError("signed out")))

    code, payload, _, _ = _run(capsys, _home(tmp_path))

    checks = _by_name(payload)
    assert checks["signed-in"]["status"] == "fail"
    assert checks["signed-in"]["fix"] == "run: spotify-uploader login"
    assert checks["show"]["status"] == "ok"
    assert code == 2


def test_signed_in_check_surfaces_upload_errors(tmp_path, capsys, monkeypatch) -> None:
    error = UploadError("could not open the episodes page: net::ERR_FAILED", step="open")
    _inject(monkeypatch, StubUploader(session_error=error))

    _, payload, _, _ = _run(capsys, _home(tmp_path))

    signed_in = _by_name(payload)["signed-in"]
    assert signed_in["status"] == "fail"
    assert "could not open the episodes page" in signed_in["detail"]


def test_show_check_fails_when_setup_is_incomplete(tmp_path, capsys, monkeypatch) -> None:
    show = ShowInfo(SHOW_ID, "Unfinished", setup_complete=False)
    _inject(monkeypatch, StubUploader(show=show))

    code, payload, _, _ = _run(capsys, _home(tmp_path))

    check = _by_name(payload)["show"]
    assert check["status"] == "fail"
    assert "Unfinished" in check["detail"]
    assert "finish the show's setup" in check["fix"]
    assert code == 2


def test_show_check_warns_when_setup_is_unknown(tmp_path, capsys, monkeypatch) -> None:
    show = ShowInfo(SHOW_ID, "Maybe ready", setup_complete=None)
    _inject(monkeypatch, StubUploader(show=show))

    code, payload, _, _ = _run(capsys, _home(tmp_path))

    check = _by_name(payload)["show"]
    assert check["status"] == "warn"
    assert "could not tell" in check["detail"]
    assert code == 0


def test_show_check_warns_when_inspection_is_unavailable(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(show_error=NotImplementedError("owned elsewhere")))

    code, payload, _, _ = _run(capsys, _home(tmp_path))

    check = _by_name(payload)["show"]
    assert check["status"] == "warn"
    assert "cannot read the show dashboard" in check["detail"]
    assert code == 0


def test_show_check_fails_when_the_session_expired(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(show_error=AuthExpiredError("signed out")))

    _, payload, _, _ = _run(capsys, _home(tmp_path))

    check = _by_name(payload)["show"]
    assert check["status"] == "fail"
    assert check["fix"] == "run: spotify-uploader login"


# --- rehearsal -------------------------------------------------------------


def test_rehearsal_check_is_absent_unless_requested(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader())

    _, payload, _, _ = _run(capsys, _home(tmp_path))

    assert "rehearsal" not in _by_name(payload)


def test_rehearsal_is_skipped_after_an_online_failure(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(session_error=AuthExpiredError("signed out")))

    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--rehearse", "--video", "x.mp4"))

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "skipped"
    assert "earlier online check failed" in rehearsal["detail"]
    assert code == 2


def test_rehearsal_generates_a_clip_with_bounded_timeouts(tmp_path, capsys, monkeypatch) -> None:
    home = _home(tmp_path)
    workdir = tmp_path / "work"
    generated = workdir / "doctor-rehearsal.mp4"
    monkeypatch.setattr(doctor, "_make_workdir", lambda: workdir)

    def fake_clip(ffmpeg: str, target_dir: Path) -> Path:
        assert ffmpeg == "/usr/bin/ffmpeg"
        target_dir.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(b"clip")
        return generated

    monkeypatch.setattr(doctor, "_generate_test_clip", fake_clip)
    stub = StubUploader()
    configs: list[doctor.UploaderConfig] = []

    def factory(config: doctor.UploaderConfig) -> StubUploader:
        configs.append(config)
        return stub

    monkeypatch.setattr(doctor, "_make_uploader", factory)

    code, payload, _, _ = _run(capsys, home, ("--rehearse",))

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "ok"
    assert "DRAFT" in rehearsal["detail"]
    assert "delete" in rehearsal["detail"]
    assert code == 0
    assert len(stub.publish_calls) == 1
    call = stub.publish_calls[0]
    request = call["request"]
    assert isinstance(request, EpisodeRequest)
    assert request.video_path == generated
    assert request.title == TITLE
    assert request.thumbnail_path is None
    assert call["dry_run"] is True
    assert call["force"] is True
    assert len(configs) == 2
    assert configs[1].debug_dir == workdir / "debug"
    assert configs[1].upload_timeout_s == doctor._REHEARSAL_UPLOAD_TIMEOUT_S
    assert 0 < configs[1].upload_timeout_s <= 600
    assert not workdir.exists()


def test_rehearsal_passes_the_thumbnail_through(tmp_path, capsys, monkeypatch) -> None:
    home = _home(tmp_path)
    video = _tiny_video(tmp_path)
    thumbnail = tmp_path / "thumb.png"
    thumbnail.write_bytes(b"png")
    stub = StubUploader()
    _inject(monkeypatch, stub)

    code, payload, _, _ = _run(
        capsys,
        home,
        ("--rehearse", "--video", str(video), "--thumbnail", str(thumbnail)),
    )

    assert code == 0
    assert _by_name(payload)["rehearsal"]["status"] == "ok"
    assert stub.publish_calls[0]["request"].thumbnail_path == thumbnail


def test_rehearsal_is_skipped_without_ffmpeg_or_video(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    stub = StubUploader()
    _inject(monkeypatch, stub)

    code, payload, _, _ = _run(capsys, _home(tmp_path), ("--rehearse",))

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "skipped"
    assert "ffmpeg is not on PATH" in rehearsal["detail"]
    assert stub.publish_calls == []
    assert code == 0


@pytest.mark.parametrize(
    ("step", "phrase"),
    [
        ("open", "did not load"),
        ("upload", "file input"),
        ("title", "title field"),
        ("description", "rich-text"),
        ("content-check", "explicit-content"),
        ("next", "Next button"),
        ("publish-now", "Now/Schedule"),
        ("thumbnail", "thumbnail"),
        ("publish", "somewhere unexpected"),
        (None, "somewhere unexpected"),
    ],
)
def test_rehearsal_maps_wizard_steps_to_human_sentences(
    tmp_path, capsys, monkeypatch, step, phrase
) -> None:
    error = UploadError("selector miss", step=step)
    _inject(monkeypatch, StubUploader(publish_error=error))

    code, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "fail"
    assert phrase in rehearsal["detail"]
    assert "pip install -U spotify-creators-uploader" in rehearsal["fix"]
    assert code == 2


def test_rehearsal_includes_the_artifact_path(tmp_path, capsys, monkeypatch) -> None:
    artifacts = tmp_path / "artifacts"
    error = UploadError("selector miss", step="upload", artifacts=artifacts)
    _inject(monkeypatch, StubUploader(publish_error=error))
    monkeypatch.setattr(doctor, "_make_workdir", lambda: tmp_path / "work")

    _, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert f"artifacts: {artifacts}" in rehearsal["detail"]


def test_rehearsal_reports_show_setup_errors(tmp_path, capsys, monkeypatch) -> None:
    error = ShowSetupError("Spotify wants the show set up first", draft_episode_id="draft-1")
    _inject(monkeypatch, StubUploader(publish_error=error))

    code, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "fail"
    assert "Spotify wants the show set up first" in rehearsal["detail"]
    assert "finish the show's setup" in rehearsal["fix"]
    assert code == 2


def test_rehearsal_reports_an_expired_session(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(publish_error=AuthExpiredError("signed out")))

    _, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "fail"
    assert rehearsal["fix"] == "run: spotify-uploader login"


def test_rehearsal_reports_invalid_requests(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(publish_error=ValidationError("video file not found")))

    _, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "fail"
    assert "video file not found" in rehearsal["detail"]
    assert ".mp4" in rehearsal["fix"]


def test_rehearsal_reports_an_unexpected_crash(tmp_path, capsys, monkeypatch) -> None:
    _inject(monkeypatch, StubUploader(publish_error=RuntimeError("boom")))

    _, payload, _, _ = _run(
        capsys, _home(tmp_path), ("--rehearse", "--video", str(_tiny_video(tmp_path)))
    )

    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "fail"
    assert "the rehearsal crashed: boom" in rehearsal["detail"]


# --- rehearsal against the fake site --------------------------------------


def _fake_factory(holder: dict[str, FakeSpotify], behavior: FakeBehavior) -> Any:
    async def setup(context: BrowserContext) -> None:
        holder["fake"] = await install_fake_spotify(context, SHOW_ID, behavior)

    def factory(config: doctor.UploaderConfig) -> SpotifyCreatorsUploader:
        uploader = SpotifyCreatorsUploader(config, context_setup=setup)

        async def check_session() -> str:
            return SHOW_ID

        async def inspect_show() -> ShowInfo:
            return ShowInfo(SHOW_ID, "Rehearsal show", True)

        uploader.check_session = check_session  # type: ignore[method-assign]
        uploader.inspect_show = inspect_show  # type: ignore[method-assign]
        return uploader

    return factory


def test_rehearsal_happy_path_against_the_fake(
    tmp_path, capsys, monkeypatch, require_chromium
) -> None:
    home = _home(tmp_path)
    video = _tiny_video(tmp_path)
    monkeypatch.setattr(doctor, "_make_workdir", lambda: tmp_path / "success-work")
    holder: dict[str, FakeSpotify] = {}
    monkeypatch.setattr(doctor, "_make_uploader", _fake_factory(holder, FakeBehavior()))

    code, payload, out, err = _run(capsys, home, ("--rehearse", "--video", str(video)))

    assert code == 0, out + err
    rehearsal = _by_name(payload)["rehearsal"]
    assert rehearsal["status"] == "ok"
    assert "DRAFT" in rehearsal["detail"]
    fake = holder["fake"]
    event_types = [event["type"] for event in fake.events]
    assert "wizard_opened" in event_types
    assert "next_clicked" in event_types
    assert "published" not in event_types
    assert fake.published == []


def test_rehearsal_names_the_description_step_against_the_fake(
    tmp_path, capsys, monkeypatch, require_chromium
) -> None:
    home = _home(tmp_path)
    video = _tiny_video(tmp_path)
    workdir = tmp_path / "failed-work"
    monkeypatch.setattr(doctor, "_make_workdir", lambda: workdir)
    holder: dict[str, FakeSpotify] = {}
    behavior = FakeBehavior(description_mode="broken")
    monkeypatch.setattr(doctor, "_make_uploader", _fake_factory(holder, behavior))

    code, payload, _, _ = _run(capsys, home, ("--rehearse", "--video", str(video)))

    rehearsal = _by_name(payload)["rehearsal"]
    assert code == 2
    assert rehearsal["status"] == "fail"
    assert "step 'description'" in rehearsal["detail"]
    assert "rich-text description editor" in rehearsal["detail"]
    assert "pip install -U spotify-creators-uploader" in rehearsal["fix"]
    assert str(workdir) in rehearsal["detail"]
    assert (workdir / "debug").is_dir()


# --- rendering and exit codes ---------------------------------------------


def test_json_payload_shape_and_check_order(tmp_path, capsys) -> None:
    code, payload, out, err = _run(capsys, _home(tmp_path), ("--offline",))

    assert code == 0
    assert err == ""
    assert out.count("\n") == 1
    assert set(payload) == {"ok", "checks"}
    assert payload["ok"] is True
    assert [check["name"] for check in payload["checks"]] == [
        "python",
        "browser",
        "ffmpeg",
        "session",
        "show-id",
        "signed-in",
        "show",
    ]
    for check in payload["checks"]:
        assert set(check) == {"name", "status", "detail", "fix"}
        assert check["status"] in {"ok", "warn", "fail", "skipped"}
        assert check["fix"] is None or isinstance(check["fix"], str)


def test_human_output_has_labels_fixes_and_a_summary(tmp_path, capsys) -> None:
    code, _, out, err = _run(capsys, tmp_path / "empty-home", json_mode=False, show_id=None)

    assert code == 2
    assert err == ""
    lines = out.splitlines()
    assert "[FAIL] session: no saved session" in out
    assert "  fix: run: spotify-uploader login" in lines
    assert "[FAIL] show-id: no show id configured" in out
    assert "[skip] signed-in: needs a saved session" in out
    assert "[skip] show: needs a saved session" in out
    assert lines[-1].startswith("doctor:")
    assert "failure(s)" in lines[-1]


def test_exit_code_is_zero_with_only_warnings_and_skips(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_which", lambda name: None)

    code, _, out, _ = _run(capsys, _home(tmp_path), ("--offline",), json_mode=False)

    assert code == 0
    assert "[warn] ffmpeg:" in out
    assert "[skip] signed-in:" in out


def test_cookie_value_is_never_rendered_in_either_mode(tmp_path, capsys) -> None:
    home = _home(tmp_path)

    json_code, _, json_out, json_err = _run(capsys, home, ("--offline",))
    human_code, _, human_out, human_err = _run(capsys, home, ("--offline",), json_mode=False)

    assert json_code == 0
    assert human_code == 0
    for text in (json_out, json_err, human_out, human_err):
        assert SECRET not in text
