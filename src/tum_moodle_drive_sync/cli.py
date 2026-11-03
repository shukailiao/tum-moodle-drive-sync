import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from filelock import FileLock, Timeout

from .auth import login
from .config import default_config, default_state, load_config
from .database import Database
from .errors import AuthenticationExpired, SyncError
from .http import Transport
from .moodle import Moodle
from .security import SessionStore, private_dir
from .sync import synchronize


def initialize(path, output):
    if not output.is_absolute():
        raise SyncError("--output must be an absolute path to your Google Drive desktop folder.")
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON quoted strings are valid TOML basic strings for normal filesystem paths.
    template = (
        f"output_dir = {json.dumps(str(output), ensure_ascii=False)}\n"
        f"state_dir = {json.dumps(str(default_state()), ensure_ascii=False)}\n"
        "course_ids = []\nrequest_interval = 1.0\nretries = 3\n"
        "timeout = 60.0\nrevalidate_hours = 24.0\nmax_file_mb = 512\n"
        '# browser_channel = "msedge"\n'
    )
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(template)
    except FileExistsError as exc:
        raise SyncError("Config already exists; edit it instead of overwriting it.") from exc
    print(f"Created {path}. Run login, then courses, then edit course_ids.")


def setup_logging(state):
    handler = RotatingFileHandler(
        state / "sync.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("tum_moodle_drive_sync")
    for old in logger.handlers[:]:
        old.close()
        logger.removeHandler(old)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy selected TUM Moodle files into Google Drive for desktop."
    )
    parser.add_argument(
        "--config", type=Path, default=default_config(), help="TOML configuration path"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create configuration without overwriting an existing file")
    init.add_argument("--output", type=Path, required=True)
    sub.add_parser("login", help="Interactive SSO/MFA in a visible browser")
    sub.add_parser(
        "logout", help="Remove the locally saved session (does not revoke server sessions)"
    )
    sub.add_parser("courses", help="List enrolled courses and their selection status")
    sub.add_parser("check", help="Verify the saved Moodle session")
    sync = sub.add_parser("sync", help="Download selected courses and print a changelog")
    sync.add_argument(
        "--revalidate",
        action="store_true",
        help="Revalidate all files now, using conditional GET when possible",
    )
    sub.add_parser("history", help="Show the last 100 local history entries")
    args = parser.parse_args(argv)
    if os.name != "nt":
        os.umask(0o077)
    try:
        if args.command == "init":
            initialize(args.config, args.output)
            return 0
        config = load_config(args.config)
        private_dir(config.state_dir)
        # Fail closed if Drive is unmounted. Do not create an accidental local substitute.
        if args.command == "sync" and not config.output_dir.is_dir():
            raise SyncError(
                "output_dir does not exist. Mount/start Google Drive and create the folder first."
            )
        with FileLock(config.state_dir / "sync.lock", timeout=0):
            setup_logging(config.state_dir)
            store = SessionStore(config.state_dir)
            if args.command == "logout":
                store.delete()
                print("Local session removed.")
                return 0
            if args.command == "login":
                login(config, store)
                print("Moodle session verified and saved locally.")
                return 0
            if args.command == "history":
                db = Database(config.state_dir / "state.sqlite3")
                try:
                    for event in db.history():
                        stamp = datetime.fromtimestamp(event["timestamp"], timezone.utc).isoformat()
                        line = f"{stamp} {event['status']} | {event['course_name']} | {event['detail']}"
                        print("".join(c if c.isprintable() else " " for c in line))
                finally:
                    db.close()
                return 0
            transport = Transport(
                store.load(),
                interval=config.request_interval,
                retries=config.retries,
                timeout=config.timeout,
            )
            try:
                moodle = Moodle(transport)
                if args.command == "check":
                    moodle.check_session()
                    print("Saved Moodle session is valid.")
                elif args.command == "courses":
                    courses = moodle.courses()
                    for course in courses:
                        mark = "*" if course.id in config.course_ids else " "
                        name = "".join(c if c.isprintable() else " " for c in course.name)
                        print(f"{mark} {course.id:8}  {name}")
                    print(f"{len(courses)} enrolled courses. Set course_ids in {args.config}.")
                else:
                    db = Database(config.state_dir / "state.sqlite3")
                    try:
                        report = synchronize(config, moodle, db, force=args.revalidate)
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        report_path = (
                            config.state_dir / f"changelog-{stamp}-{uuid.uuid4().hex[:8]}.txt"
                        )
                        report_path.write_text(report.text() + "\n", encoding="utf-8")
                        print(report.text())
                        print(f"Changelog saved to {report_path}")
                        if report.auth_expired:
                            return 2
                        store.save(transport.export_cookies())
                        return 1 if report.failed else 0
                    finally:
                        db.close()
                store.save(transport.export_cookies())
            finally:
                transport.close()
        return 0
    except AuthenticationExpired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Timeout:
        print("Another command is using this state directory; try later.", file=sys.stderr)
        return 3
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, httpx.HTTPError):
        print(
            "Local IO, database or network error. Check paths, permissions, free space and connectivity.",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; session verification was not completed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
