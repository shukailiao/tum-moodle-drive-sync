"""Real headless Chromium, synthetic browser login; never contacts TUM/SSO.

Set TUM_SYNC_TEST_CHROMIUM to a Chromium executable, or install `chromium` on PATH.
The visible/manual production login is substituted only for this local fixture.
"""

import os
import shutil
from types import SimpleNamespace

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tum_moodle_drive_sync import auth
from tum_moodle_drive_sync.config import Config
from tum_moodle_drive_sync.http import Transport
from tum_moodle_drive_sync.security import SessionStore


@pytest.mark.browser
def test_real_browser_cookie_handoff(tmp_path, monkeypatch):
    executable = os.environ.get("TUM_SYNC_TEST_CHROMIUM") or shutil.which("chromium")
    if not executable:
        pytest.skip("Optional browser fixture requires a Chromium executable")
    state = tmp_path / "state"
    store = SessionStore(state)
    cfg = Config(tmp_path / "drive", state)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=executable)
        context = browser.new_context()
        context.route(
            "**/*",
            lambda route: route.fulfill(
                content_type="text/html", body="<html><title>Synthetic Moodle login</title></html>"
            ),
        )
        proxy_browser = SimpleNamespace(new_context=lambda **kw: context, close=browser.close)

        class PlaywrightFixture:
            def __enter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: proxy_browser))

            def __exit__(self, *args):
                pass

        monkeypatch.setattr(auth, "sync_playwright", PlaywrightFixture)

        def complete_login(prompt):
            assert context.pages[0].title() == "Synthetic Moodle login"
            context.add_cookies(
                [
                    {
                        "name": "MoodleSession",
                        "value": "fixture",
                        "domain": "www.moodle.tum.de",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "SSO",
                        "value": "do-not-save",
                        "domain": "login.tum.de",
                        "path": "/",
                        "secure": True,
                    },
                ]
            )
            return ""

        monkeypatch.setattr("builtins.input", complete_login)

        def handle(request):
            assert request.headers["cookie"] == "MoodleSession=fixture"
            return httpx.Response(
                200,
                text='<script>{"sesskey":"fixture"}</script><a href="/login/logout.php">Logout</a>',
            )

        monkeypatch.setattr(
            auth,
            "Transport",
            lambda cookies, **kw: Transport(
                cookies, interval=0, transport=httpx.MockTransport(handle)
            ),
        )
        auth.login(cfg, store)
    assert [c["name"] for c in store.load()] == ["MoodleSession"]
