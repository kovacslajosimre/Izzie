import sqlite3

from app import db


def test_migrate_creates_schema():
    conn = sqlite3.connect(":memory:")
    db._migrate(conn)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "messages", "facts"} <= tables

    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_messages_session", "idx_facts_active"} <= indexes

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db.MIGRATIONS)


def test_migrate_is_idempotent():
    conn = sqlite3.connect(":memory:")
    db._migrate(conn)
    db._migrate(conn)  # nem hasalhat el ismetelt CREATE TABLE-on

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db.MIGRATIONS)


def test_migrate_v1_to_v2_preserves_data():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO sessions (started_at) VALUES ('2026-01-01T00:00:00+00:00')")
    conn.commit()

    db._migrate(conn)

    row = conn.execute(
        "SELECT started_at, extracted_at, extract_attempts FROM sessions"
    ).fetchone()
    assert row["started_at"] == "2026-01-01T00:00:00+00:00"
    assert row["extracted_at"] is None
    assert row["extract_attempts"] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_connect_creates_db_file_and_parent_dir(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "izzie.db"
    monkeypatch.setattr(db, "DB_PATH", target)

    conn = db.connect()
    try:
        assert target.exists()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == len(db.MIGRATIONS)
    finally:
        conn.close()
