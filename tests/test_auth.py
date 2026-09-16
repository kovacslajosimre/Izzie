"""app/auth.py es a /chat, /health vegpontok hitelesites- es CORS-viselkedese.

A tobbi teszt mintajara: a db.DB_PATH-t tmp_path-ra allitjuk, mielott eloszor
importaljuk az app.main-t - igy a modulszintu induláskori db.connect() nem a
valodi fejlesztoi adatbazist erinti. A Gemini-hivas mockolva van a sikeres
tokenes esetben, ahol tenylegesen lefutna.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, db


async def _fake_stream(chunks):
    for chunk in chunks:
        yield chunk
        await asyncio.sleep(0)


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")
    from app import main

    return main, TestClient(main.app)


def test_chat_without_authorization_header_is_401(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.post("/chat", json={"message": "Szia"})

    assert response.status_code == 401


def test_chat_with_wrong_token_is_401(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/chat", json={"message": "Szia"}, headers={"Authorization": "Bearer rossz-token"}
    )

    assert response.status_code == 401


def test_chat_with_correct_token_succeeds(tmp_path, monkeypatch):
    main, client = _client(tmp_path, monkeypatch)

    async def fake_generate_content_stream(**kwargs):
        return _fake_stream([_Chunk("Szia")])

    monkeypatch.setattr(
        main.client.aio.models, "generate_content_stream", fake_generate_content_stream
    )

    token = auth.load_api_token()
    response = client.post(
        "/chat", json={"message": "Szia"}, headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_health_does_not_require_token(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/health")

    assert response.status_code == 200


def test_cors_preflight_passes_without_token(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.options(
        "/chat",
        headers={
            "Origin": "http://localhost:1420",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:1420"


def test_401_response_has_cors_headers(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/chat",
        json={"message": "Szia"},
        headers={"Origin": "http://localhost:1420"},
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "http://localhost:1420"


def test_load_api_token_raises_if_missing(monkeypatch):
    monkeypatch.delenv("IZZIE_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError):
        auth.load_api_token()


def test_load_api_token_raises_if_blank(monkeypatch):
    monkeypatch.setenv("IZZIE_API_TOKEN", "   ")

    with pytest.raises(RuntimeError):
        auth.load_api_token()
