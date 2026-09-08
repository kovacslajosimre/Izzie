import os

from fastapi import FastAPI
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai

load_dotenv()

app = FastAPI(title= "Izzie Brain")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL_NAME = "gemini-3.6-flash"

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    response: str

@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    result = client.models.generate_content(
        model=MODEL_NAME,
        contents=req.message,
    )
    return ChatResponse(response=result.text)

@app.get("/health")
def health():
    return {"status": "ok"}