import sqlite3
import time
from pathlib import Path

from .errors import SyncError


class Database:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        if self.connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            self.connection.close()
            raise SyncError(
                "Unsupported database schema version; use the matching application version."
            )
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS resources (
                key TEXT PRIMARY KEY, course_id INTEGER NOT NULL, course_name TEXT NOT NULL,
                section TEXT NOT NULL, source_url TEXT NOT NULL, revision TEXT,
                path TEXT NOT NULL, sha256 TEXT NOT NULL, etag TEXT, last_modified TEXT,
                downloaded_at REAL NOT NULL, checked_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, resource_key TEXT,
                course_name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL
            );
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.connection.close()

    def get(self, key):
        row = self.connection.execute("SELECT * FROM resources WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def record(self, resource, path, digest, etag, modified, status, *, downloaded=True):
        now = time.time()
        old = self.get(resource.key)
        downloaded_at = now if downloaded or not old else old["downloaded_at"]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    course_name=excluded.course_name, section=excluded.section,
                    source_url=excluded.source_url, revision=excluded.revision,
                    path=excluded.path, sha256=excluded.sha256, etag=excluded.etag,
                    last_modified=excluded.last_modified, downloaded_at=excluded.downloaded_at,
                    checked_at=excluded.checked_at
            """,
                (
                    resource.key,
                    resource.course.id,
                    resource.course.name,
                    resource.section,
                    resource.url,
                    resource.revision,
                    str(path),
                    digest,
                    etag,
                    modified,
                    downloaded_at,
                    now,
                ),
            )
            self._event(resource.key, resource.course.name, status, str(path))

    def _event(self, key, course, status, detail):
        self.connection.execute(
            "INSERT INTO events VALUES (NULL, ?, ?, ?, ?, ?)",
            (time.time(), key, course, status, detail),
        )

    def event(self, key, course, status, detail):
        with self.connection:
            self._event(key, course, status, detail)

    def history(self, limit=100):
        return self.connection.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
