"""Read-only discovery of standard Moodle file/folder activities and section pages."""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .errors import AuthenticationExpired, SyncError
from .http import safe_url
from .moodle import Course


@dataclass(frozen=True)
class Resource:
    key: str
    course: Course
    section: str
    url: str
    filename: str
    revision: str | None


def file_resource(url: str, course: Course, section: str) -> Resource | None:
    try:
        url = safe_url(url)
    except SyncError:
        return None
    parsed = urlsplit(url)
    # Standard file and folder activities. Arbitrary plugin/private submission URLs are not crawled.
    match = re.fullmatch(
        r"/pluginfile\.php/(\d+)/(mod_resource|mod_folder)/content/(\d+)/(.*)", parsed.path
    )
    if not match or not match[4]:
        return None
    context, component, revision, path = match.groups()
    # Revision and forcedownload are representation details, not logical resource identity.
    identity = f"{course.id}/{context}/{component}/{unquote(path)}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return Resource(key, course, section, clean_url, unquote(path.rsplit("/", 1)[-1]), revision)


def page_links(html: str, base: str, course: Course, default_section: str = "General"):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('input[type="password"], body.notloggedin, body.guestuser'):
        raise AuthenticationExpired()
    if soup.select_one('[data-region="error"], .errorbox, #errorbox'):
        raise SyncError("Moodle returned an error page instead of course content.")
    root = soup.select_one("#region-main") or soup
    files, pages = {}, {}
    for tag in root.select("a[href], object[data], embed[src], iframe[src]"):
        raw = tag.get("href") or tag.get("data") or tag.get("src")
        try:
            url = safe_url(raw, base)
        except SyncError:
            continue
        parent = tag.find_parent(id=re.compile(r"^section-\d+$"))
        if parent is None:
            parent = tag.find_parent(attrs={"data-sectionid": True})
        heading = parent.select_one(".sectionname, h2, h3") if parent else None
        section = heading.get_text(" ", strip=True) if heading else default_section
        resource = file_resource(url, course, section)
        if resource:
            files[resource.key] = resource
            continue
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        # Only follow read-only activity and section views, never enrol/logout/action links.
        if set(query) - {"id", "section", "sectionid"}:
            continue
        if parsed.path in ("/mod/resource/view.php", "/mod/folder/view.php"):
            if query.get("id", [""])[0].isdigit():
                pages[url] = section
        elif parsed.path == "/course/view.php":
            if query.get("id") == [str(course.id)] and ("section" in query or "sectionid" in query):
                pages[url] = section
        elif parsed.path == "/course/section.php" and query.get("id", [""])[0].isdigit():
            pages[url] = section
    return list(files.values()), pages


class ResourceDiscovery:
    def __init__(self, transport):
        self.transport = transport

    def discover(self, course: Course) -> list[Resource]:
        pending = {safe_url(f"/course/view.php?id={course.id}"): "General"}
        seen, resources = set(), {}
        while pending:
            url, section = next(iter(pending.items()))
            del pending[url]
            if url in seen:
                continue
            if len(seen) >= 2000:
                raise SyncError("Course traversal limit reached; discovery is incomplete.")
            seen.add(url)
            with self.transport.stream("GET", url) as response:
                # A resource activity can redirect directly to a potentially large file.
                resource = file_resource(str(response.url), course, section)
                if resource:
                    resources[resource.key] = resource
                    continue  # Close without reading the body; sync handles the download.
                if "text/html" not in response.headers.get("content-type", "").lower():
                    raise SyncError(
                        "Unsupported activity response: expected an HTML page or Moodle file."
                    )
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise SyncError("Moodle HTML page exceeds the 10 MiB safety limit.")
                    chunks.append(chunk)
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                files, links = page_links(html, str(response.url), course, section)
                resources.update({r.key: r for r in files})
                for link, topic in links.items():
                    if link not in seen:
                        pending[link] = topic
                if "/mod/resource/view.php" in url and not files and not links:
                    raise SyncError(
                        "Could not resolve a file activity; Moodle markup may have changed."
                    )
        return list(resources.values())
