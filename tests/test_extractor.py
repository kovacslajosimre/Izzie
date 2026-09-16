"""app.extractor tesztek.

LLM-hivasra nincs teszt (docs/memory.md): a hivas helyere kanonizalt
parsed-valaszt (vagy kivetelt) ado hamis kliens kerul.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import db, extractor, memory

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def _insert_fact(conn, kind="preference", content="teszt teny", pinned=0, extractor_version="manual"):
    cur = conn.execute(
        "INSERT INTO facts (kind, content, created_at, extractor_version, pinned) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, content, T0.isoformat(), extractor_version, pinned),
    )
    conn.commit()
    return cur.lastrowid


def _fact(action="new", kind="identity", content="Valami.", supersedes_id=None, source_message_id=1):
    return extractor.ExtractedFact(
        action=action,
        kind=kind,
        content=content,
        supersedes_id=supersedes_id,
        source_message_id=source_message_id,
    )


class _FakeModels:
    def __init__(self, parsed=None, raise_exc=None):
        self._parsed = parsed
        self._raise = raise_exc

    async def generate_content(self, **kwargs):
        if self._raise:
            raise self._raise
        return SimpleNamespace(parsed=self._parsed)


class _FakeClient:
    def __init__(self, parsed=None, raise_exc=None):
        self.aio = SimpleNamespace(models=_FakeModels(parsed, raise_exc))


# --- load_extractor_prompt ---------------------------------------------


def test_load_extractor_prompt_reads_file(tmp_path):
    path = tmp_path / "extractor.md"
    path.write_text("Tarolt prompt szoveg.", encoding="utf-8")

    assert extractor.load_extractor_prompt(path) == "Tarolt prompt szoveg."


def test_load_extractor_prompt_missing_file_raises(tmp_path):
    missing = tmp_path / "nincs.md"

    with pytest.raises(FileNotFoundError):
        extractor.load_extractor_prompt(missing)


# --- select_unprocessed_sessions ----------------------------------------


def test_select_unprocessed_sessions_filters_correctly(conn):
    recent_id = memory._create_session(conn, T0)
    conn.commit()
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ?",
        ((T0 + timedelta(seconds=60)).isoformat(), recent_id),
    )

    extracted_id = memory._create_session(conn, T0)
    conn.commit()
    conn.execute(
        "UPDATE sessions SET ended_at = ?, extracted_at = ? WHERE id = ?",
        (T0.isoformat(), T0.isoformat(), extracted_id),
    )

    exhausted_id = memory._create_session(conn, T0)
    conn.commit()
    conn.execute(
        "UPDATE sessions SET ended_at = ?, extract_attempts = ? WHERE id = ?",
        (T0.isoformat(), extractor.MAX_EXTRACT_ATTEMPTS, exhausted_id),
    )

    ready_id = memory._create_session(conn, T0)
    conn.commit()
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (T0.isoformat(), ready_id))
    conn.commit()

    now = T0 + timedelta(seconds=extractor.EXTRACT_GRACE_SECONDS + 10)
    result = extractor.select_unprocessed_sessions(conn, now=now)

    assert result == [ready_id]


# --- load_session_input --------------------------------------------------


def test_load_session_input_excludes_partial_and_redacted_includes_ids(conn):
    session_id = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, session_id, "user", "elso uzenet", "complete", created_at=T0)
    memory.log_message(
        conn, session_id, "assistant", "reszleges valasz", "partial", created_at=T0 + timedelta(seconds=1)
    )
    redacted_id = memory.log_message(
        conn, session_id, "user", "titkos", "complete", created_at=T0 + timedelta(seconds=2)
    )
    conn.execute("UPDATE messages SET redacted_at = ? WHERE id = ?", (T0.isoformat(), redacted_id))
    conn.commit()

    fact_id = _insert_fact(conn, kind="identity", content="A felhasznalo neve Lajos.")

    result = extractor.load_session_input(conn, session_id)

    assert [m["content"] for m in result.messages] == ["elso uzenet"]
    assert result.facts == [{"id": fact_id, "kind": "identity", "content": "A felhasznalo neve Lajos."}]


# --- _is_valid_fact --------------------------------------------------------


def test_is_valid_fact_rejects_unknown_kind():
    assert extractor._is_valid_fact(_fact(kind="hobbi"), {1}, set(), set()) is False


def test_is_valid_fact_rejects_unknown_action():
    assert extractor._is_valid_fact(_fact(action="delete"), {1}, set(), set()) is False


def test_is_valid_fact_rejects_empty_content():
    assert extractor._is_valid_fact(_fact(content="   "), {1}, set(), set()) is False


def test_is_valid_fact_rejects_foreign_source_message_id():
    assert extractor._is_valid_fact(_fact(source_message_id=99), {1, 2}, set(), set()) is False


def test_is_valid_fact_rejects_supersede_missing_id():
    item = _fact(action="supersede", supersedes_id=None, source_message_id=1)
    assert extractor._is_valid_fact(item, {1}, {5}, set()) is False


def test_is_valid_fact_rejects_supersede_of_inactive_fact():
    item = _fact(action="supersede", supersedes_id=5, source_message_id=1)
    assert extractor._is_valid_fact(item, {1}, {6, 7}, set()) is False


def test_is_valid_fact_rejects_double_supersede_in_same_round():
    item = _fact(action="supersede", supersedes_id=5, source_message_id=1)
    assert extractor._is_valid_fact(item, {1}, {5}, {5}) is False


def test_is_valid_fact_accepts_valid_new():
    assert extractor._is_valid_fact(_fact(action="new", source_message_id=1), {1}, set(), set()) is True


def test_is_valid_fact_accepts_valid_supersede():
    item = _fact(action="supersede", supersedes_id=5, source_message_id=1)
    assert extractor._is_valid_fact(item, {1}, {5}, set()) is True


# --- _validate_and_apply ---------------------------------------------------


def test_validate_and_apply_writes_new_fact_and_summary(conn):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "A kutyam neve Morzsa.", "complete", created_at=T0)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "A kutyam neve Morzsa."}], facts=[]
    )
    result = extractor.ExtractionResult(
        summary="Beszelgettek a kutyarol.",
        facts=[
            extractor.ExtractedFact(
                action="new",
                kind="relationship",
                content="A felhasznalo kutyaja Morzsa.",
                source_message_id=msg_id,
            )
        ],
    )

    extractor._validate_and_apply(conn, session_id, result, session_input, T0)
    conn.commit()

    fact = conn.execute("SELECT kind, content, pinned, extractor_version FROM facts").fetchone()
    assert fact["kind"] == "relationship"
    assert fact["content"] == "A felhasznalo kutyaja Morzsa."
    assert fact["pinned"] == 0
    assert fact["extractor_version"] == extractor.EXTRACTOR_VERSION

    session = conn.execute(
        "SELECT summary, summary_model, extracted_at FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    assert session["summary"] == "Beszelgettek a kutyarol."
    assert session["summary_model"] == extractor.EXTRACTOR_MODEL
    assert session["extracted_at"] == T0.isoformat()


def test_validate_and_apply_supersede_sets_superseded_by_and_inherits_pinned(conn):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "Uzenet", "complete", created_at=T0)
    old_id = _insert_fact(conn, kind="identity", content="A felhasznalo Budapesten el.", pinned=1)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "Uzenet"}],
        facts=[{"id": old_id, "kind": "identity", "content": "A felhasznalo Budapesten el."}],
    )
    result = extractor.ExtractionResult(
        summary="Koltozott.",
        facts=[
            extractor.ExtractedFact(
                action="supersede",
                kind="identity",
                content="A felhasznalo Szegeden el.",
                supersedes_id=old_id,
                source_message_id=msg_id,
            )
        ],
    )

    extractor._validate_and_apply(conn, session_id, result, session_input, T0)
    conn.commit()

    old = conn.execute("SELECT superseded_by, superseded_at FROM facts WHERE id = ?", (old_id,)).fetchone()
    new = conn.execute("SELECT id, pinned FROM facts WHERE id != ?", (old_id,)).fetchone()

    assert old["superseded_by"] == new["id"]
    assert old["superseded_at"] == T0.isoformat()
    assert new["pinned"] == 1


def test_validate_and_apply_new_fact_never_pinned(conn):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "Uzenet", "complete", created_at=T0)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "Uzenet"}], facts=[]
    )
    result = extractor.ExtractionResult(
        summary="X.",
        facts=[
            extractor.ExtractedFact(
                action="new", kind="preference", content="Valami.", source_message_id=msg_id
            )
        ],
    )

    extractor._validate_and_apply(conn, session_id, result, session_input, T0)

    fact = conn.execute("SELECT pinned FROM facts").fetchone()
    assert fact["pinned"] == 0


def test_validate_and_apply_skips_invalid_items_but_writes_summary(conn):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "Uzenet", "complete", created_at=T0)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "Uzenet"}], facts=[]
    )
    result = extractor.ExtractionResult(
        summary="Osszefoglalo meg ervenytelen teny mellett is.",
        facts=[
            extractor.ExtractedFact(
                action="new", kind="ismeretlen", content="X.", source_message_id=msg_id
            )
        ],
    )

    extractor._validate_and_apply(conn, session_id, result, session_input, T0)

    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    session = conn.execute("SELECT summary FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert session["summary"] == "Osszefoglalo meg ervenytelen teny mellett is."


def test_validate_and_apply_blank_summary_stored_as_null(conn):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "Uzenet", "complete", created_at=T0)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "Uzenet"}], facts=[]
    )
    result = extractor.ExtractionResult(summary="   ", facts=[])

    extractor._validate_and_apply(conn, session_id, result, session_input, T0)

    session = conn.execute(
        "SELECT summary, summary_model, summarized_at, extracted_at FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert session["summary"] is None
    assert session["summary_model"] is None
    assert session["summarized_at"] is None
    assert session["extracted_at"] == T0.isoformat()


def test_validate_and_apply_rollback_leaves_nothing_on_error(conn, monkeypatch):
    session_id = memory.resolve_session(conn, now=T0)
    msg_id = memory.log_message(conn, session_id, "user", "Uzenet", "complete", created_at=T0)
    session_input = extractor.SessionInput(
        messages=[{"id": msg_id, "role": "user", "content": "Uzenet"}], facts=[]
    )
    result = extractor.ExtractionResult(
        summary="X.",
        facts=[
            extractor.ExtractedFact(action="new", kind="preference", content="Elso.", source_message_id=msg_id),
            extractor.ExtractedFact(action="new", kind="preference", content="Masodik.", source_message_id=msg_id),
        ],
    )

    original_insert = extractor._insert_fact
    calls = {"count": 0}

    def failing_insert(conn, kind, content, source_message_id, now, pinned):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("szimulalt iras kozbeni hiba")
        return original_insert(conn, kind, content, source_message_id, now, pinned)

    monkeypatch.setattr(extractor, "_insert_fact", failing_insert)

    with pytest.raises(RuntimeError):
        extractor._validate_and_apply(conn, session_id, result, session_input, T0)
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


# --- process_session (tmp DB, hamis kliens) --------------------------------


def test_process_session_no_user_message_marks_extracted_without_llm_call(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    conn = db.connect()
    session_id = memory._create_session(conn, T0)
    conn.commit()
    memory.log_message(conn, session_id, "assistant", "csak asszisztens uzenet", "complete", created_at=T0)
    conn.close()

    class _ExplodingModels:
        async def generate_content(self, **kwargs):
            raise AssertionError("nem szabadna LLM-hivast inditani")

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=_ExplodingModels()))

    asyncio.run(extractor.process_session(fake_client, session_id, now=T0))

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT extracted_at, summary FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["extracted_at"] == T0.isoformat()
    assert row["summary"] is None


def test_process_session_api_error_increments_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    conn = db.connect()
    session_id = memory._create_session(conn, T0)
    conn.commit()
    memory.log_message(conn, session_id, "user", "Szia", "complete", created_at=T0)
    conn.close()

    fake_client = _FakeClient(raise_exc=RuntimeError("API hiba"))

    asyncio.run(extractor.process_session(fake_client, session_id, now=T0))

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT extracted_at, extract_attempts FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["extracted_at"] is None
    assert row["extract_attempts"] == 1


def test_process_session_timeout_increments_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")
    monkeypatch.setattr(extractor, "EXTRACTOR_TIMEOUT_SECONDS", 0.05)

    conn = db.connect()
    session_id = memory._create_session(conn, T0)
    conn.commit()
    memory.log_message(conn, session_id, "user", "Szia", "complete", created_at=T0)
    conn.close()

    class _HangingModels:
        async def generate_content(self, **kwargs):
            await asyncio.sleep(999)
            raise AssertionError("nem szabadna ide eljutni")  # pragma: no cover

    fake_client = SimpleNamespace(aio=SimpleNamespace(models=_HangingModels()))

    asyncio.run(extractor.process_session(fake_client, session_id, now=T0))

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT extracted_at, extract_attempts FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["extracted_at"] is None
    assert row["extract_attempts"] == 1


def test_process_session_unparseable_json_increments_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    conn = db.connect()
    session_id = memory._create_session(conn, T0)
    conn.commit()
    memory.log_message(conn, session_id, "user", "Szia", "complete", created_at=T0)
    conn.close()

    fake_client = _FakeClient(parsed=None)

    asyncio.run(extractor.process_session(fake_client, session_id, now=T0))

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT extracted_at, extract_attempts FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row["extracted_at"] is None
    assert row["extract_attempts"] == 1


def test_process_session_success_writes_facts_and_marks_extracted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    conn = db.connect()
    session_id = memory._create_session(conn, T0)
    conn.commit()
    msg_id = memory.log_message(
        conn, session_id, "user", "A kedvenc szinem a kek.", "complete", created_at=T0
    )
    conn.close()

    parsed = extractor.ExtractionResult(
        summary="A felhasznalo elmondta a kedvenc szinet.",
        facts=[
            extractor.ExtractedFact(
                action="new",
                kind="preference",
                content="A felhasznalo kedvenc szine a kek.",
                source_message_id=msg_id,
            )
        ],
    )
    fake_client = _FakeClient(parsed=parsed)

    asyncio.run(extractor.process_session(fake_client, session_id, now=T0))

    conn = db.connect()
    try:
        session = conn.execute(
            "SELECT extracted_at, summary FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        fact = conn.execute("SELECT content, kind FROM facts").fetchone()
    finally:
        conn.close()

    assert session["extracted_at"] == T0.isoformat()
    assert session["summary"] == "A felhasznalo elmondta a kedvenc szinet."
    assert fact["content"] == "A felhasznalo kedvenc szine a kek."
    assert fact["kind"] == "preference"


# --- run_cycle --------------------------------------------------------------


def test_run_cycle_closes_expired_and_processes_unprocessed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    conn = db.connect()
    stale_id = memory.resolve_session(conn, now=T0)
    memory.log_message(conn, stale_id, "user", "Regi uzenet", "complete", created_at=T0)

    ready_id = memory._create_session(conn, T0)
    conn.commit()
    memory.log_message(conn, ready_id, "user", "Uj uzenet", "complete", created_at=T0)
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (T0.isoformat(), ready_id))
    conn.commit()
    conn.close()

    parsed = extractor.ExtractionResult(summary="Kesz.", facts=[])
    fake_client = _FakeClient(parsed=parsed)

    now = T0 + timedelta(minutes=31, seconds=extractor.EXTRACT_GRACE_SECONDS + 10)
    asyncio.run(extractor.run_cycle(fake_client, now=now))

    conn = db.connect()
    try:
        stale = conn.execute(
            "SELECT ended_at, closed_by FROM sessions WHERE id = ?", (stale_id,)
        ).fetchone()
        ready = conn.execute(
            "SELECT extracted_at, summary FROM sessions WHERE id = ?", (ready_id,)
        ).fetchone()
        session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()

    assert stale["ended_at"] is not None
    assert stale["closed_by"] == "timeout"
    assert ready["extracted_at"] is not None
    assert ready["summary"] == "Kesz."
    assert session_count == 2
