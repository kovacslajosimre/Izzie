import os
import sqlite3

import pytest

# app.main modulszinten kotelezonek veszi a GEMINI_API_KEY-t (a genai.Client
# letrehozasahoz), es load_dotenv()-et hiv, ami nem irja felul a mar
# beallitott env varokat. Igy a tesztek valodi .env / kulcs nelkul, friss
# klonon is lefutnak - a mockolt Gemini-hivasokhoz a kulcs erteke lenyegtelen.
os.environ.setdefault("GEMINI_API_KEY", "test-key-not-a-real-secret")

from app import db


@pytest.fixture
def conn():
    """Migralt, memoriaban elo sqlite kapcsolat - nem erinti a valodi DB-fajlt."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    db._migrate(connection)
    yield connection
    connection.close()
