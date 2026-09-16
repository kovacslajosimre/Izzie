"""SQLite kapcsolat es PRAGMA user_version alapu migracio.

Lasd docs/memory.md - a nyers naplo (sessions, messages) es a meg ures
facts tabla semaja innen szarmazik szo szerint.
"""

import sqlite3

from app.paths import resolve_path

DB_PATH = resolve_path("IZZIE_DB_PATH", "data/izzie.db")

_SCHEMA_V1 = """
CREATE TABLE sessions (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL,          -- ISO 8601, UTC
  ended_at      TEXT,
  closed_by     TEXT,                   -- 'timeout' | 'nightly' | 'manual'
  summary       TEXT,
  summary_model TEXT,
  summarized_at TEXT
);

CREATE TABLE messages (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id),
  role        TEXT NOT NULL,            -- 'user' | 'assistant'
  content     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  status      TEXT NOT NULL,            -- 'complete' | 'partial'
  redacted_at TEXT                      -- lasd: Felejtes
);

CREATE TABLE facts (
  id                INTEGER PRIMARY KEY,
  kind              TEXT NOT NULL,      -- zart lista, lasd docs/memory.md
  content           TEXT NOT NULL,      -- egy mondat, termeszetes nyelven
  confidence        REAL,
  source_message_id INTEGER REFERENCES messages(id),
  created_at        TEXT NOT NULL,
  superseded_by     INTEGER REFERENCES facts(id),
  superseded_at     TEXT,
  deleted_at        TEXT,
  extractor_version TEXT NOT NULL,
  pinned            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_messages_session ON messages(session_id, created_at);
CREATE INDEX idx_facts_active     ON facts(kind)
  WHERE superseded_by IS NULL AND deleted_at IS NULL;
"""

_SCHEMA_V2 = """
ALTER TABLE sessions ADD COLUMN extracted_at     TEXT;
ALTER TABLE sessions ADD COLUMN extract_attempts INTEGER NOT NULL DEFAULT 0;
"""

MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2]


def connect() -> sqlite3.Connection:
    """Megnyit egy kapcsolatot a DB-hez, es lefuttatja a hianyzo migraciokat."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, script in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
