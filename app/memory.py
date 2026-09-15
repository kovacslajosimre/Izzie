"""Session-hozzarendeles es uzenetnaplozas a nyers naplohoz.

Csak iras: az 1. szelet resze, nincs kivonatolas, nincs visszakereses.
A sqlite3-hivasok szandekosan szinkronok es tiszta be/kimenetuek (kapcsolatot
es idopontot parameterkent kapjak) - ez teszi oket pytest-teszthetove; az
async wrapperek csomagoljak asyncio.to_thread-be a hivasi oldalon.
"""

import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from app.db import connect

SESSION_TIMEOUT_MINUTES = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _minutes_between(earlier: datetime, later: datetime) -> float:
    return (later - earlier).total_seconds() / 60


def resolve_session(conn: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    """Visszaadja a nyitott session id-jet.

    Ha nincs nyitott session, uj nyilik. Ha tobb is nyitva van (pl. egy
    korabbi osszeomlas utan), a legfrissebbnel regebbieket lezarja
    ('timeout'), fuggetlenul attol, hogy egyenileg tullepte-e a 30 percet -
    egyszerre egynel tobb nyitott session anomalia. Ha a legfrissebb is
    tullepte a 30 perces inaktivitast (az utolso hozza tartozo uzenettol,
    vagy uzenet hianyaban a session kezdetetol szamitva), az is lezarul,
    es uj session nyilik.
    """
    now = now or _now()
    rows = conn.execute(
        "SELECT id, started_at FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC"
    ).fetchall()

    if not rows:
        session_id = _create_session(conn, now)
        conn.commit()
        return session_id

    newest, *older = rows
    for row in older:
        _close_session(conn, row["id"], now)

    last_activity = _last_activity(conn, newest["id"], newest["started_at"])
    if _minutes_between(last_activity, now) < SESSION_TIMEOUT_MINUTES:
        conn.commit()
        return newest["id"]

    _close_session(conn, newest["id"], now)
    session_id = _create_session(conn, now)
    conn.commit()
    return session_id


def _last_activity(conn: sqlite3.Connection, session_id: int, started_at: str) -> datetime:
    row = conn.execute(
        "SELECT MAX(created_at) FROM messages WHERE session_id = ?", (session_id,)
    ).fetchone()
    value = row[0] or started_at
    return _parse(value)


def _create_session(conn: sqlite3.Connection, now: datetime) -> int:
    cur = conn.execute("INSERT INTO sessions (started_at) VALUES (?)", (_format(now),))
    return cur.lastrowid


def _close_session(conn: sqlite3.Connection, session_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ?, closed_by = 'timeout' WHERE id = ?",
        (_format(now), session_id),
    )


def log_message(
    conn: sqlite3.Connection,
    session_id: int,
    role: str,
    content: str,
    status: str,
    created_at: Optional[datetime] = None,
) -> int:
    created_at = created_at or _now()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, created_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, role, content, _format(created_at), status),
    )
    conn.commit()
    return cur.lastrowid


async def start_turn(user_message: str) -> int:
    """Session-feloldas + a user uzenet naplozasa. Visszaadja a session id-t."""

    def _work() -> int:
        conn = connect()
        try:
            session_id = resolve_session(conn)
            log_message(conn, session_id, "user", user_message, "complete")
            return session_id
        finally:
            conn.close()

    return await asyncio.to_thread(_work)


async def log_assistant_message(session_id: int, content: str, status: str) -> None:
    """Az assistant-valasz naplozasa. Ures tartalom eseten (complete vagy partial) nincs sor."""
    if not content.strip():
        return

    def _work() -> None:
        conn = connect()
        try:
            log_message(conn, session_id, "assistant", content, status)
        finally:
            conn.close()

    await asyncio.to_thread(_work)
