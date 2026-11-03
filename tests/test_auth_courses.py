import json
import os

import httpx
import pytest

from tum_moodle_drive_sync.errors import AuthenticationExpired, SyncError
from tum_moodle_drive_sync.http import Transport
from tum_moodle_drive_sync.moodle import Moodle, session_key
from tum_moodle_drive_sync.security import SessionStore

AUTH = '<script>M.cfg = {"sesskey":"testkey"};</script><a href="/login/logout.php?sesskey=testkey">Logout</a>'


def client(handler):
    return Transport([], interval=0, retries=0, transport=httpx.MockTransport(handler))


def test_session_requires_authenticated_marker():
    assert session_key(AUTH) == "testkey"
    for html in [
        '<script>{"sesskey":"guest"}</script>',
        '<input type="password">' + AUTH,
        '<body class="notloggedin">' + AUTH,
    ]:
        with pytest.raises(AuthenticationExpired):
            session_key(html)


def test_courses_pagination_and_deduplication():
    offsets = []

    def handle(r):
        if r.url.path == "/my/":
            return httpx.Response(200, text=AUTH)
        body = json.loads(r.content)[0]
        assert body["methodname"] == "core_course_get_enrolled_courses_by_timeline_classification"
        offset = body["args"]["offset"]
        offsets.append(offset)
        batch = [{"id": 2, "fullname": "Math &amp; Logic"}] if offset == 0 else []
        return httpx.Response(200, json=[{"data": {"courses": batch, "nextoffset": 1}}])

    t = client(handle)
    try:
        courses = Moodle(t).courses()
        assert [(c.id, c.name) for c in courses] == [(2, "Math & Logic")]
        assert offsets == [0, 1]
    finally:
        t.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://login.tum.de/idp"}),
        httpx.Response(200, text='<body class="notloggedin">Login</body>'),
        httpx.Response(403),
    ],
)
def test_expired_session(response):
    t = client(lambda r: response)
    with pytest.raises(AuthenticationExpired):
        Moodle(t).check_session()
    t.close()


def test_ajax_denied_is_not_empty_success():
    t = client(
        lambda r: (
            httpx.Response(200, text=AUTH)
            if r.url.path == "/my/"
            else httpx.Response(
                200, json=[{"error": True, "exception": {"errorcode": "notallowed"}}]
            )
        )
    )
    with pytest.raises(SyncError, match="unavailable"):
        Moodle(t).courses()
    t.close()


def test_session_store_filters_sso_cookies(tmp_path):
    store = SessionStore(tmp_path / "private")
    store.save(
        [
            {"domain": "www.moodle.tum.de", "name": "MoodleSession", "value": "fixture"},
            {"domain": "login.tum.de", "name": "SSO", "value": "discard"},
        ]
    )
    assert [c["name"] for c in store.load()] == ["MoodleSession"]
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0
        assert store.path.parent.stat().st_mode & 0o077 == 0
    store.delete()
    with pytest.raises(AuthenticationExpired):
        store.load()
