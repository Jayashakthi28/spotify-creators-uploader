"""Login helper tests: pure selection/polling logic and a fully faked interactive flow.

No real Chrome, Playwright browser or network connection is ever started here.
"""

from __future__ import annotations

import stat
import urllib.error
from typing import Any

import pytest

from spotify_uploader import login, selectors
from spotify_uploader.config import UploaderConfig, read_json_object
from spotify_uploader.errors import ConfigurationError

SECRET = "super-secret-cookie-value"


def _sp_dc(**overrides: Any) -> dict[str, Any]:
    cookie: dict[str, Any] = {
        "name": "sp_dc",
        "value": SECRET,
        "domain": ".spotify.com",
        "path": "/",
        "expires": -1.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    cookie.update(overrides)
    return cookie


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self.goto_urls: list[str] = []

    async def goto(self, url: str) -> None:
        self.goto_urls.append(url)
        self.url = url


class FakeContext:
    def __init__(
        self,
        *,
        cookies: list[dict[str, Any]] | None = None,
        pages: list[FakePage] | None = None,
    ) -> None:
        self._cookies = list(cookies or [])
        self._pages = list(pages or [])
        self.created_pages: list[FakePage] = []

    @property
    def pages(self) -> list[FakePage]:
        return list(self._pages)

    async def cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies)

    async def storage_state(self) -> dict[str, Any]:
        return {"cookies": list(self._cookies), "origins": []}

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.created_pages.append(page)
        self._pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.contexts = [context]
        self._context = context
        self.closed = False

    async def new_context(self) -> FakeContext:
        return self._context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.cdp_url: str | None = None
        self.launch_kwargs: dict[str, Any] | None = None

    async def connect_over_cdp(self, url: str) -> FakeBrowser:
        self.cdp_url = url
        return self.browser

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self.launch_kwargs = kwargs
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


class FakePlaywrightManager:
    def __init__(self, playwright: FakePlaywright) -> None:
        self._playwright = playwright

    async def __aenter__(self) -> FakePlaywright:
        return self._playwright

    async def __aexit__(self, *args: object) -> bool:
        return False


class FakeProcess:
    def __init__(self) -> None:
        self.args: list[str] = []
        self.kwargs: dict[str, Any] = {}
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


def _fake_playwright(monkeypatch, context: FakeContext) -> tuple[FakeChromium, FakeBrowser]:
    browser = FakeBrowser(context)
    chromium = FakeChromium(browser)
    playwright = FakePlaywright(chromium)
    monkeypatch.setattr(login, "async_playwright", lambda: FakePlaywrightManager(playwright))
    return chromium, browser


def test_find_chrome_env_override(tmp_path, monkeypatch) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_text("", encoding="utf-8")
    chrome.chmod(0o755)
    monkeypatch.setenv(login.CHROME_ENV, str(chrome))

    assert login.find_chrome() == chrome
    assert login.find_chrome(candidates=[]) == chrome


def test_find_chrome_env_override_must_exist(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(login.CHROME_ENV, str(tmp_path / "missing"))

    assert login.find_chrome() is None


def test_find_chrome_uses_candidates(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(login.CHROME_ENV, raising=False)
    missing = tmp_path / "missing"
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("", encoding="utf-8")
    not_executable.chmod(0o644)
    executable = tmp_path / "chrome"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)

    assert login.find_chrome(candidates=[missing, not_executable, executable]) == executable
    assert login.find_chrome(candidates=[missing, not_executable]) is None


def test_find_chrome_without_candidates_does_not_require_a_browser(monkeypatch) -> None:
    monkeypatch.delenv(login.CHROME_ENV, raising=False)
    # The return value depends on the host; only the type contract is asserted.
    found = login.find_chrome()
    assert found is None or found.is_file()


def test_build_chrome_args(tmp_path) -> None:
    chrome = tmp_path / "Google Chrome"
    profile = tmp_path / "profile"

    args = login.build_chrome_args(chrome, profile, 9223)

    assert args == [
        str(chrome),
        "--remote-debugging-port=9223",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        selectors.BASE_URL,
    ]


async def test_wait_for_cdp_probes_version_endpoint() -> None:
    seen: list[str] = []

    def fetch(url: str) -> object:
        seen.append(url)
        return True

    await login.wait_for_cdp(9223, 1.0, fetch=fetch)

    assert seen == ["http://127.0.0.1:9223/json/version"]


async def test_wait_for_cdp_retries_until_ready() -> None:
    calls = 0

    def flaky(url: str) -> object:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise urllib.error.URLError("not listening yet")
        return True

    await login.wait_for_cdp(9223, 2.0, fetch=flaky)

    assert calls == 3


async def test_wait_for_cdp_accepts_async_fetch() -> None:
    async def fetch(url: str) -> object:
        return True

    await login.wait_for_cdp(9223, 1.0, fetch=fetch)


async def test_wait_for_cdp_treats_false_as_not_ready() -> None:
    calls = 0

    def fetch(url: str) -> object:
        nonlocal calls
        calls += 1
        return calls >= 2

    await login.wait_for_cdp(9223, 1.0, fetch=fetch)

    assert calls == 2


async def test_wait_for_cdp_times_out() -> None:
    def never(url: str) -> object:
        raise urllib.error.URLError("refused")

    with pytest.raises(ConfigurationError, match="debugging port 9223"):
        await login.wait_for_cdp(9223, 0.02, fetch=never)


@pytest.mark.parametrize(
    ("cookies", "urls", "expected"),
    [
        ([_sp_dc()], ["https://creators.spotify.com/pod/show/x/episodes"], True),
        ([_sp_dc()], ["about:blank", "https://creators.spotify.com/pod/show/x"], True),
        ([_sp_dc()], ["https://accounts.spotify.com/en/login"], False),
        ([_sp_dc()], ["https://creators.spotify.com/login"], False),
        ([_sp_dc(domain="evil.com")], ["https://creators.spotify.com/pod/show/x"], False),
        ([_sp_dc(value="")], ["https://creators.spotify.com/pod/show/x"], False),
        ([], ["https://creators.spotify.com/pod/show/x"], False),
        ([_sp_dc()], ["https://example.com/pod/show/x"], False),
    ],
)
def test_is_signed_in(cookies, urls, expected) -> None:
    assert login.is_signed_in(cookies, urls) is expected


async def test_interactive_login_chrome_attaches_and_terminates_only_its_process(
    tmp_path, monkeypatch
) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_text("", encoding="utf-8")
    chrome.chmod(0o755)
    monkeypatch.setenv(login.CHROME_ENV, str(chrome))

    started: list[FakeProcess] = []

    def fake_popen(args, **kwargs):
        process = FakeProcess()
        process.args = args
        process.kwargs = kwargs
        started.append(process)
        return process

    async def fake_wait_for_cdp(port: int, timeout_s: float, *, fetch=None) -> None:
        del port, timeout_s, fetch

    monkeypatch.setattr(login.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(login, "wait_for_cdp", fake_wait_for_cdp)

    context = FakeContext(
        cookies=[_sp_dc()],
        pages=[FakePage("https://creators.spotify.com/pod/show/found_show/episodes")],
    )
    chromium, browser = _fake_playwright(monkeypatch, context)
    unrelated = FakeProcess()
    config = UploaderConfig(home=tmp_path / "home")
    statuses: list[str] = []

    result = await login.interactive_login(
        config,
        mode="chrome",
        timeout_s=1.0,
        cdp_port=9333,
        status=statuses.append,
    )

    assert result.show_id == "found_show"
    assert result.state_path == config.storage_state_path
    assert result.summary.has_sp_dc is True
    assert result.summary.sp_dc_expires_at is None
    assert chromium.cdp_url == "http://127.0.0.1:9333"
    assert len(started) == 1
    assert started[0].terminated is True
    assert started[0].waits == [10]
    assert unrelated.terminated is False
    assert browser.closed is False
    assert started[0].args[0] == str(chrome)
    assert "--remote-debugging-port=9333" in started[0].args
    assert f"--user-data-dir={config.chrome_profile_dir}" in started[0].args
    assert started[0].args[-1] == selectors.BASE_URL
    assert stat.S_IMODE(config.storage_state_path.stat().st_mode) == 0o600
    assert read_json_object(config.config_path)["show_id"] == "found_show"
    assert SECRET not in " ".join(statuses)


async def test_interactive_login_chrome_timeout_still_terminates_the_process(
    tmp_path, monkeypatch
) -> None:
    chrome = tmp_path / "chrome"
    chrome.write_text("", encoding="utf-8")
    chrome.chmod(0o755)
    monkeypatch.setenv(login.CHROME_ENV, str(chrome))

    started: list[FakeProcess] = []

    def fake_popen(args, **kwargs):
        process = FakeProcess()
        process.args = args
        started.append(process)
        return process

    async def fake_wait_for_cdp(port: int, timeout_s: float, *, fetch=None) -> None:
        del port, timeout_s, fetch

    monkeypatch.setattr(login.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(login, "wait_for_cdp", fake_wait_for_cdp)
    context = FakeContext(cookies=[], pages=[])
    _fake_playwright(monkeypatch, context)
    config = UploaderConfig(home=tmp_path / "home")

    with pytest.raises(ConfigurationError, match="login timed out"):
        await login.interactive_login(config, mode="chrome", timeout_s=0.0)

    assert started[0].terminated is True
    assert not config.storage_state_path.exists()


async def test_interactive_login_chrome_missing_executable(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(login.CHROME_ENV, raising=False)
    monkeypatch.setattr(login, "find_chrome", lambda: None)
    monkeypatch.setattr(
        login.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("must not start Chrome without an executable"),
    )

    with pytest.raises(ConfigurationError, match="Chrome was not found"):
        await login.interactive_login(UploaderConfig(home=tmp_path / "home"), mode="chrome")


async def test_auto_falls_back_to_playwright_and_closes_the_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(login.CHROME_ENV, raising=False)
    monkeypatch.setattr(login, "find_chrome", lambda: None)
    monkeypatch.setattr(
        login.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("auto fell back to playwright, not Chrome"),
    )
    monkeypatch.setattr(login, "SHOW_ID_TIMEOUT_S", 0.01)
    context = FakeContext(cookies=[_sp_dc()])
    chromium, browser = _fake_playwright(monkeypatch, context)
    config = UploaderConfig(home=tmp_path / "home", channel="chrome")

    result = await login.interactive_login(config, mode="auto", timeout_s=0.2)

    assert result.show_id is None
    assert result.summary.has_sp_dc is True
    assert chromium.launch_kwargs == {"headless": False, "channel": "chrome"}
    assert browser.closed is True
    assert context.created_pages[0].goto_urls == [selectors.BASE_URL]
    assert config.storage_state_path.exists()
