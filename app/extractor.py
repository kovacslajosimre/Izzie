"""Hatter-extractor: lezart sessionok tenyekre es osszefoglalora bontasa.

Egyetlen LLM-hivas sessiononkent, strukturalt JSON valasszal
(response_mime_type="application/json" + response_schema). A kind/action
mezok a Pydantic-semaban szandekosan `str` tipusuak, nem Literal/enum: igy
egy ervenytelen ertek nem buktatja el a teljes valasz feldolgozasat, csak
azt az egy tetelt - a zart lista es az ismert action ellenorzese sajat
kodban, tetelenkent tortenik, a docs/memory.md "Validalas" szakasza szerint.

A tesztelheto mag (load_session_input, select_unprocessed_sessions,
_is_valid_fact, _validate_and_apply) szinkron, kapcsolatot parameterkent
kap. Az async process_session()/run_cycle() ezeket to_thread-be csomagolva,
sajat kapcsolatot nyitva-zarva hivja - a sqlite3.Connection sosem el tul
egy to_thread-hatart.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

from app import memory
from app.db import connect
from app.paths import resolve_path

logger = logging.getLogger(__name__)

EXTRACTOR_PROMPT_PATH = resolve_path("IZZIE_EXTRACTOR_PROMPT", "prompts/extractor.md")
EXTRACTOR_VERSION = "v2"
EXTRACTOR_MODEL = "gemini-3.6-flash"  # kulon konstans, ld. docs/memory.md
EXTRACT_GRACE_SECONDS = 120
MAX_EXTRACT_ATTEMPTS = 3

_KINDS = {"identity", "preference", "project", "relationship", "event"}
_ACTIONS = {"new", "supersede"}


class ExtractedFact(BaseModel):
    action: str
    kind: str
    content: str
    supersedes_id: Optional[int] = None
    source_message_id: int


class ExtractionResult(BaseModel):
    summary: str
    facts: list[ExtractedFact]


@dataclass
class SessionInput:
    messages: list[dict]
    facts: list[dict]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.isoformat()


def load_extractor_prompt(path: Optional[Path] = None) -> str:
    path = path or EXTRACTOR_PROMPT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Extractor prompt fajl nem talalhato: {path}")
    return path.read_text(encoding="utf-8")


def select_unprocessed_sessions(conn: sqlite3.Connection, now: Optional[datetime] = None) -> list[int]:
    """A meg fel nem dolgozott, lezart sessionok id-i, turelmi idovel."""
    now = now or _now()
    cutoff = _format(now - timedelta(seconds=EXTRACT_GRACE_SECONDS))
    rows = conn.execute(
        "SELECT id FROM sessions "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND extracted_at IS NULL AND extract_attempts < ? "
        "ORDER BY ended_at",
        (cutoff, MAX_EXTRACT_ATTEMPTS),
    ).fetchall()
    return [row["id"] for row in rows]


def load_session_input(conn: sqlite3.Connection, session_id: int) -> SessionInput:
    """A session extractor-bemenete: 'complete', nem redaktalt uzenetek es
    az osszes aktiv teny, mindketto id-vel."""
    messages = conn.execute(
        "SELECT id, role, content FROM messages "
        "WHERE session_id = ? AND status = 'complete' AND redacted_at IS NULL "
        "ORDER BY created_at, id",
        (session_id,),
    ).fetchall()
    facts = conn.execute(
        "SELECT id, kind, content FROM facts "
        "WHERE superseded_by IS NULL AND deleted_at IS NULL "
        "ORDER BY id"
    ).fetchall()
    return SessionInput(
        messages=[dict(row) for row in messages],
        facts=[dict(row) for row in facts],
    )


def _format_session_input(session_input: SessionInput) -> str:
    lines = ["## Beszélgetés", ""]
    for message in session_input.messages:
        speaker = "Felhasználó" if message["role"] == "user" else "Izzie"
        lines.append(f"[{message['id']}] {speaker}: {message['content']}")

    lines.append("")
    lines.append("## Jelenleg ismert tények")
    if session_input.facts:
        for fact in session_input.facts:
            lines.append(f"[{fact['id']}] ({fact['kind']}) {fact['content']}")
    else:
        lines.append("(nincs)")

    return "\n".join(lines)


async def _call_llm(client: genai.Client, session_input: SessionInput) -> ExtractionResult:
    prompt = load_extractor_prompt()
    input_text = _format_session_input(session_input)

    response = await client.aio.models.generate_content(
        model=EXTRACTOR_MODEL,
        contents=input_text,
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            response_schema=ExtractionResult,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    if response.parsed is None:
        raise ValueError("Az extractor valasza nem ertelmezheto JSON-kent.")
    return response.parsed


def _is_valid_fact(
    item: ExtractedFact,
    valid_message_ids: set[int],
    active_fact_ids: set[int],
    superseded_this_round: set[int],
) -> bool:
    if item.kind not in _KINDS:
        return False
    if item.action not in _ACTIONS:
        return False
    if not item.content.strip():
        return False
    if item.source_message_id not in valid_message_ids:
        return False
    if item.action == "supersede":
        if item.supersedes_id is None:
            return False
        if item.supersedes_id not in active_fact_ids:
            return False
        if item.supersedes_id in superseded_this_round:
            return False
    return True


def _get_pinned(conn: sqlite3.Connection, fact_id: int) -> int:
    row = conn.execute("SELECT pinned FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return row["pinned"] if row else 0


def _insert_fact(
    conn: sqlite3.Connection,
    kind: str,
    content: str,
    source_message_id: int,
    now: datetime,
    pinned: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO facts (kind, content, source_message_id, created_at, extractor_version, pinned) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (kind, content.strip(), source_message_id, _format(now), EXTRACTOR_VERSION, pinned),
    )
    return cur.lastrowid


def _mark_superseded(conn: sqlite3.Connection, old_id: int, new_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE facts SET superseded_by = ?, superseded_at = ? WHERE id = ?",
        (new_id, _format(now), old_id),
    )


def _validate_and_apply(
    conn: sqlite3.Connection,
    session_id: int,
    result: ExtractionResult,
    session_input: SessionInput,
    now: datetime,
) -> None:
    """Tetelenkent ellenorzi es beirja a tenyeket, majd frissiti a session
    osszefoglalojat. Nem commitol - a hivo felelossege. Ures/csak szokoz
    summary eseten a summary/summary_model/summarized_at NULL marad, hogy a
    previous_session_summary() ne adjon vissza ures blokkot."""
    valid_message_ids = {message["id"] for message in session_input.messages}
    active_fact_ids = {fact["id"] for fact in session_input.facts}
    superseded_this_round: set[int] = set()

    for item in result.facts:
        if not _is_valid_fact(item, valid_message_ids, active_fact_ids, superseded_this_round):
            logger.warning(
                "Extractor: ervenytelen teny kihagyva a(z) %s sessionben: %r", session_id, item
            )
            continue

        if item.action == "supersede":
            pinned = _get_pinned(conn, item.supersedes_id)
            new_id = _insert_fact(conn, item.kind, item.content, item.source_message_id, now, pinned)
            _mark_superseded(conn, item.supersedes_id, new_id, now)
            superseded_this_round.add(item.supersedes_id)
        else:
            _insert_fact(conn, item.kind, item.content, item.source_message_id, now, pinned=0)

    summary = result.summary.strip() or None
    summary_model = EXTRACTOR_MODEL if summary else None
    summarized_at = _format(now) if summary else None

    conn.execute(
        "UPDATE sessions SET summary = ?, summary_model = ?, summarized_at = ?, extracted_at = ? "
        "WHERE id = ?",
        (summary, summary_model, summarized_at, _format(now), session_id),
    )


def _record_failed_attempt(session_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE sessions SET extract_attempts = extract_attempts + 1 WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        attempts = conn.execute(
            "SELECT extract_attempts FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()[0]
        if attempts >= MAX_EXTRACT_ATTEMPTS:
            logger.warning(
                "Extractor: a(z) %s session harom sikertelen kiserlet utan kimarad", session_id
            )
    finally:
        conn.close()


async def process_session(client: genai.Client, session_id: int, now: Optional[datetime] = None) -> None:
    """Egy session feldolgozasa: bemenet betoltese, LLM-hivas, validalas,
    iras. Hiba eseten (API-hiba, ertelmezhetetlen JSON, iras kozbeni hiba)
    extract_attempts no, extracted_at ures marad."""
    now = now or _now()

    def _load_and_check() -> tuple[SessionInput, bool]:
        conn = connect()
        try:
            session_input = load_session_input(conn, session_id)
            if not any(message["role"] == "user" for message in session_input.messages):
                conn.execute(
                    "UPDATE sessions SET extracted_at = ? WHERE id = ?", (_format(now), session_id)
                )
                conn.commit()
                return session_input, True
            return session_input, False
        finally:
            conn.close()

    session_input, done = await asyncio.to_thread(_load_and_check)
    if done:
        return

    try:
        result = await _call_llm(client, session_input)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Extractor sikertelen kiserlet a(z) %s sessionnel: %s", session_id, exc)
        await asyncio.to_thread(_record_failed_attempt, session_id)
        return

    def _write() -> None:
        conn = connect()
        try:
            _validate_and_apply(conn, session_id, result, session_input, now)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    try:
        await asyncio.to_thread(_write)
    except Exception:
        logger.exception("Extractor iras sikertelen a(z) %s sessionnel", session_id)
        await asyncio.to_thread(_record_failed_attempt, session_id)


async def run_cycle(client: genai.Client, now: Optional[datetime] = None) -> None:
    """A hatterciklus egy kore: lejart session lezarasa uj nyitasa nelkul,
    majd a feldolgozatlan sessionok egyenkenti feldolgozasa."""
    now = now or _now()

    def _prepare() -> list[int]:
        conn = connect()
        try:
            memory.close_expired_sessions(conn, now)
            return select_unprocessed_sessions(conn, now)
        finally:
            conn.close()

    session_ids = await asyncio.to_thread(_prepare)
    for session_id in session_ids:
        await process_session(client, session_id, now)
