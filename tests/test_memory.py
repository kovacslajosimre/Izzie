from datetime import datetime, timedelta, timezone

from app import memory

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_resolve_session_creates_new_when_none_open(conn):
    session_id = memory.resolve_session(conn, now=T0)

    row = conn.execute(
        "SELECT started_at, ended_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["ended_at"] is None
    assert row["started_at"] == T0.isoformat()


def test_resolve_session_reuses_open_session_within_timeout(conn):
    session_id = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, session_id, "user", "szia", "complete", created_at=T0)

    later = T0 + timedelta(minutes=29)
    same_id = memory.resolve_session(conn, now=later)

    assert same_id == session_id
    row = conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert row["ended_at"] is None


def test_resolve_session_closes_stale_session_and_opens_new(conn):
    session_id = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, session_id, "user", "szia", "complete", created_at=T0)

    later = T0 + timedelta(minutes=31)
    new_id = memory.resolve_session(conn, now=later)

    assert new_id != session_id
    old = conn.execute(
        "SELECT ended_at, closed_by FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert old["ended_at"] == later.isoformat()
    assert old["closed_by"] == "timeout"


def test_resolve_session_without_messages_uses_started_at(conn):
    session_id = memory.resolve_session(conn, now=T0)

    # Nincs uzenet a sessionben - a started_at-hoz kell meroje a timeoutnak.
    later = T0 + timedelta(minutes=31)
    new_id = memory.resolve_session(conn, now=later)

    assert new_id != session_id


def test_resolve_session_closes_older_duplicates_after_crash(conn):
    old_id = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, old_id, "user", "elso", "complete", created_at=T0)

    # Korabbi osszeomlas szimulalasa: masodik session nyilik anelkul, hogy
    # az elsot lezarnank.
    crash_time = T0 + timedelta(minutes=5)
    new_session_id = memory._create_session(conn, crash_time)
    conn.commit()
    memory.log_message(conn, new_session_id, "user", "masodik", "complete", created_at=crash_time)

    check_time = crash_time + timedelta(minutes=5)
    resolved_id = memory.resolve_session(conn, now=check_time)

    assert resolved_id == new_session_id  # a legfrissebb marad aktiv

    old = conn.execute(
        "SELECT ended_at, closed_by FROM sessions WHERE id = ?", (old_id,)
    ).fetchone()
    assert old["ended_at"] == check_time.isoformat()
    assert old["closed_by"] == "timeout"


def test_resolve_session_closes_all_when_even_newest_is_stale(conn):
    id1 = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, id1, "user", "elso", "complete", created_at=T0)

    t1 = T0 + timedelta(minutes=5)
    id2 = memory._create_session(conn, t1)
    conn.commit()
    memory.log_message(conn, id2, "user", "masodik", "complete", created_at=t1)

    much_later = t1 + timedelta(minutes=40)
    new_id = memory.resolve_session(conn, now=much_later)

    assert new_id not in (id1, id2)
    for old_id in (id1, id2):
        row = conn.execute(
            "SELECT ended_at, closed_by FROM sessions WHERE id = ?", (old_id,)
        ).fetchone()
        assert row["closed_by"] == "timeout"
        assert row["ended_at"] is not None


def test_log_message_inserts_and_returns_id(conn):
    session_id = memory.resolve_session(conn, now=T0)
    message_id = memory.log_message(
        conn, session_id, "assistant", "szia!", "complete", created_at=T0
    )

    row = conn.execute(
        "SELECT session_id, role, content, created_at, status FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row["session_id"] == session_id
    assert row["role"] == "assistant"
    assert row["content"] == "szia!"
    assert row["created_at"] == T0.isoformat()
    assert row["status"] == "complete"
