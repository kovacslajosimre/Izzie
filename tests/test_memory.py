import asyncio
from datetime import datetime, timedelta, timezone

from app import db, memory

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


def test_log_assistant_message_skips_empty_content_regardless_of_status(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    async def scenario():
        session_id = await memory.start_turn("szia")
        await memory.log_assistant_message(session_id, "   ", "complete")
        await memory.log_assistant_message(session_id, "", "partial")
        return session_id

    session_id = asyncio.run(scenario())

    conn = db.connect()
    try:
        roles = [
            row["role"]
            for row in conn.execute(
                "SELECT role FROM messages WHERE session_id = ?", (session_id,)
            )
        ]
    finally:
        conn.close()

    assert roles == ["user"]


def _insert_fact(
    conn,
    kind="preference",
    content="teszt teny",
    created_at=T0,
    pinned=0,
    superseded_by=None,
    deleted_at=None,
    extractor_version="manual",
):
    cur = conn.execute(
        "INSERT INTO facts "
        "(kind, content, created_at, extractor_version, pinned, superseded_by, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            kind,
            content,
            _format(created_at) if isinstance(created_at, datetime) else created_at,
            extractor_version,
            pinned,
            superseded_by,
            deleted_at,
        ),
    )
    conn.commit()
    return cur.lastrowid


def _format(dt: datetime) -> str:
    return dt.isoformat()


def test_get_history_orders_and_filters(conn):
    session_id = memory.resolve_session(conn, now=T0)
    other_session_id = memory._create_session(conn, T0)
    conn.commit()

    memory.log_message(conn, session_id, "user", "elso", "complete", created_at=T0)
    memory.log_message(
        conn, session_id, "assistant", "valasz", "complete", created_at=T0 + timedelta(seconds=1)
    )
    redacted_id = memory.log_message(
        conn, session_id, "user", "titkos", "complete", created_at=T0 + timedelta(seconds=2)
    )
    conn.execute(
        "UPDATE messages SET redacted_at = ? WHERE id = ?", (_format(T0), redacted_id)
    )
    memory.log_message(
        conn, session_id, "assistant", "reszleges", "partial", created_at=T0 + timedelta(seconds=3)
    )
    memory.log_message(conn, other_session_id, "user", "mas session", "complete", created_at=T0)

    history = memory.get_history(conn, session_id)

    assert history == [
        {"role": "user", "content": "elso"},
        {"role": "assistant", "content": "valasz"},
        {"role": "assistant", "content": "reszleges"},
    ]


def test_get_history_respects_limit(conn):
    session_id = memory.resolve_session(conn, now=T0)
    for i in range(5):
        memory.log_message(
            conn, session_id, "user", f"uzenet-{i}", "complete", created_at=T0 + timedelta(seconds=i)
        )

    history = memory.get_history(conn, session_id, limit=2)

    assert [m["content"] for m in history] == ["uzenet-3", "uzenet-4"]


def test_core_profile_excludes_non_active_and_non_pinned(conn):
    _insert_fact(conn, kind="identity", content="pinned aktiv", pinned=1)
    _insert_fact(conn, kind="identity", content="pinned de superseded", pinned=1, superseded_by=999)
    _insert_fact(conn, kind="identity", content="pinned de torolt", pinned=1, deleted_at=_format(T0))
    _insert_fact(conn, kind="identity", content="nem pinned", pinned=0)

    profile = memory.core_profile(conn)

    assert [f.content for f in profile] == ["pinned aktiv"]


def test_core_profile_orders_by_kind_then_created_at(conn):
    _insert_fact(conn, kind="relationship", content="r", pinned=1, created_at=T0)
    _insert_fact(conn, kind="identity", content="i2", pinned=1, created_at=T0 + timedelta(seconds=1))
    _insert_fact(conn, kind="identity", content="i1", pinned=1, created_at=T0)

    profile = memory.core_profile(conn)

    assert [f.content for f in profile] == ["i1", "i2", "r"]


def test_retrieve_matches_inflected_forms(conn):
    _insert_fact(conn, content="A felhasznalo szerveren fut Izzie.")

    results = memory.retrieve(conn, "mi van a szerverről?")

    assert [f.content for f in results] == ["A felhasznalo szerveren fut Izzie."]


def test_retrieve_ignores_stopwords_and_short_words(conn):
    _insert_fact(conn, content="Izzie egy szemelyes asszisztens.")

    results = memory.retrieve(conn, "hogy nem van azt mit")

    assert results == []


def test_tokenize_keeps_non_hungarian_accented_letters_whole():
    # Regi Windows-kodlapok miatt elofordulo ekezetek (pl. õ, û) - egy szuk,
    # csak magyar ekezeteket ismero minta szothatarnak nezne oket.
    assert memory._tokenize("kõnyvtár") == ["kõnyvtár"]
    assert memory._tokenize("Straße") == ["straße"]


def test_tokenize_keeps_words_with_digits():
    assert memory._tokenize("win11 gepen fut") == ["win11", "gepen", "fut"]


def test_retrieve_matches_query_on_digits(conn):
    _insert_fact(conn, content="A gepben egy RTX 3060 van.")

    results = memory.retrieve(conn, "milyen a 3060?")

    assert [f.content for f in results] == ["A gepben egy RTX 3060 van."]


def test_retrieve_excludes_pinned_zero_score_and_respects_k(conn):
    _insert_fact(conn, content="A kutya neve Morzsa.", pinned=1)
    for i in range(7):
        _insert_fact(conn, content=f"Morzsa kutyaval kapcsolatos teny {i}.", created_at=T0 + timedelta(seconds=i))
    _insert_fact(conn, content="Semmi kozos szo.")

    results = memory.retrieve(conn, "mit tudsz a kutyáról?", k=5)

    assert len(results) == 5
    assert all("kutya" in f.content.lower() for f in results)


def test_retrieve_ranks_by_score_then_recency(conn):
    _insert_fact(conn, content="A szerver otthon van.", created_at=T0)
    _insert_fact(conn, content="A szerver és a gép otthon van.", created_at=T0 + timedelta(seconds=1))
    _insert_fact(conn, content="A gép régi.", created_at=T0 + timedelta(seconds=2))

    results = memory.retrieve(conn, "a szerver és a gép otthon van")

    assert [f.content for f in results] == [
        "A szerver és a gép otthon van.",
        "A szerver otthon van.",
        "A gép régi.",
    ]


def test_format_memory_empty_inputs():
    assert memory.format_memory([], []) == ""


def test_format_memory_omits_empty_part():
    core = [memory.Fact(id=1, kind="identity", content="Nev: Lajos.", pinned=True, created_at=T0.isoformat())]

    result = memory.format_memory(core, [])

    assert result == "A felhasználóról:\n- Nev: Lajos."
