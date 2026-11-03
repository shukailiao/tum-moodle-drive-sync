import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from .config import SITE
from .errors import AuthenticationExpired, SyncError


def safe_url(url: str, base: str = SITE) -> str:
    try:
        full = urljoin(base, url)
        parsed = urlsplit(full)
        port = parsed.port
    except (ValueError, TypeError) as exc:
        raise SyncError("Malformed Moodle resource URL.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.moodle.tum.de"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise SyncError("Refusing a request outside the TUM Moodle HTTPS origin.")
    if any(x in parsed.path.lower() for x in ("/login/", "/auth/", "/shibboleth.sso")):
        raise AuthenticationExpired()
    return full


class Transport:
    def __init__(self, cookies, *, interval=1.0, retries=3, timeout=60, transport=None):
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "tum-moodle-drive-sync/0.1"},
        )
        for cookie in cookies:
            if cookie.get("domain", "").lstrip(".") == "www.moodle.tum.de":
                self.client.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain="www.moodle.tum.de",
                    path=cookie.get("path", "/"),
                )
        self.interval, self.retries, self.last_request = interval, retries, 0.0

    def close(self):
        self.client.close()

    def export_cookies(self):
        return [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
                "expires": c.expires or -1,
            }
            for c in self.client.cookies.jar
        ]

    @contextmanager
    def stream(self, method, url, **kwargs):
        url = safe_url(url)
        redirects = attempts = 0
        while True:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.send(
                    self.client.build_request(method, url, **kwargs), stream=True
                )
            except httpx.TransportError as exc:
                if attempts >= self.retries:
                    raise SyncError(
                        "Moodle network request failed; check connection and retry."
                    ) from exc
                time.sleep(2**attempts)
                attempts += 1
                continue
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                response.close()
                if redirects >= 8 or not location:
                    raise SyncError("Invalid or excessive Moodle redirects.")
                target = urljoin(url, location)
                if urlsplit(target).hostname != "www.moodle.tum.de":
                    raise AuthenticationExpired()
                url = safe_url(target)
                redirects += 1
                if response.status_code == 303:
                    method, kwargs = "GET", {}
                continue
            if response.status_code in (401, 403):
                response.close()
                raise AuthenticationExpired()
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                wait = response.headers.get("retry-after", "")
                response.close()
                if attempts >= self.retries:
                    raise SyncError("Moodle is busy or rate-limiting requests; retry later.")
                try:
                    delay = float(wait)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(wait).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2**attempts
                if delay > 120:
                    raise SyncError("Moodle requested a long Retry-After; retry later.")
                time.sleep(max(0, delay))
                attempts += 1
                continue
            try:
                if response.status_code >= 400:
                    raise SyncError(f"Moodle returned HTTP {response.status_code}.")
                yield response
            finally:
                response.close()
            return

    def request(self, method, url, **kwargs):
        with self.stream(method, url, **kwargs) as response:
            response.read()
            return response
