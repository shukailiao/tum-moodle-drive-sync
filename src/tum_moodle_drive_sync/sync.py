import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .errors import AuthenticationExpired, SyncError
from .files import disposition_name, inside, publish, sha256
from .resources import ResourceDiscovery

log = logging.getLogger(__name__)


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    failed: bool = False
    auth_expired: bool = False

    def add(self, status, course, name):
        # Prevent terminal escape/control injection from remotely supplied names.
        def printable(value):
            return "".join(c if c.isprintable() else " " for c in str(value))

        line = f"{status:14} | {printable(course)} | {printable(name)}"
        self.lines.append(line)
        log.info(line)

    def text(self):
        return "\n".join(self.lines) or "No matching downloadable file/folder resources found."


def sync_file(resource, config, transport, db, *, force=False):
    old = db.get(resource.key)
    local = Path(old["path"]) if old else None
    present = bool(local and inside(config.output_dir, local) and local.is_file())
    intact = present and sha256(local) == old["sha256"]
    fresh = old and time.time() - old["checked_at"] < config.revalidate_hours * 3600
    same_version = (
        old and old["source_url"] == resource.url and old["revision"] == resource.revision
    )
    if present and same_version and fresh and not force:
        return "UNCHANGED" if intact else "LOCAL EDIT"
    headers = {}
    if intact and same_version:
        if old["etag"]:
            headers["If-None-Match"] = old["etag"]
        elif old["last_modified"]:
            headers["If-Modified-Since"] = old["last_modified"]
    with transport.stream("GET", resource.url, headers=headers) as response:
        if response.status_code == 304:
            if not intact or not headers:
                raise SyncError("Moodle returned 304 without a usable local file.")
            db.record(
                resource,
                local,
                old["sha256"],
                response.headers.get("etag", old["etag"]),
                response.headers.get("last-modified", old["last_modified"]),
                "UNCHANGED",
                downloaded=False,
            )
            return "UNCHANGED"
        if response.status_code != 200:
            raise SyncError("Unexpected download response; no file saved.")
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind in ("text/html", "application/xhtml+xml", "application/json"):
            raise SyncError(
                "Moodle returned HTML/JSON instead of a file; check login or access. "
                "HTML course files are not supported by this MVP."
            )
        limit = config.max_file_mb * 1024 * 1024
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            raise SyncError(
                "File exceeds max_file_mb; increase the configured limit if appropriate."
            )
        digest, total = hashlib.sha256(), 0
        # Staging is private, outside the cloud-synchronized directory.
        with tempfile.NamedTemporaryFile(
            dir=config.state_dir, suffix=".part", delete=False
        ) as temp:
            temporary = Path(temp.name)
            try:
                for chunk in response.iter_bytes(1024 * 1024):
                    if total == 0:
                        beginning = chunk[:1024].lstrip().lower()
                        if beginning.startswith((b"<!doctype html", b"<html")):
                            raise SyncError("Received an HTML page disguised as a download.")
                    total += len(chunk)
                    if total > limit:
                        raise SyncError("Download exceeded max_file_mb; incomplete file discarded.")
                    temp.write(chunk)
                    digest.update(chunk)
                temp.flush()
                temp.close()  # Windows requires a closed temporary file before re-opening it.
                value = digest.hexdigest()
                if intact and value == old["sha256"]:
                    path, status = local, "UNCHANGED"
                else:
                    filename = disposition_name(
                        response.headers.get("content-disposition"), resource.filename
                    )
                    path = publish(temporary, config.output_dir, resource, filename, value)
                    status = "NEW" if not old else "UPDATED" if present else "RESTORED"
                db.record(
                    resource,
                    path,
                    value,
                    response.headers.get("etag"),
                    response.headers.get("last-modified"),
                    status,
                )
                return status
            finally:
                temp.close()
                temporary.unlink(missing_ok=True)


def synchronize(config, moodle, db, *, force=False):
    report = Report()
    if not config.course_ids:
        raise SyncError("No courses selected. Run courses, then set course_ids in config.toml.")
    courses = {c.id: c for c in moodle.courses()}
    missing = set(config.course_ids) - courses.keys()
    if missing:
        raise SyncError(
            "Selected course IDs are not in your enrolled course list: "
            + ", ".join(str(i) for i in sorted(missing))
        )
    for cid in config.course_ids:
        course = courses[cid]
        try:
            resources = ResourceDiscovery(moodle.transport).discover(course)
            if not resources:
                report.add("NO FILES", course.name, "No supported visible file/folder links")
            for resource in resources:
                try:
                    status = sync_file(resource, config, moodle.transport, db, force=force)
                    report.add(status, course.name, resource.filename)
                except AuthenticationExpired:
                    raise
                except (SyncError, OSError, httpx.HTTPError) as exc:
                    # HTTP exception strings can contain URLs/secrets. Only our errors are printable.
                    message = (
                        str(exc)
                        if isinstance(exc, SyncError)
                        else "File IO/network failure; retry later."
                    )
                    db.event(resource.key, course.name, "ERROR", message)
                    report.add("ERROR", course.name, resource.filename + ": " + message)
                    report.failed = True
        except AuthenticationExpired:
            report.add("LOGIN REQUIRED", course.name, "Run tum-moodle-sync login; sync stopped.")
            report.failed = report.auth_expired = True
            break
        except (SyncError, OSError, httpx.HTTPError) as exc:
            message = (
                str(exc)
                if isinstance(exc, SyncError)
                else "Course IO/network failure; retry later."
            )
            db.event(None, course.name, "ERROR", message)
            report.add("ERROR", course.name, message)
            report.failed = True
    return report
