"""Persona betoltese es a system prompt osszerakasa.

Szandekosan kulon van a /chat logikatol. Ez az a reteg, amit
sokszor fogsz piszkalni, es nem akarod, hogy kozben a
memoria-kezeleshez kelljen hozzanyulni.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PERSONA_PATH = Path(os.getenv("IZZIE_PERSONA", "persona/izzie.yaml"))

_LENGTH_HINT = {
    "rovid": "Roviden valaszolj, altalaban nehany mondatban.",
    "kozepes": "Kozepes hosszan valaszolj.",
    "reszletes": "Reszletesen valaszolj, ha a kerdes megkivanja.",
}

_ADDRESS_HINT = {
    "tegez": "Tegezd a felhasznalot.",
    "magaz": "Magazd a felhasznalot.",
}


@dataclass
class Persona:
    name: str = "Izzie"
    language: str = "hu"
    address: str = "tegez"
    response_length: str = "rovid"
    style: str = ""
    self_description: str = ""
    extra: str = ""


def load_persona(path: Optional[Path] = None) -> Persona:
    path = path or PERSONA_PATH
    if not path.exists():
        return Persona()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    known = {f.name for f in Persona.__dataclass_fields__.values()}
    return Persona(**{k: v for k, v in data.items() if k in known and v is not None})


def build_system_prompt(persona: Persona, context: Optional[dict] = None) -> str:
    """Osszerakja a system promptot personabol + futasideju kontextusbol.

    A `context` egyelore ures. Ide fog bekerulni kesobb a pontos ido,
    a memoria-talalatok es a naptar. Azert van mar most itt, hogy
    a hivasi felulet ne valtozzon, amikor bejon a Memory modul.
    """
    parts = [persona.self_description.strip()]

    if persona.style.strip():
        parts.append("Stilus:\n" + persona.style.strip())

    hints = [
        _ADDRESS_HINT.get(persona.address, ""),
        _LENGTH_HINT.get(persona.response_length, ""),
    ]
    if persona.language == "hu":
        hints.append("Magyarul valaszolj.")
    parts.append(" ".join(h for h in hints if h))

    if persona.extra and persona.extra.strip():
        parts.append(persona.extra.strip())

    for block in ("time", "memory", "calendar"):
        value = (context or {}).get(block)
        if value:
            parts.append(f"{block.capitalize()}:\n{value}")

    return "\n\n".join(p for p in parts if p)
