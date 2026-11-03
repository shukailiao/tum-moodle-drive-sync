from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .config import SITE
from .errors import SyncError
from .http import Transport
from .moodle import Moodle


def login(config, store):
    """The user completes SSO/MFA in a fresh visible browser. Never read login fields."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, channel=config.browser_channel)
            try:
                context = browser.new_context(accept_downloads=False)
                page = context.new_page()
                page.goto(SITE + "/my/", wait_until="domcontentloaded", timeout=60000)
                input("Complete TUM login and MFA in the browser, then press Enter here: ")
                cookies = context.cookies(SITE)
                client = Transport(
                    cookies,
                    interval=config.request_interval,
                    retries=config.retries,
                    timeout=config.timeout,
                )
                try:
                    Moodle(client).check_session()
                    store.save(client.export_cookies())
                finally:
                    client.close()
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise SyncError(
            "Browser login failed. Install Chromium with `python -m playwright install "
            "chromium`, or configure browser_channel='msedge'/'chrome'. "
            "Run login in an interactive desktop session."
        ) from exc
