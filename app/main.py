import asyncio
import json
import logging
import os
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app import db, memory
from app.persona import build_system_prompt, load_persona
from app.text import split_sentences

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="Izzie Brain")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"

# Induláskor egyszer betoltjuk/lefuttatjuk, hogy hianyzo/hibas persona-fajl
# vagy DB-migracio eseten az uvicorn azonnal, hangosan elszalljon, ne csak
# egy /chat hivasnal.
load_persona()
db.connect().close()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


def sse(event: dict) -> str:
    """Egy SSE event szerializalasa."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _log_partial_safely(session_id: int, full_text: str) -> None:
    """Reszleges valasz naplozasa megszakitas kozben.

    A GeneratorExit/CancelledError kezelese soran az aktualis task tovabbi
    await-jei ismet megszakitast kaphatnak (pl. egy anyio cancel scope
    eseten, amit a StreamingResponse hasznal) - az asyncio.shield() a
    naplozast egy kulon taskba teszi, ami a hivo megszakitasatol
    fuggetlenul lefut, akkor is, ha a shield-re varakozas maga megszakad.
    """
    try:
        await asyncio.shield(memory.log_assistant_message(session_id, full_text, "partial"))
    except asyncio.CancelledError:
        pass


async def generate(message: str, session_id: int) -> AsyncIterator[str]:
    """A modell valaszat token- es mondatszintu eventekre bontja.

    Event tipusok:
      {"type": "token",    "text": "..."}              - inkrementalis, a UI-nak
      {"type": "sentence", "index": 0, "text": "..."}  - kesz mondat, a TTS-nek
      {"type": "done"}
      {"type": "error",    "message": "..."}
    """
    persona = load_persona()
    system_prompt = build_system_prompt(persona)

    buffer = ""
    full_text = ""
    index = 0

    try:
        stream = await client.aio.models.generate_content_stream(
            model=MODEL_NAME,
            contents=message,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )

        async for chunk in stream:
            piece = chunk.text
            if not piece:
                continue

            full_text += piece
            yield sse({"type": "token", "text": piece})

            buffer += piece
            sentences, buffer = split_sentences(buffer)
            for sentence in sentences:
                yield sse({"type": "sentence", "index": index, "text": sentence})
                index += 1

        tail = buffer.strip()
        if tail:
            yield sse({"type": "sentence", "index": index, "text": tail})

        await memory.log_assistant_message(session_id, full_text, "complete")
        yield sse({"type": "done"})

    except GeneratorExit:
        # A kliens lelepett kozben (a StreamingResponse lezarja a generatort).
        # Csak naplozunk, ujabb SSE eventet nem probalunk kikuldeni.
        logger.info("Kliens megszakitotta a /chat kapcsolatot, reszleges valasz naplozasa")
        await _log_partial_safely(session_id, full_text)
        raise
    except asyncio.CancelledError:
        # A kliens lelepeset az uvicorn/anyio taszk-megszakitassal is
        # kezelheti - ekkor GeneratorExit helyett ez jon. Csak naplozunk,
        # ujabb SSE eventet nem probalunk kikuldeni.
        logger.info("A /chat taszk megszakitva, reszleges valasz naplozasa")
        await _log_partial_safely(session_id, full_text)
        raise
    except genai_errors.APIError:
        logger.exception("Gemini API hiba a /chat streamben")
        await memory.log_assistant_message(session_id, full_text, "partial")
        yield sse({"type": "error", "message": "Hiba tortent a valasz generalasa kozben."})
    except Exception:  # noqa: BLE001
        logger.exception("Varatlan hiba a /chat streamben")
        await memory.log_assistant_message(session_id, full_text, "partial")
        yield sse({"type": "error", "message": "Hiba tortent a valasz generalasa kozben."})


@app.post("/chat")
async def chat(req: ChatRequest):
    session_id = await memory.start_turn(req.message)
    return StreamingResponse(
        generate(req.message, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx ne pufferelje, ha egyszer moge kerul
        },
    )


@app.get("/health")
def health():
    return {"status": "ok"}
