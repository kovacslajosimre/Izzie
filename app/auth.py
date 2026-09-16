"""Bearer token hitelesites minden vedett vegponthoz.

FastAPI dependency, nem middleware: a bongeszo CORS-elokerese (OPTIONS)
fejlec nelkul erkezik, es middleware eseten ez is elutasitasra kerulne -
igy a kliens csak egy semmitmondo halozati hibat latna a valodi 401 helyett.
"""

import os
import secrets
from typing import Optional

from fastapi import Header, HTTPException

_token: Optional[str] = None


def load_api_token() -> str:
    """Beolvassa az IZZIE_API_TOKEN env vart. Hianyzo vagy csak szokozokbol
    allo ertek eseten RuntimeError - az alkalmazas nem indul el csendben
    nyitott vegponttal."""
    global _token
    token = os.environ.get("IZZIE_API_TOKEN", "")
    if not token.strip():
        raise RuntimeError("Az IZZIE_API_TOKEN env var hianyzik vagy ures.")
    _token = token
    return token


async def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """401-et dob, ha az Authorization fejlec hianyzik vagy a token hibas."""
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Hianyzo vagy hibas Authorization fejlec.")
    token = authorization[len(prefix):]
    if _token is None or not secrets.compare_digest(token, _token):
        raise HTTPException(status_code=401, detail="Ervenytelen token.")
