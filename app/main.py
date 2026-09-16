import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, Field

from app import auth, db, extractor, memory
from app.persona import build_system_prompt, load_persona
from app.text import split_sentences

load_dotenv()

logger = logging.getLogger(__name__)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"
BACKGROUND_INTERVAL_SECONDS = 300
FIRST_CHUNK_TIMEOUT_SECONDS = 30
CHUNK_IDLE_TIMEOUT_SECONDS = 30

# Induláskor egyszer betoltjuk/lefuttatjuk, hogy hianyzo/hibas persona- vagy
# extractor-prompt-fajl, hianyzo/ures API-token, vagy DB-migracio eseten az
# uvicorn azonnal, hangosan elszalljon, ne csak egy /chat hivasnal.
load_persona()
extractor.load_extractor_prompt()
auth.load_api_token()
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

_DEFAULT_CORS_ORIGINS = ["http://localhost:1420", "http://tauri.localhost"]


def _load_cors_origins() -> list[str]:
    value = os.getenv("IZZIE_CORS_ORIGINS")
    if not value:
        return _DEFAULT_CORS_ORIGINS
    return [origin.strip() for origin in value.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_load_cors_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

# Minden vegpont ezen a routeren at vedett - egy uj vegpont igy nem maradhat
# veletlenul token nelkul. Kivetel a /health, az kozvetlenul az app-on el.
protected_router = APIRouter(dependencies=[Depends(auth.require_token)])


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


async def _next_chunk_or_none(stream_iter):
    """StopAsyncIteration -> None, hogy a hivo ciklus egyszeruen zarhasson."""
    try:
        return await stream_iter.__anext__()
    except StopAsyncIteration:
        return None


async def _close_stream_safely(stream) -> None:
    """A Gemini-stream lezarasa idokorlat utan, hogy ne maradjon nyitva a
    kapcsolat a hatterben."""
    if stream is None:
        return
    try:
        await stream.aclose()
    except Exception:  # noqa: BLE001
        logger.exception("Hiba a Gemini-stream lezarasa kozben idokorlat utan")


async def _handle_stream_timeout(
    stream, session_id: int, full_text: str, timeout_kind: str, timeout_seconds: float
) -> str:
    """Kozos ag a ket idokorlat-esetre: stream lezarasa, reszleges valasz
    naplozasa, es a kliensnek kikuldendo error-esemeny elokeszitese. Igy a
    ket eset (elso darab / darabok kozott) nem tud szetcsuszni."""
    logger.warning(
        "Gemini nem valaszolt idoben a /chat streamben (%s, idokorlat: %ss)",
        timeout_kind,
        timeout_seconds,
    )
    await _close_stream_safely(stream)
    await memory.log_assistant_message(session_id, full_text, "partial")
    return sse({"type": "error", "message": "Izzie most nem kapott választ a modelltől. Próbáld újra."})


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
    stream = None

    try:
        context = await memory.load_turn_context(session_id, message)
        system_prompt = build_system_prompt(persona, {"memory": context["memory"]})
        contents = to_gemini_contents(context["history"])

        try:
            async with asyncio.timeout(FIRST_CHUNK_TIMEOUT_SECONDS):
                stream = await client.aio.models.generate_content_stream(
                    model=MODEL_NAME,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    ),
                )
                stream_iter = stream.__aiter__()
                chunk = await _next_chunk_or_none(stream_iter)
        except TimeoutError:
            yield await _handle_stream_timeout(
                stream, session_id, full_text, "elso darab", FIRST_CHUNK_TIMEOUT_SECONDS
            )
            return

        while chunk is not None:
            piece = chunk.text
            if piece:
                full_text += piece
                yield sse({"type": "token", "text": piece})

                buffer += piece
                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    yield sse({"type": "sentence", "index": index, "text": sentence})
                    index += 1

            try:
                async with asyncio.timeout(CHUNK_IDLE_TIMEOUT_SECONDS):
                    chunk = await _next_chunk_or_none(stream_iter)
            except TimeoutError:
                yield await _handle_stream_timeout(
                    stream, session_id, full_text, "ket adatdarab kozott", CHUNK_IDLE_TIMEOUT_SECONDS
                )
                return

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
        yield sse({"type": "error", "message": "Hiba történt a válasz generálása közben."})
    except Exception:  # noqa: BLE001
        logger.exception("Varatlan hiba a /chat streamben")
        await memory.log_assistant_message(session_id, full_text, "partial")
        yield sse({"type": "error", "message": "Hiba történt a válasz generálása közben."})


@protected_router.post("/chat")
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


app.include_router(protected_router)


@app.get("/health")
def health():
    return {"status": "ok"}
