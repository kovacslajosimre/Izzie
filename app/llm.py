"""Gemini-hivasok kozos resze: kliens letrehozasa es hibaosztalyozas.

A chat es az extractor egyarant ezt hasznalja, ld. docs/llm.md.
"""

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

RETRY_ATTEMPTS = 3
RETRY_HTTP_STATUS_CODES = [500, 502, 503, 504]

_OVERLOADED_MESSAGE = "A modell most túlterhelt vagy nem elérhető. Próbáld újra egy kicsit később."
_QUOTA_MESSAGE = "Elérted a modell használati keretét. Próbáld újra később."
_GENERIC_MESSAGE = "Hiba történt a válasz generálása közben."


def create_client(api_key: str) -> genai.Client:
    """A kozos Gemini-kliens, rovid ujraprobalkozassal atmeneti (5xx) hibakra."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(
                attempts=RETRY_ATTEMPTS,
                http_status_codes=RETRY_HTTP_STATUS_CODES,
            )
        ),
    )


def _api_status_code(exc: BaseException) -> int | None:
    if isinstance(exc, genai_errors.APIError) and isinstance(exc.code, int):
        return exc.code
    return None


def is_transient_error(exc: BaseException) -> bool:
    """Igaz kulso, atmeneti hibara: 429/5xx API-hiba, idokorlat, halozati hiba."""
    code = _api_status_code(exc)
    if code is not None:
        return code == 429 or 500 <= code < 600
    return isinstance(exc, (TimeoutError, httpx.TransportError))


def describe_error(exc: BaseException) -> str:
    """A felhasznalonak szolo uzenet egy generalas kozbeni hibahoz."""
    code = _api_status_code(exc)
    if code is not None:
        if 500 <= code < 600:
            return _OVERLOADED_MESSAGE
        if code == 429:
            return _QUOTA_MESSAGE
    return _GENERIC_MESSAGE
