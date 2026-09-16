"""Session-hozzarendeles, uzenetnaplozas, elozmeny es tenyvisszakereses.

A sqlite3-hivasok szandekosan szinkronok es tiszta be/kimenetuek (kapcsolatot
es idopontot parameterkent kapjak) - ez teszi oket pytest-teszthetove; az
async wrapperek csomagoljak asyncio.to_thread-be a hivasi oldalon.

Szolgaltatofuggetlen marad: {"role": ..., "content": ...} listat ad vissza,
google.genai-t nem importal. A Gemini-specifikus szerepkor-atalakitas
(to_gemini_contents) az app/main.py-ban el.
"""

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import connect

logger = logging.getLogger(__name__)

SESSION_TIMEOUT_MINUTES = 30
HISTORY_MESSAGE_LIMIT = 20
CORE_PROFILE_WARN_THRESHOLD = 20

_STOPWORDS = {
    "hogy", "nem", "van", "egy", "és", "meg", "mit", "ami", "azt", "csak",
    "már", "még", "volt", "lesz", "kell", "nekem", "neked",
}
# Unicode betuk es szamjegyek - nem csak a magyar ekezetek, mert pl. a regi
# Windows-kodlapok miatt eloforduló idegen ekezetek (pl. "Straße", "kõnyvtár")
# is szohatar nelkul szakadnanak szet; a szamjegyek gepnevekhez/verziokhoz kellenek.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_WORD_LENGTH = 3
_MIN_COMMON_PREFIX = 4


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


def get_history(
    conn: sqlite3.Connection, session_id: int, limit: int = HISTORY_MESSAGE_LIMIT
) -> list[dict]:
    """Az aktualis session utolso `limit` uzenete, idorendben.

    A redaktalt uzenetek (redacted_at IS NOT NULL) kimaradnak. A 'partial'
    uzenetek bent maradnak - a felhasznalo latta oket, a folytonossaghoz
    hozzatartoznak.
    """
    rows = conn.execute(
        "SELECT role, content FROM messages "
        "WHERE session_id = ? AND redacted_at IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


@dataclass
class Fact:
    id: int
    kind: str
    content: str
    pinned: bool
    created_at: str


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        kind=row["kind"],
        content=row["content"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
    )


def core_profile(conn: sqlite3.Connection) -> list[Fact]:
    """Aktiv, pinned = 1 tenyek, kind szerint csoportositva.

    Nincs kemeny plafon, de a mag-profilnak kicsinek kell maradnia - ha
    CORE_PROFILE_WARN_THRESHOLD folott van, warning a logba.
    """
    rows = conn.execute(
        "SELECT id, kind, content, created_at, pinned FROM facts "
        "WHERE pinned = 1 AND superseded_by IS NULL AND deleted_at IS NULL "
        "ORDER BY kind, created_at"
    ).fetchall()
    facts = [_row_to_fact(row) for row in rows]
    if len(facts) > CORE_PROFILE_WARN_THRESHOLD:
        logger.warning(
            "core_profile %d tenyt tartalmaz (varhato hatar: %d)",
            len(facts),
            CORE_PROFILE_WARN_THRESHOLD,
        )
    return facts


def _tokenize(text: str) -> list[str]:
    words = _WORD_RE.findall(text.lower())
    return [w for w in words if len(w) >= _MIN_WORD_LENGTH and w not in _STOPWORDS]


def _match(a: str, b: str) -> bool:
    """Szandekosan durva szotovezes: rovidebb a hosszabb prefixe, vagy a
    kozos prefixuk legalabb _MIN_COMMON_PREFIX karakter."""
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer.startswith(shorter):
        return True
    common = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common += 1
    return common >= _MIN_COMMON_PREFIX


def retrieve(conn: sqlite3.Connection, query: str, k: int = 5) -> list[Fact]:
    """Aktiv, nem pinned tenyek kulcsszo-egyezes szerint rangsorolva.

    Pontszam: hany kulonbozo query-szonak van egyezese a teny szovegeben.
    A 0 pontos teny nem kerul vissza. Ha a query csak stopwordokbol es/vagy
    3 karakternel rovidebb szavakbol all, ures listat ad, nem hibat.
    """
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return []

    rows = conn.execute(
        "SELECT id, kind, content, created_at, pinned FROM facts "
        "WHERE pinned = 0 AND superseded_by IS NULL AND deleted_at IS NULL"
    ).fetchall()

    scored = []
    for row in rows:
        fact_tokens = _tokenize(row["content"])
        score = sum(
            1 for qt in query_tokens if any(_match(qt, ft) for ft in fact_tokens)
        )
        if score > 0:
            scored.append((score, row["created_at"], _row_to_fact(row)))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [fact for _, _, fact in scored[:k]]


def format_memory(core: list[Fact], relevant: list[Fact]) -> str:
    """A mag-profilbol es a visszakeresett tenyekbol osszerakja a promptba
    kerulo memoria-blokkot. Ures bemenetre ures sztring."""
    parts = []
    if core:
        lines = "\n".join(f"- {fact.content}" for fact in core)
        parts.append(f"A felhasználóról:\n{lines}")
    if relevant:
        lines = "\n".join(f"- {fact.content}" for fact in relevant)
        parts.append(f"Ami most releváns lehet:\n{lines}")
    return "\n\n".join(parts)


async def load_turn_context(session_id: int, user_message: str) -> dict:
    """Egy kapcsolatban beolvassa az elozmenyt, a mag-profilt es a talalatokat.

    Visszaadja: {"history": [...], "memory": "..."}. A `generate()` ezt a
    try blokkon belul hivja, hogy DB-hiba eseten is error event menjen ki.
    """

    def _work() -> dict:
        conn = connect()
        try:
            history = get_history(conn, session_id)
            core = core_profile(conn)
            relevant = retrieve(conn, user_message)
            return {"history": history, "memory": format_memory(core, relevant)}
        finally:
            conn.close()

    return await asyncio.to_thread(_work)


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
