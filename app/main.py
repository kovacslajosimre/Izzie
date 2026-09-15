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

from app.persona import build_system_prompt, load_persona
from app.text import split_sentences

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="Izzie Brain")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"

# Induláskor egyszer betoltjuk, hogy hianyzo/hibas persona-fajl eseten
# az uvicorn azonnal, hangosan elszalljon, ne csak egy /chat hivasnal.
load_persona()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


def sse(event: dict) -> str:
    """Egy SSE event szerializalasa."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def generate(message: str) -> AsyncIterator[str]:
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

            yield sse({"type": "token", "text": piece})

            buffer += piece
            sentences, buffer = split_sentences(buffer)
            for sentence in sentences:
                yield sse({"type": "sentence", "index": index, "text": sentence})
                index += 1

        tail = buffer.strip()
        if tail:
            yield sse({"type": "sentence", "index": index, "text": tail})

        yield sse({"type": "done"})

    except genai_errors.APIError:
        logger.exception("Gemini API hiba a /chat streamben")
        yield sse({"type": "error", "message": "Hiba tortent a valasz generalasa kozben."})
    except Exception:  # noqa: BLE001
        logger.exception("Varatlan hiba a /chat streamben")
        yield sse({"type": "error", "message": "Hiba tortent a valasz generalasa kozben."})


@app.post("/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        generate(req.message),
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
