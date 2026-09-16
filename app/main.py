import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app import db, extractor, memory
from app.persona import build_system_prompt, load_persona
from app.text import split_sentences

load_dotenv()

logger = logging.getLogger(__name__)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"
BACKGROUND_INTERVAL_SECONDS = 300

# Induláskor egyszer betoltjuk/lefuttatjuk, hogy hianyzo/hibas persona- vagy
# extractor-prompt-fajl, vagy DB-migracio eseten az uvicorn azonnal, hangosan
# elszalljon, ne csak egy /chat hivasnal.
load_persona()
extractor.load_extractor_prompt()
db.connect().close()


async def _background_cycle_loop() -> None:
    """Ötpercenként lezárja a lejárt sessiont és feldolgozza a lezártakat.

    Az első kör induláskor azonnal lefut, nem 5 perc múlva - fejlesztés
    közben az uvicorn --reload minden mentésnél újraindít, egy késleltetett
    első kör így akár órákig sem futna le. Egy kör hibája logolódik, de a
    ciklust nem állítja le.
    """
    while True:
        try:
            await extractor.run_cycle(client)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Hatterciklus hiba, folytatas a kovetkezo korben")
        await asyncio.sleep(BACKGROUND_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_background_cycle_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Izzie Brain", lifespan=lifespan)


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


def to_gemini_contents(history: list[dict]) -> list[types.Content]:
    """Az elozmenyt a Gemini tobbfordulos `contents` formatumara alakitja.

    A naplo 'assistant' szerepkoret 'model'-re forditja, az egymast koveto
    azonos szerepkoru uzeneteket egy forduloba vonja ossze (a naplo ezt nem
    garantalja: ures valasz nem kerul naplozasra, igy ket user uzenet is
    kovetheti egymast), es a lista elejerol eldobja a 'model' fordulokat -
    a Gemini valtakozo szerepkoroket var, user-rel kezdve.
    """
    merged: list[tuple[str, str]] = []
    for entry in history:
        role = "model" if entry["role"] == "assistant" else "user"
        content = entry["content"]
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n" + content)
        else:
            merged.append((role, content))

    while merged and merged[0][0] == "model":
        merged.pop(0)

    return [types.Content(role=role, parts=[types.Part(text=content)]) for role, content in merged]


async def generate(message: str, session_id: int) -> AsyncIterator[str]:
    """A modell valaszat token- es mondatszintu eventekre bontja.

    Event tipusok:
      {"type": "token",    "text": "..."}              - inkrementalis, a UI-nak
      {"type": "sentence", "index": 0, "text": "..."}  - kesz mondat, a TTS-nek
      {"type": "done"}
      {"type": "error",    "message": "..."}
    """
    persona = load_persona()

    buffer = ""
    full_text = ""
    index = 0

    try:
        context = await memory.load_turn_context(session_id, message)
        system_prompt = build_system_prompt(persona, {"memory": context["memory"]})
        contents = to_gemini_contents(context["history"])

        stream = await client.aio.models.generate_content_stream(
            model=MODEL_NAME,
            contents=contents,
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
