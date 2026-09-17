"""app.llm tesztek: hibaosztalyozas es a kozos ujraprobalkozasi konstansok.

Nincs valodi halozati/API hivas - a genai_errors peldanyokat kezzel epitjuk
fel a kivant status kodokkal.
"""

import httpx
import pytest
from google.genai import errors as genai_errors

from app import llm


def _api_error(cls, code):
    return cls(code, {"message": "hiba", "status": "HIBA"})


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_is_transient_error_true_for_5xx(code):
    assert llm.is_transient_error(_api_error(genai_errors.ServerError, code)) is True


def test_is_transient_error_true_for_429():
    assert llm.is_transient_error(_api_error(genai_errors.ClientError, 429)) is True


@pytest.mark.parametrize("code", [400, 403])
def test_is_transient_error_false_for_4xx_non_429(code):
    assert llm.is_transient_error(_api_error(genai_errors.ClientError, code)) is False


def test_is_transient_error_true_for_timeout_error():
    assert llm.is_transient_error(TimeoutError("nincs valasz")) is True


def test_is_transient_error_true_for_network_error():
    assert llm.is_transient_error(httpx.ConnectError("kapcsolat megszakadt")) is True


def test_is_transient_error_false_for_other_exception():
    assert llm.is_transient_error(ValueError("valami mas")) is False


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_describe_error_overloaded_message_for_5xx(code):
    assert (
        llm.describe_error(_api_error(genai_errors.ServerError, code))
        == "A modell most túlterhelt vagy nem elérhető. Próbáld újra egy kicsit később."
    )


def test_describe_error_quota_message_for_429():
    assert (
        llm.describe_error(_api_error(genai_errors.ClientError, 429))
        == "Elérted a modell használati keretét. Próbáld újra később."
    )


@pytest.mark.parametrize(
    "exc",
    [
        _api_error(genai_errors.ClientError, 400),
        _api_error(genai_errors.ClientError, 403),
        TimeoutError("nincs valasz"),
        httpx.ConnectError("kapcsolat megszakadt"),
        ValueError("valami mas"),
    ],
)
def test_describe_error_generic_message_otherwise(exc):
    assert llm.describe_error(exc) == "Hiba történt a válasz generálása közben."


def test_retry_options_constants():
    assert llm.RETRY_ATTEMPTS == 3
    assert llm.RETRY_HTTP_STATUS_CODES == [500, 502, 503, 504]
