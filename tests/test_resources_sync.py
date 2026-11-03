from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from tum_moodle_drive_sync.config import Config
from tum_moodle_drive_sync.database import Database
from tum_moodle_drive_sync.errors import SyncError
from tum_moodle_drive_sync.files import clean_name, disposition_name, publish
from tum_moodle_drive_sync.http import Transport
from tum_moodle_drive_sync.moodle import Course, Moodle
from tum_moodle_drive_sync.resources import ResourceDiscovery, file_resource, page_links
from tum_moodle_drive_sync.sync import sync_file, synchronize

AUTH = '<script>{"sesskey":"fixture"}</script><a href="/login/logout.php">Logout</a>'
FILE = "/pluginfile.php/12/mod_resource/content/1/lecture.pdf"
COURSE = Course(42, "Algorithms")


@pytest.fixture
def setup(tmp_path):
    state, output = tmp_path / "state", tmp_path / "drive"
    state.mkdir()
    output.mkdir()
    config = Config(output, state, (42,), request_interval=0.25)
    db = Database(state / "state.sqlite3")
    yield config, db
    db.close()


def transport(handler):
    return Transport([], interval=0, retries=0, transport=httpx.MockTransport(handler))


def test_revision_changes_preserve_identity():
    a = file_resource(FILE, COURSE, "Week 1")
    b = file_resource(
        FILE.replace("/content/1/", "/content/2/") + "?forcedownload=1", COURSE, "Renamed"
    )
    assert a.key == b.key
    assert a.revision != b.revision
    assert "?" not in b.url
    assert file_resource("https://evil.test" + FILE, COURSE, "x") is None
    assert file_resource("/pluginfile.php/1/assignsubmission_file/a.pdf", COURSE, "x") is None


def test_sections_folders_embedded_files_and_whitelist():
    html = """<main id="region-main"><li id="section-1"><h3 class="sectionname">Week 1</h3>
      <a href="/mod/folder/view.php?id=5">Folder</a>
      <a href="/course/view.php?id=42&amp;section=2">Week 2</a>
      <a href="/course/view.php?id=99&amp;section=2">Other course</a>
      <a href="/course/section.php?id=123">Section</a>
      <a href="/mod/folder/view.php?id=5&amp;delete=1">Delete</a>
      <object data="/pluginfile.php/12/mod_resource/content/1/a.pdf"></object>
      <a href="https://evil.test/pluginfile.php/12/mod_resource/content/1/b.pdf">External</a>
      </li></main>"""
    files, pages = page_links(html, "https://www.moodle.tum.de/course/view.php?id=42", COURSE)
    assert len(files) == 1 and files[0].section == "Week 1"
    assert len(pages) == 3
    assert all("delete" not in p and "id=99" not in p for p in pages)


def test_discovery_follows_folder_section_and_resource_redirect():
    paths = []

    def handle(r):
        paths.append(r.url.path)
        if r.url.path == "/course/view.php":
            content = '<div id="section-1"><h3>Week 1</h3><a href="/mod/resource/view.php?id=1">PDF</a><a href="/mod/folder/view.php?id=2">Folder</a></div>'
        elif r.url.path == "/mod/resource/view.php":
            return httpx.Response(303, headers={"location": FILE})
        elif r.url.path == FILE:
            return httpx.Response(
                200, content=b"%PDF-1 fixture", headers={"content-type": "application/pdf"}
            )
        else:
            content = '<a href="/pluginfile.php/9/mod_folder/content/2/sub/notes.docx">Notes</a>'
        return httpx.Response(200, text=content, headers={"content-type": "text/html"})

    t = transport(handle)
    try:
        found = ResourceDiscovery(t).discover(COURSE)
        assert sorted(r.filename for r in found) == ["lecture.pdf", "notes.docx"]
        assert all(r.section == "Week 1" for r in found)
        assert paths.count("/mod/folder/view.php") == 1
    finally:
        t.close()


def test_new_unchanged_updated_and_local_edits(setup):
    cfg, db = setup
    requests = []
    body = b"%PDF-1 version one"

    def handle(r):
        requests.append(r)
        return httpx.Response(
            200, content=body, headers={"etag": '"one"', "content-type": "application/pdf"}
        )

    t = transport(handle)
    resource = file_resource(FILE, COURSE, "Week 1")
    assert sync_file(resource, cfg, t, db) == "NEW"
    original = Path(db.get(resource.key)["path"])
    assert original.read_bytes() == body
    assert sync_file(resource, cfg, t, db) == "UNCHANGED"
    assert len(requests) == 1
    original.write_bytes(b"user annotations")
    assert sync_file(resource, cfg, t, db) == "LOCAL EDIT"
    body = b"%PDF-1 new version"
    changed = file_resource(FILE.replace("/content/1/", "/content/2/"), COURSE, "Week 1")
    assert sync_file(changed, cfg, t, db) == "UPDATED"
    updated = Path(db.get(resource.key)["path"])
    assert updated != original
    assert original.read_bytes() == b"user annotations"
    assert updated.read_bytes() == body
    assert db.get(resource.key)["revision"] == "2"
    assert len(db.history()) == 2
    t.close()


def test_conditional_revalidation_and_missing_file(setup):
    cfg, db = setup
    calls = []

    def handle(r):
        calls.append(r)
        if r.headers.get("If-None-Match"):
            return httpx.Response(304)
        return httpx.Response(200, content=b"PK fixture", headers={"etag": '"v1"'})

    t = transport(handle)
    resource = file_resource(FILE, COURSE, "Week 1")
    sync_file(resource, cfg, t, db)
    stamp = db.get(resource.key)["downloaded_at"]
    assert sync_file(resource, cfg, t, db, force=True) == "UNCHANGED"
    assert calls[-1].headers["If-None-Match"] == '"v1"'
    assert db.get(resource.key)["downloaded_at"] == stamp
    Path(db.get(resource.key)["path"]).unlink()
    assert sync_file(resource, cfg, t, db) == "RESTORED"
    assert "If-None-Match" not in calls[-1].headers
    t.close()


def test_same_hash_and_metadata_less_revalidation(setup):
    cfg, db = setup
    body = b"old"
    t = transport(lambda r: httpx.Response(200, content=body))
    resource = file_resource(FILE, COURSE, "Week 1")
    sync_file(resource, cfg, t, db)
    assert sync_file(resource, cfg, t, db, force=True) == "UNCHANGED"
    body = b"new without changed revision"
    assert sync_file(resource, cfg, t, db, force=True) == "UPDATED"
    assert len(list(cfg.output_dir.rglob("*.pdf"))) == 2
    t.close()


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "text/html"}, b"<html>Login</html>"),
        ({"content-type": "application/pdf"}, b"<!DOCTYPE html><html>Login</html>"),
        ({"content-type": "application/json"}, b'{"error": "denied"}'),
        ({"content-length": "99999999999"}, b"too large"),
    ],
)
def test_invalid_download_never_published(setup, headers, body):
    cfg, db = setup
    t = transport(lambda r: httpx.Response(200, content=body, headers=headers))
    resource = file_resource(FILE, COURSE, "x")
    with pytest.raises(SyncError):
        sync_file(resource, cfg, t, db)
    assert db.get(resource.key) is None
    assert not list(cfg.output_dir.rglob("*"))
    assert not list(cfg.state_dir.glob("*.part"))
    t.close()


def test_stream_size_limit_and_interruption(setup):
    cfg, db = setup
    cfg = replace(cfg, max_file_mb=1)
    t = transport(lambda r: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
    with pytest.raises(SyncError):
        sync_file(file_resource(FILE, COURSE, "x"), cfg, t, db)
    assert not list(cfg.state_dir.glob("*.part"))
    t.close()


def test_collision_safety_and_sanitizing(tmp_path):
    resource = file_resource(FILE, Course(42, "../CON"), "../../Week")
    staging = tmp_path / "staging"
    staging.write_bytes(b"content")
    output = tmp_path / "output"
    output.mkdir()
    first = publish(staging, output, resource, "../../evil.pdf", "abc")
    second = publish(staging, output, resource, "../../evil.pdf", "abc")
    assert first != second
    assert first.read_bytes() == second.read_bytes() == b"content"
    assert first.resolve().is_relative_to(output)
    assert clean_name("CON.pdf") == "_CON.pdf"
    assert clean_name("LPT1.docx") == "_LPT1.docx"
    assert "/" not in clean_name("../../secret")
    assert len(clean_name("🧪" * 100).encode()) <= 100
    assert (
        disposition_name("attachment; filename*=UTF-8''Lecture%20%C3%9C.pdf", "x")
        == "Lecture Ü.pdf"
    )


def test_symlink_escape_rejected(tmp_path):
    outside, output = tmp_path / "outside", tmp_path / "output"
    outside.mkdir()
    output.mkdir()
    try:
        (output / "Algorithms [42]").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    temp = tmp_path / "temp"
    temp.write_bytes(b"x")
    with pytest.raises(SyncError):
        publish(temp, output, file_resource(FILE, COURSE, "x"), "x.pdf", "abc")
    assert not list(outside.iterdir())


def test_sync_only_selected_courses_and_partial_failures(setup):
    cfg, db = setup
    paths = []

    def handle(r):
        paths.append(str(r.url))
        if r.url.path == "/my/":
            return httpx.Response(200, text=AUTH)
        if r.url.path == "/lib/ajax/service.php":
            return httpx.Response(
                200,
                json=[
                    {
                        "data": {
                            "courses": [
                                {"id": 42, "fullname": "Algorithms"},
                                {"id": 99, "fullname": "Other"},
                            ],
                            "nextoffset": 0,
                        }
                    }
                ],
            )
        if r.url.path == "/course/view.php":
            html = f'<a href="{FILE}">File</a><a href="{FILE.replace("lecture.pdf", "missing.zip")}">Missing</a>'
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})
        if r.url.path.endswith("missing.zip"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF fixture")

    t = transport(handle)
    report = synchronize(cfg, Moodle(t), db)
    assert report.failed and not report.auth_expired
    assert "NEW" in report.text() and "ERROR" in report.text()
    assert not any("id=99" in p for p in paths)
    assert len(list(cfg.output_dir.rglob("*.pdf"))) == 1
    t.close()


def test_interrupted_stream_does_not_update_database_or_output(setup):
    cfg, db = setup

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * (1024 * 1024)
            raise httpx.ReadError("fixture interruption")

    t = transport(lambda r: httpx.Response(200, stream=BrokenStream()))
    resource = file_resource(FILE, COURSE, "Week 1")
    with pytest.raises(httpx.ReadError):
        sync_file(resource, cfg, t, db)
    assert not list(cfg.state_dir.glob("*.part"))
    assert not list(cfg.output_dir.rglob("*"))
    assert db.get(resource.key) is None
    t.close()


def test_unselected_and_unenrolled_fail_before_crawling(setup):
    cfg, db = setup

    class EmptyMoodle:
        def courses(self):
            return []

    with pytest.raises(SyncError, match="No courses selected"):
        synchronize(replace(cfg, course_ids=()), EmptyMoodle(), db)
    with pytest.raises(SyncError, match="not in your enrolled"):
        synchronize(cfg, EmptyMoodle(), db)


def test_remote_deletion_preserves_local_copy(setup):
    cfg, db = setup
    response = httpx.Response(200, content=b"keep me")
    t = transport(lambda r: response)
    resource = file_resource(FILE, COURSE, "x")
    sync_file(resource, cfg, t, db)
    saved = db.get(resource.key)
    response = httpx.Response(404)
    with pytest.raises(SyncError):
        sync_file(resource, cfg, t, db, force=True)
    assert db.get(resource.key) == saved
    assert Path(saved["path"]).read_bytes() == b"keep me"
    t.close()
