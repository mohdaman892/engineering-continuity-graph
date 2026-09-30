"""SQLite connection wrapper.

The file is not opened during import or server construction. Reads use
``mode=ro``. The first real write reopens ``mode=rw`` (``rwc`` only to create
a missing file). A lock timeout of about two seconds keeps a stuck writer from
hanging a tool call.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from eng_graph.models import LOCK_TIMEOUT_SECONDS, SQLITE_TIMEOUT_SECONDS, debug_enabled

SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS repositories (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        remote_url TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL REFERENCES repositories(id),
        mode TEXT NOT NULL DEFAULT 'default',
        ask_prompt INTEGER NOT NULL DEFAULT 1,
        may_store INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consent_grants (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        decision TEXT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS exchanges (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange_id TEXT NOT NULL REFERENCES exchanges(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        ordinal INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS facts (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL REFERENCES repositories(id),
        session_id TEXT NOT NULL REFERENCES sessions(id),
        exchange_id TEXT REFERENCES exchanges(id),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        fact_type TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 1.0,
        disputed INTEGER NOT NULL DEFAULT 0,
        promoted INTEGER NOT NULL DEFAULT 1,
        superseded_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_embeddings (
        fact_id TEXT PRIMARY KEY REFERENCES facts(id) ON DELETE CASCADE,
        dim INTEGER NOT NULL,
        vector BLOB NOT NULL,
        provider TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL REFERENCES repositories(id),
        name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        UNIQUE (repo_id, normalized_name, entity_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fact_entities (
        fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        PRIMARY KEY (fact_id, entity_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edges (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL,
        src_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        dst_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        edge_type TEXT NOT NULL,
        weight REAL NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflicts (
        id TEXT PRIMARY KEY,
        repo_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        conflict_type TEXT NOT NULL,
        new_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        existing_fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'open',
        resolution TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
        title,
        body,
        fact_id UNINDEXED,
        tokenize='porter'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_facts_repo ON facts(repo_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_repo ON sessions(repo_id, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_fact_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_fact_id)",
    "CREATE INDEX IF NOT EXISTS idx_grants_session ON consent_grants(session_id, decision, consumed)",
    "CREATE INDEX IF NOT EXISTS idx_entities_repo ON entities(repo_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_unique ON edges(src_fact_id, dst_fact_id, edge_type)",
)


class DatabaseError(Exception):
    """Base class for storage failures that a tool should surface immediately."""


class DatabaseMissing(DatabaseError):
    pass


class DatabaseBusy(DatabaseError):
    pass


class DatabaseCorrupt(DatabaseError):
    pass


def debug(message: str) -> None:
    if debug_enabled():
        print(f"ecg: {message}", file=sys.stderr)


class Database:
    """One process-wide connection, guarded by an ``RLock`` with a short timeout."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._mode: str | None = None
        self._migrated = False
        import threading

        self._lock = threading.RLock()

    def close(self) -> None:
        acquired = self._lock.acquire(timeout=LOCK_TIMEOUT_SECONDS)
        if not acquired:
            return
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._mode = None
        finally:
            self._lock.release()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self._acquire()
        try:
            conn = self._open("ro")
            try:
                yield conn
            except sqlite3.DatabaseError as exc:
                raise DatabaseCorrupt(str(exc)) from exc
        finally:
            self._lock.release()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Begin an immediate transaction and roll it back if the body raises."""
        self._acquire()
        started = False
        try:
            conn = self._open("rw")
            self._migrate(conn)
            try:
                conn.execute("BEGIN IMMEDIATE")
                started = True
                yield conn
                conn.execute("COMMIT")
                started = False
            except sqlite3.OperationalError as exc:
                if started:
                    self._rollback(conn)
                    started = False
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise DatabaseBusy(str(exc)) from exc
                raise DatabaseCorrupt(str(exc)) from exc
            except sqlite3.DatabaseError as exc:
                if started:
                    self._rollback(conn)
                    started = False
                raise DatabaseCorrupt(str(exc)) from exc
            except Exception:
                if started:
                    self._rollback(conn)
                    started = False
                raise
        finally:
            self._lock.release()

    def _acquire(self) -> None:
        if not self._lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            raise DatabaseBusy(
                f"storage lock was not free within {LOCK_TIMEOUT_SECONDS:.0f}s"
            )

    def _open(self, mode: str) -> sqlite3.Connection:
        if mode == "ro":
            if self._conn is not None:
                return self._conn
            if not self.path.exists():
                raise DatabaseMissing(str(self.path))
            self._conn = self._connect("ro")
            self._mode = "ro"
            return self._conn
        if self._mode == "rw" and self._conn is not None:
            return self._conn
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._mode = None
        creating = not self.path.exists()
        if creating:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri_mode = "rwc" if creating else "rw"
        self._conn = self._connect(uri_mode)
        self._mode = "rw"
        return self._conn

    def _connect(self, uri_mode: str) -> sqlite3.Connection:
        uri = "file:" + quote(self.path.as_posix()) + f"?mode={uri_mode}"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=SQLITE_TIMEOUT_SECONDS,
                check_same_thread=False,
            )
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise DatabaseBusy(str(exc)) from exc
            if "unable to open" in text and uri_mode == "ro":
                raise DatabaseMissing(str(self.path)) from exc
            raise DatabaseCorrupt(str(exc)) from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.isolation_level = None
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT_SECONDS * 1000)}")
            if uri_mode != "ro":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError as exc:
            conn.close()
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                raise DatabaseBusy(str(exc)) from exc
            raise DatabaseCorrupt(str(exc)) from exc
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        if self._migrated:
            return
        try:
            for statement in _SCHEMA:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) > SCHEMA_VERSION:
                raise DatabaseCorrupt(
                    f"database schema {row['version']} is newer than {SCHEMA_VERSION}"
                )
        except sqlite3.DatabaseError as exc:
            raise DatabaseCorrupt(str(exc)) from exc
        self._migrated = True

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
