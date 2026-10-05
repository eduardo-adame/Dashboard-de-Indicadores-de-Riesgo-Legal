"""Cliente HTTPS acotado para la generación RAG con Groq."""
from __future__ import annotations

import json
from http.client import IncompleteRead
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_CHAT_URL = f"{GROQ_BASE_URL}/chat/completions"
GROQ_USER_AGENT = "RiesgoLegalMVP/0.1"


class GroqFailure(RuntimeError):
    """Fallo seguro de la dependencia externa, sin contenido de la solicitud."""

    def __init__(self, cause_code: str) -> None:
        super().__init__("La generación no pudo completarse")
        self.cause_code = cause_code


class _RejectRedirects(HTTPRedirectHandler):
    """Impide reenviar la clave o los fragmentos a cualquier URL redirigida."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_opener():
    return build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )


class GroqProvider:
    """Envía solo mensajes aprobados al único origen HTTPS permitido."""

    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_seconds: float,
        max_request_bytes: int = 60_000,
        opener=None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0 or max_request_bytes <= 0 or max_request_bytes > 60_000:
            raise ValueError("Configuración Groq no válida")
        self._api_key = api_key or ""
        self._timeout_seconds = timeout_seconds
        self._max_request_bytes = max_request_bytes
        self._opener = opener or _default_opener()
        self._sleep = sleep

    def generate(
        self,
        messages: Sequence[dict[str, str]],
        *,
        before_send: Callable[[], None],
    ) -> str:
        """Revalida antes de cada POST, incluso al reintentar un 429."""
        if not self._api_key:
            raise GroqFailure("PROVIDER_UNAVAILABLE")
        payload = json.dumps(
            {
                "model": GROQ_MODEL,
                "messages": list(messages),
                "max_completion_tokens": 4096,
                "include_reasoning": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > self._max_request_bytes:
            raise GroqFailure("CONTEXT_LIMIT_EXCEEDED")
        for attempt in range(3):
            before_send()
            request = Request(
                GROQ_CHAT_URL,
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": GROQ_USER_AGENT,
                },
            )
            try:
                with self._opener.open(request, timeout=self._timeout_seconds) as response:
                    if response.status != 200 or response.geturl() != GROQ_CHAT_URL:
                        raise GroqFailure("PROVIDER_UNAVAILABLE")
                    raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise GroqFailure("INVALID_PROVIDER_OUTPUT")
                parsed = json.loads(raw)
                value = parsed["choices"][0]["message"]["content"]
                if not isinstance(value, str) or not value.strip():
                    raise GroqFailure("INVALID_PROVIDER_OUTPUT")
                return value.strip()
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt == 2:
                        raise GroqFailure("PROVIDER_RATE_LIMITED") from None
                    self._sleep(min(2**attempt, 2))
                    continue
                raise GroqFailure("PROVIDER_UNAVAILABLE") from None
            except (socket.timeout, TimeoutError):
                raise GroqFailure("PROVIDER_TIMEOUT") from None
            except URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise GroqFailure("PROVIDER_TIMEOUT") from None
                raise GroqFailure("PROVIDER_UNAVAILABLE") from None
            except (OSError, IncompleteRead):
                raise GroqFailure("PROVIDER_UNAVAILABLE") from None
            except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError):
                raise GroqFailure("INVALID_PROVIDER_OUTPUT") from None
        raise GroqFailure("PROVIDER_RATE_LIMITED")
