"""A GET /history vegpont: tokenes vedelem, limit-validacio, es hogy
mellekhatas nelkul fut (nem nyit es nem zar le sessiont).

A tobbi TestClient-es teszt mintajara (lasd tests/test_auth.py): a
db.DB_PATH-t tmp_path-ra allitjuk, mielott eloszor importaljuk az
app.main-t.
"""

from datetime import timedelta, timezone
from datetime import datetime as dt

from fastapi.testclient import TestClient

from app import auth, db, memory


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "izzie.db")
    from app import main

    return main, TestClient(main.app)


def _auth_headers():
    return {"Authorization": f"Bearer {auth.load_api_token()}"}


def test_history_without_authorization_header_is_401(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/history")

    assert response.status_code == 401


def test_history_returns_messages_in_order_with_default_limit(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    now = dt.now(timezone.utc)
    conn = db.connect()
    try:
        session_id = memory._create_session(conn, now)
        conn.commit()
        memory.log_message(conn, session_id, "user", "Szia", "complete", created_at=now)
        memory.log_message(
            conn,
            session_id,
            "assistant",
            "Szia, miben segithetek?",
            "complete",
            created_at=now + timedelta(seconds=1),
        )
    finally:
        conn.close()

    response = client.get("/history", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert [m["content"] for m in body["messages"]] == ["Szia", "Szia, miben segithetek?"]
    first = body["messages"][0]
    assert first["session_id"] == session_id
    assert first["role"] == "user"
    assert first["status"] == "complete"
    assert first["created_at"] == now.isoformat()


def test_history_limit_zero_is_422(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/history?limit=0", headers=_auth_headers())

    assert response.status_code == 422


def test_history_limit_201_is_422(tmp_path, monkeypatch):
    _, client = _client(tmp_path, monkeypatch)

    response = client.get("/history?limit=201", headers=_auth_headers())

    assert response.status_code == 422


def test_history_does_not_resolve_or_close_sessions(tmp_path, monkeypatch):
    main, client = _client(tmp_path, monkeypatch)

    # Egy lejart, meg nyitott session: az utolso aktivitasa 60 perccel
    # regebbi, mint a 30 perces idokorlat.
    stale_activity = dt.now(timezone.utc) - timedelta(minutes=60)
    conn = db.connect()
    try:
        session_id = memory._create_session(conn, stale_activity)
        conn.commit()
        memory.log_message(conn, session_id, "user", "regi uzenet", "complete", created_at=stale_activity)
    finally:
        conn.close()

    def _fail(*args, **kwargs):
        raise AssertionError("a /history nem hivhatja ezt")

    monkeypatch.setattr(main.memory, "resolve_session", _fail)
    monkeypatch.setattr(main.memory, "close_expired_sessions", _fail)

    response = client.get("/history", headers=_auth_headers())

    assert response.status_code == 200

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["ended_at"] is None


def test_history_db_error_returns_500_without_leaking_details(tmp_path, monkeypatch):
    main, client = _client(tmp_path, monkeypatch)

    async def _broken(limit):
        raise RuntimeError("titkos reszlet, ami nem kerulhet a valaszba")

    monkeypatch.setattr(main.memory, "load_recent_messages", _broken)

    response = client.get("/history", headers=_auth_headers())

    assert response.status_code == 500
    assert "titkos reszlet" not in response.text
