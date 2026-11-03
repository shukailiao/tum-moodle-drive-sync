import json

import httpx
import pytest
from filelock import FileLock

from tum_moodle_drive_sync.cli import main
from tum_moodle_drive_sync.config import load_config
from tum_moodle_drive_sync.errors import AuthenticationExpired, SyncError
from tum_moodle_drive_sync.http import Transport, safe_url


def write_config(tmp_path, **changes):
    values = {
        "output_dir": str(tmp_path / "drive"),
        "state_dir": str(tmp_path / "state"),
        "course_ids": [42],
    }
    values.update(changes)
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items()), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize(
    "changes",
    [
        {"course_ids": [True]},
        {"retries": 6},
        {"request_interval": 0},
        {"output_dir": "relative"},
        {"browser_channel": "bad"},
        {"token": "not-a-supported-setting"},
    ],
)
def test_config_validation(tmp_path, changes):
    with pytest.raises(SyncError):
        load_config(write_config(tmp_path, **changes))


def test_nested_state_rejected(tmp_path):
    path = write_config(tmp_path, state_dir=str(tmp_path / "drive" / "state"))
    with pytest.raises(SyncError):
        load_config(path)


def test_cli_init_and_no_overwrite(tmp_path, capsys):
    path = tmp_path / "config.toml"
    args = ["--config", str(path), "init", "--output", str(tmp_path / "drive")]
    assert main(args) == 0
    original = path.read_text()
    assert main(args) == 1
    assert path.read_text() == original
    assert "already exists" in capsys.readouterr().err


def test_cli_missing_auth_and_drive(tmp_path, capsys):
    path = write_config(tmp_path)
    assert main(["--config", str(path), "check"]) == 2
    assert main(["--config", str(path), "sync"]) == 1
    assert "output_dir does not exist" in capsys.readouterr().err


def test_cli_lock_prevents_overlap(tmp_path):
    path = write_config(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    with FileLock(state / "sync.lock"):
        assert main(["--config", str(path), "logout"]) == 3


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/",
        "http://www.moodle.tum.de/",
        "https://www.moodle.tum.de:444/",
        "https://user:pass@www.moodle.tum.de/",
    ],
)
def test_url_scope(url):
    with pytest.raises(SyncError):
        safe_url(url)


def test_retry_limit_and_retry_after(monkeypatch):
    waits, calls = [], []
    monkeypatch.setattr("tum_moodle_drive_sync.http.time.sleep", waits.append)

    def handle(r):
        calls.append(r)
        return httpx.Response(429, headers={"Retry-After": "2"})

    t = Transport([], interval=0, retries=2, transport=httpx.MockTransport(handle))
    with pytest.raises(SyncError, match="rate-limiting"):
        t.request("GET", "/my/")
    assert len(calls) == 3
    assert waits.count(2) == 2
    t.close()


def test_no_cookie_forwarding_on_external_redirect():
    calls = []

    def handle(r):
        calls.append(r)
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    t = Transport(
        [{"name": "MoodleSession", "value": "fixture", "domain": "www.moodle.tum.de"}],
        interval=0,
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(AuthenticationExpired):
        t.request("GET", "/my/")
    assert len(calls) == 1
    t.close()


def test_malformed_port_is_a_safe_error():
    with pytest.raises(SyncError, match="Malformed"):
        safe_url("https://www.moodle.tum.de:invalid/resource")


def test_cli_full_fixture_sync(tmp_path, monkeypatch, capsys):
    from tum_moodle_drive_sync import cli
    from tum_moodle_drive_sync.security import SessionStore

    path = write_config(tmp_path)
    (tmp_path / "drive").mkdir()
    store = SessionStore(tmp_path / "state")
    store.save([{"domain": "www.moodle.tum.de", "name": "MoodleSession", "value": "fixture"}])
    requests = []

    def handle(r):
        requests.append(r.url.path)
        if r.url.path == "/my/":
            return httpx.Response(
                200,
                text='<script>{"sesskey":"fixture"}</script><a href="/login/logout.php">Logout</a>',
            )
        if r.url.path == "/lib/ajax/service.php":
            return httpx.Response(
                200,
                json=[
                    {"data": {"courses": [{"id": 42, "fullname": "Algorithms"}], "nextoffset": 0}}
                ],
            )
        if r.url.path == "/course/view.php":
            return httpx.Response(
                200,
                text='<a href="/pluginfile.php/12/mod_resource/content/1/a.pdf">PDF</a>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(200, content=b"%PDF fixture")

    monkeypatch.setattr(
        cli,
        "Transport",
        lambda cookies, **kwargs: Transport(
            cookies, interval=0, transport=httpx.MockTransport(handle)
        ),
    )
    assert main(["--config", str(path), "sync"]) == 0
    assert "NEW" in capsys.readouterr().out
    assert main(["--config", str(path), "sync"]) == 0
    assert "UNCHANGED" in capsys.readouterr().out
    assert main(["--config", str(path), "history"]) == 0
    assert "NEW" in capsys.readouterr().out
    assert len(list((tmp_path / "state").glob("changelog-*.txt"))) == 2
    assert requests.count("/pluginfile.php/12/mod_resource/content/1/a.pdf") == 1
    assert len(list((tmp_path / "drive").rglob("*.pdf"))) == 1
