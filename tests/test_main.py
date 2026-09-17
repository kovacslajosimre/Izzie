"""app.main.generate() teszt: kliens-megszakitas eseten (GeneratorExit vagy
asyncio.CancelledError - attol fuggoen, hogy az ASGI szerver hogyan zarja le
a streamet) a reszleges valasz status='partial'-kent naplozodik, es a
generator nem probal ujabb SSE eventet kikuldeni a lezaras utan.

A modult csak itt importaljuk, es elotte a DB_PATH-t egy tmp fajlra allitjuk,
igy a modul-szintu induláskori db.connect() nem a valodi fejlesztoi
adatbazist erinti. A Gemini-hivas es a memory.log_assistant_message mockolva
van - nincs valodi LLM-hivas, es a GEMINI_API_KEY-t a conftest.py mar
beallitja, valodi .env nelkul is.
"""

import asyncio

import httpx
import pytest
from google.genai import errors as genai_errors

from app import db


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


async def _fake_stream(chunks):
    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)


def test_generate_logs_partial_on_client_disconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main  # elso import hasznalja a fenti DB_PATH-t induláskor

    logged = {}

    async def fake_log_assistant_message(session_id, content, status):
        logged["session_id"] = session_id
        logged["content"] = content
        logged["status"] = status

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream([_Chunk("Szia"), _Chunk(" vilag")])

    monkeypatch.setattr(main.memory, "log_assistant_message", fake_log_assistant_message)
    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main.memory.start_turn("teszt")
        agen = main.generate("teszt", session_id)
        first = await agen.__anext__()
        assert first == main.sse({"type": "token", "text": "Szia"})
        await agen.aclose()
        return session_id

    session_id = asyncio.run(scenario())

    assert logged == {"session_id": session_id, "content": "Szia", "status": "partial"}


def test_generate_logs_partial_on_task_cancellation(tmp_path, monkeypatch):
    """Ha a kliens lelepeset a szerver taszk-megszakitassal jelzi (nem
    aclose()-zal), a generatorba asyncio.CancelledError erkezik - ezt is
    'partial'-kent kell naplozni, es a CancelledError-t tovabb kell adni."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    logged = {}

    async def fake_log_assistant_message(session_id, content, status):
        logged["session_id"] = session_id
        logged["content"] = content
        logged["status"] = status

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream([_Chunk("Szia"), _Chunk(" vilag")])

    monkeypatch.setattr(main.memory, "log_assistant_message", fake_log_assistant_message)
    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main.memory.start_turn("teszt")
        agen = main.generate("teszt", session_id)
        first = await agen.__anext__()
        assert first == main.sse({"type": "token", "text": "Szia"})
        with pytest.raises(asyncio.CancelledError):
            await agen.athrow(asyncio.CancelledError)
        return session_id

    session_id = asyncio.run(scenario())

    assert logged == {"session_id": session_id, "content": "Szia", "status": "partial"}


def test_generate_logs_nothing_if_disconnect_before_any_token(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    calls = []

    async def fake_log_assistant_message(session_id, content, status):
        calls.append((session_id, content, status))

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream([_Chunk("Szia")])

    monkeypatch.setattr(main.memory, "log_assistant_message", fake_log_assistant_message)
    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main.memory.start_turn("teszt")
        agen = main.generate("teszt", session_id)
        await agen.aclose()  # meg az elso yield elott zarodik

    asyncio.run(scenario())

    assert calls == []


async def _fake_stream_hangs():
    await asyncio.sleep(999)
    yield _Chunk("sosem erkezik meg")  # pragma: no cover


async def _fake_stream_hangs_after_one_chunk():
    yield _Chunk("Szia")
    await asyncio.sleep(999)
    yield _Chunk("sosem erkezik meg")  # pragma: no cover


def test_generate_sends_error_on_first_chunk_timeout_and_logs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    monkeypatch.setattr(main, "FIRST_CHUNK_TIMEOUT_SECONDS", 0.05)

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream_hangs()

    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main.memory.start_turn("teszt")
        events = [event async for event in main.generate("teszt", session_id)]
        return session_id, events

    session_id, events = asyncio.run(scenario())

    assert events == [
        main.sse(
            {
                "type": "error",
                "message": "Izzie most nem kapott választ a modelltől. Próbáld újra.",
            }
        )
    ]

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT content, status FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_generate_sends_error_on_chunk_idle_timeout_and_logs_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    monkeypatch.setattr(main, "CHUNK_IDLE_TIMEOUT_SECONDS", 0.05)

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream_hangs_after_one_chunk()

    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main.memory.start_turn("teszt")
        events = [event async for event in main.generate("teszt", session_id)]
        return session_id, events

    session_id, events = asyncio.run(scenario())

    assert events == [
        main.sse({"type": "token", "text": "Szia"}),
        main.sse(
            {
                "type": "error",
                "message": "Izzie most nem kapott választ a modelltől. Próbáld újra.",
            }
        ),
    ]

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT content, status FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    assert [(row["content"], row["status"]) for row in rows] == [("Szia", "partial")]


def test_to_gemini_contents_maps_merges_and_drops_leading_model(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    history = [
        {"role": "assistant", "content": "korabban lezarult valasz, ami elveszett elozmeny"},
        {"role": "user", "content": "h1"},
        {"role": "user", "content": "h2"},
        {"role": "assistant", "content": "a1"},
    ]

    contents = main.to_gemini_contents(history)

    assert contents == [
        main.types.Content(role="user", parts=[main.types.Part(text="h1\n\nh2")]),
        main.types.Content(role="model", parts=[main.types.Part(text="a1")]),
    ]


def test_to_gemini_contents_empty_history(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    assert main.to_gemini_contents([]) == []


def test_generate_sends_full_history_and_core_profile_to_gemini(tmp_path, monkeypatch):
    """Bekotesi teszt: a contents a teljes (aktualis session-beli) elozmenyt
    tartalmazza helyes szerepkorokkel, utolso elemkent az aktualis uzenettel,
    a system_instruction pedig a pinned teny szoveget."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    conn = db.connect()
    conn.execute(
        "INSERT INTO facts (kind, content, created_at, extractor_version, pinned) "
        "VALUES ('identity', 'A felhasználó neve Lajos.', '2026-01-01T10:00:00+00:00', 'manual', 1)"
    )
    conn.commit()
    session_id = main.memory.resolve_session(conn)
    main.memory.log_message(conn, session_id, "user", "Korábbi kérdés", "complete")
    main.memory.log_message(conn, session_id, "assistant", "Korábbi válasz", "complete")
    conn.close()

    captured = {}

    async def fake_generate_content_stream(**kwargs):
        captured["contents"] = kwargs["contents"]
        captured["config"] = kwargs["config"]
        return _fake_stream([_Chunk("Valasz")])

    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        current_session_id = await main.memory.start_turn("Mostani kérdés")
        assert current_session_id == session_id  # ugyanaz a nyitott session

        agen = main.generate("Mostani kérdés", current_session_id)
        async for _ in agen:
            pass

    asyncio.run(scenario())

    contents = captured["contents"]
    assert [(c.role, c.parts[0].text) for c in contents] == [
        ("user", "Korábbi kérdés"),
        ("model", "Korábbi válasz"),
        ("user", "Mostani kérdés"),
    ]
    assert "A felhasználó neve Lajos." in captured["config"].system_instruction


# --- atmeneti/nem-atmeneti hibak (docs/llm.md, 1. szelet) --------------------


def _assert_single_error_event_and_no_assistant_row(main_module, events, session_id, expected_message):
    assert events == [main_module.sse({"type": "error", "message": expected_message})]

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT content, status FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    assert rows == []


def test_generate_sends_overloaded_message_on_503(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main as main_module

    async def fake_generate_content_stream(**kwargs):
        raise genai_errors.ServerError(503, {"message": "overloaded", "status": "UNAVAILABLE"})

    monkeypatch.setattr(
        main_module.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main_module.memory.start_turn("teszt")
        events = [event async for event in main_module.generate("teszt", session_id)]
        return session_id, events

    session_id, events = asyncio.run(scenario())

    _assert_single_error_event_and_no_assistant_row(
        main_module,
        events,
        session_id,
        "A modell most túlterhelt vagy nem elérhető. Próbáld újra egy kicsit később.",
    )


def test_generate_sends_generic_message_on_400(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main as main_module

    async def fake_generate_content_stream(**kwargs):
        raise genai_errors.ClientError(400, {"message": "bad request", "status": "INVALID_ARGUMENT"})

    monkeypatch.setattr(
        main_module.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main_module.memory.start_turn("teszt")
        events = [event async for event in main_module.generate("teszt", session_id)]
        return session_id, events

    session_id, events = asyncio.run(scenario())

    _assert_single_error_event_and_no_assistant_row(
        main_module, events, session_id, "Hiba történt a válasz generálása közben."
    )


def test_generate_sends_generic_message_on_network_error(tmp_path, monkeypatch):
    """A halozati hiba (pl. kapcsolat megszakad) nem APIError, az altalanos
    except Exception agba fut - annak is a warning/exception szetvalasztast
    es a describe_error()-t kell hasznalnia (docs/llm.md)."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main as main_module

    async def fake_generate_content_stream(**kwargs):
        raise httpx.ConnectError("kapcsolat megszakadt")

    monkeypatch.setattr(
        main_module.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    async def scenario():
        session_id = await main_module.memory.start_turn("teszt")
        events = [event async for event in main_module.generate("teszt", session_id)]
        return session_id, events

    session_id, events = asyncio.run(scenario())

    _assert_single_error_event_and_no_assistant_row(
        main_module, events, session_id, "Hiba történt a válasz generálása közben."
    )


def test_background_cycle_loop_continues_after_run_cycle_error(tmp_path, monkeypatch):
    """Ha az elso kor hibara fut, a ciklus logolja es folytatja - eljut a
    masodik korig. A sleep ki van valtva, hogy a teszt ne varjon valoban."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")

    from app import main

    calls = []

    async def fake_run_cycle(client):
        calls.append(client)
        if len(calls) == 1:
            raise RuntimeError("szimulalt hiba az elso korben")

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(main.extractor, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    async def scenario():
        task = asyncio.create_task(main._background_cycle_loop())
        for _ in range(1000):
            if len(calls) >= 2:
                break
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(calls) >= 2
