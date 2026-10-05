"""Pruebas del transporte Groq sin filtrar secretos ni datos documentales."""
from __future__ import annotations

import json
from http.client import IncompleteRead
import socket
from urllib.error import HTTPError, URLError

import pytest

from app.config import Settings
from app.rag.provider import GROQ_CHAT_URL, GROQ_USER_AGENT, GroqFailure, GroqProvider, _RejectRedirects


class _Response:
    status = 200

    def __init__(self, content: str = "Respuesta [E1]", reasoning: str | None = None) -> None:
        self.content = content
        self.reasoning = reasoning

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return GROQ_CHAT_URL

    def read(self, _size):
        return json.dumps({"choices": [{"message": {"content": self.content, "reasoning": self.reasoning}}]}).encode()


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _messages():
    return ({"role": "system", "content": "Solo evidencia"}, {"role": "user", "content": "Texto sintético"})


def test_groq_key_is_masked_in_settings_representation() -> None:
    settings = Settings(_env_file=None, groq_api_key="clave-sintetica")
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == "clave-sintetica"
    assert "clave-sintetica" not in repr(settings)


def test_provider_sends_only_to_fixed_https_origin_with_timeout() -> None:
    opener = _Opener([_Response()])
    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=7, opener=opener)
    assert provider.generate(_messages(), before_send=lambda: None) == "Respuesta [E1]"
    request, timeout = opener.requests[0]
    assert request.full_url == GROQ_CHAT_URL
    assert timeout == 7
    assert request.get_header("User-agent") == GROQ_USER_AGENT
    payload = json.loads(request.data)
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["include_reasoning"] is False
    assert b"Texto sint" in request.data


def test_provider_uses_final_content_not_separate_reasoning() -> None:
    provider = GroqProvider(
        api_key="clave-sintetica", timeout_seconds=7,
        opener=_Opener([_Response("Respuesta final [E1]", "Razón interna no presentable")]),
    )
    assert provider.generate(_messages(), before_send=lambda: None) == "Respuesta final [E1]"


def test_429_retries_twice_and_revalidates_before_every_post() -> None:
    error = HTTPError(GROQ_CHAT_URL, 429, "rate", {}, None)
    opener = _Opener([error, error, _Response()])
    checks = []
    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=opener, sleep=lambda delay: None)
    assert provider.generate(_messages(), before_send=lambda: checks.append(True)) == "Respuesta [E1]"
    assert len(checks) == len(opener.requests) == 3


def test_revocation_between_429_attempts_prevents_second_post() -> None:
    opener = _Opener([HTTPError(GROQ_CHAT_URL, 429, "rate", {}, None)])
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("revocado")

    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=opener, sleep=lambda delay: None)
    with pytest.raises(PermissionError):
        provider.generate(_messages(), before_send=check)
    assert calls == 2
    assert len(opener.requests) == 1


@pytest.mark.parametrize("code", [302, 307, 308])
def test_redirect_is_not_followed_or_retried(code) -> None:
    assert _RejectRedirects().redirect_request(None, None, code, "redirect", {}, "https://other.invalid") is None
    opener = _Opener([HTTPError(GROQ_CHAT_URL, code, "redirect", {"Location": "https://other.invalid"}, None)])
    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=opener)
    with pytest.raises(GroqFailure) as error:
        provider.generate(_messages(), before_send=lambda: None)
    assert error.value.cause_code == "PROVIDER_UNAVAILABLE"
    assert len(opener.requests) == 1
    assert "clave-sintetica" not in str(error.value)


def test_timeout_and_invalid_provider_output_are_safe() -> None:
    timeout = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=_Opener([URLError(socket.timeout())]))
    with pytest.raises(GroqFailure) as error:
        timeout.generate(_messages(), before_send=lambda: None)
    assert error.value.cause_code == "PROVIDER_TIMEOUT"
    invalid = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=_Opener([_Response("")]))
    with pytest.raises(GroqFailure) as error:
        invalid.generate(_messages(), before_send=lambda: None)
    assert error.value.cause_code == "INVALID_PROVIDER_OUTPUT"


def test_missing_key_and_oversized_request_fail_before_post() -> None:
    opener = _Opener([])
    with pytest.raises(GroqFailure):
        GroqProvider(api_key=None, timeout_seconds=1, opener=opener).generate(_messages(), before_send=lambda: None)
    with pytest.raises(GroqFailure) as error:
        GroqProvider(api_key="clave-sintetica", timeout_seconds=1, max_request_bytes=10, opener=opener).generate(
            _messages(), before_send=lambda: None
        )
    assert error.value.cause_code == "CONTEXT_LIMIT_EXCEEDED"
    assert opener.requests == []


@pytest.mark.parametrize("failure", [ConnectionResetError("synthetic"), BrokenPipeError("synthetic")])
def test_transport_failure_is_a_safe_provider_failure(failure: OSError) -> None:
    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=_Opener([failure]))
    with pytest.raises(GroqFailure) as error:
        provider.generate(_messages(), before_send=lambda: None)
    assert error.value.cause_code == "PROVIDER_UNAVAILABLE"
    assert "synthetic" not in str(error.value)


def test_truncated_response_is_a_safe_provider_failure() -> None:
    class Truncated(_Response):
        def read(self, _size):
            raise IncompleteRead(b"partial", 100)

    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=_Opener([Truncated()]))
    with pytest.raises(GroqFailure) as error:
        provider.generate(_messages(), before_send=lambda: None)
    assert error.value.cause_code == "PROVIDER_UNAVAILABLE"


def test_exhausted_rate_limit_is_safe_and_revalidates_each_attempt() -> None:
    opener = _Opener([HTTPError(GROQ_CHAT_URL, 429, "rate", {}, None) for _ in range(3)])
    checks = []
    provider = GroqProvider(api_key="clave-sintetica", timeout_seconds=1, opener=opener, sleep=lambda _: None)
    with pytest.raises(GroqFailure) as error:
        provider.generate(_messages(), before_send=lambda: checks.append(True))
    assert error.value.cause_code == "PROVIDER_RATE_LIMITED"
    assert len(checks) == len(opener.requests) == 3


def test_request_limit_cannot_exceed_approved_maximum() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, groq_max_request_bytes=60_001)
    with pytest.raises(ValueError):
        GroqProvider(api_key="clave-sintetica", timeout_seconds=1, max_request_bytes=60_001)
