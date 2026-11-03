import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .errors import AuthenticationExpired, SyncError


@dataclass(frozen=True)
class Course:
    id: int
    name: str


def session_key(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('input[type="password"], body.notloggedin, body.guestuser'):
        raise AuthenticationExpired()
    match = re.search(r'"sesskey"\s*:\s*"([A-Za-z0-9]+)"', html)
    # A guest page can also have a sesskey: require an authenticated user menu.
    if not match or not soup.select_one('a[href*="/login/logout.php"]'):
        raise AuthenticationExpired()
    return match.group(1)


class Moodle:
    def __init__(self, transport):
        self.transport = transport
        self.key = None

    def check_session(self):
        response = self.transport.request("GET", "/my/")
        self.key = session_key(response.text)

    def courses(self) -> list[Course]:
        self.check_session()
        courses = {}
        offset = 0
        for _ in range(1000):
            response = self.transport.request(
                "POST",
                "/lib/ajax/service.php",
                params={"sesskey": self.key},
                json=[
                    {
                        "index": 0,
                        "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                        "args": {
                            "classification": "all",
                            "limit": 100,
                            "offset": offset,
                            "sort": "fullname",
                        },
                    }
                ],
            )
            try:
                result = response.json()
                item = result[0]
                if item.get("error"):
                    detail = item.get("exception") or item.get("error")
                    code = detail.get("errorcode", "") if isinstance(detail, dict) else ""
                    if code in ("invalidsesskey", "servicerequireslogin", "requireloginerror"):
                        raise AuthenticationExpired()
                    raise SyncError(
                        "Moodle course AJAX is unavailable for this session/site. "
                        "No REST token fallback is attempted."
                    )
                data = item["data"]
                batch = data["courses"]
                if not isinstance(batch, list):
                    raise ValueError()
                for c in batch:
                    cid = int(c["id"])
                    if cid > 1:
                        courses[cid] = Course(
                            cid, BeautifulSoup(c["fullname"], "html.parser").get_text()
                        )
                next_offset = data.get("nextoffset", offset + len(batch))
                if not batch or next_offset == 0:
                    return list(courses.values())
                if not isinstance(next_offset, int) or next_offset <= offset:
                    raise SyncError("Moodle course pagination did not advance.")
                offset = next_offset
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                # HTML may be a login form returned with HTTP 200.
                if "text/html" in response.headers.get("content-type", ""):
                    raise AuthenticationExpired() from exc
                raise SyncError(
                    "Unrecognized Moodle course response; site compatibility needs review."
                ) from exc
        raise SyncError("Course discovery pagination limit reached; results are incomplete.")
