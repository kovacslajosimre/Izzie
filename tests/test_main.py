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

import pytest

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
        agen = main.generate("teszt", 42)
        first = await agen.__anext__()
        assert first == main.sse({"type": "token", "text": "Szia"})
        await agen.aclose()

    asyncio.run(scenario())

    assert logged == {"session_id": 42, "content": "Szia", "status": "partial"}


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
        agen = main.generate("teszt", 42)
        first = await agen.__anext__()
        assert first == main.sse({"type": "token", "text": "Szia"})
        with pytest.raises(asyncio.CancelledError):
            await agen.athrow(asyncio.CancelledError)

    asyncio.run(scenario())

    assert logged == {"session_id": 42, "content": "Szia", "status": "partial"}


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
        agen = main.generate("teszt", 7)
        await agen.aclose()  # meg az elso yield elott zarodik

    asyncio.run(scenario())

    assert calls == []
