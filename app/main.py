import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors

load_dotenv()

app = FastAPI(title="Izzie Brain")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    response: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        result = client.models.generate_content(
            model=MODEL_NAME,
            contents=req.message,
        )
    except genai_errors.APIError as e:
        raise HTTPException(status_code=502, detail=f"Gemini hiba: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Váratlan hiba: {e}")
    return ChatResponse(response=result.text)


@app.get("/health")
def health():
    return {"status": "ok"}