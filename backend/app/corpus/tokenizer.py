"""Tokenizador BGE-M3 para conteo y delimitación de fragmentos.

Solo se carga el tokenizador, nunca los pesos del modelo. El tokenizador se usa
para contar tokens y obtener los desplazamientos de caracteres sobre el texto
original; no se generan representaciones vectoriales.
"""
from __future__ import annotations

from typing import Protocol, Sequence

from app.config import get_settings

TokenOffset = tuple[int, int]


class TokenizerUnavailableError(RuntimeError):
    """El tokenizador no está disponible en este proceso."""


class Tokenizer(Protocol):
    """Contrato mínimo que consume el fragmentador."""

    def tokenize_with_offsets(self, text: str) -> list[TokenOffset]: ...

    def count_tokens(self, text: str) -> int: ...


class BgeM3Tokenizer:
    """Envoltura perezosa del tokenizador de BGE-M3."""

    def __init__(self, model_id: str | None = None, cache_dir: str | None = None) -> None:
        settings = get_settings()
        self._model_id = model_id or settings.bge_m3_model_id
        self._cache_dir = cache_dir or settings.bge_m3_cache_dir
        self._tokenizer = None
        self._error: str | None = None

    def _load(self):
        if self._tokenizer is not None:
            return self._tokenizer
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:  # pragma: no cover - dependencia externa
            self._error = f"tokenizador no disponible: {exc.__class__.__name__}"
            raise TokenizerUnavailableError(self._error) from exc
        try:
            self._tokenizer = AutoTokenizer.from_pretrained(
                self._model_id,
                cache_dir=self._cache_dir,
            )
        except Exception as exc:  # noqa: BLE001 - frontera de dependencia externa
            self._error = f"{exc.__class__.__name__}: no se pudo cargar el tokenizador"
            raise TokenizerUnavailableError(self._error) from exc
        return self._tokenizer

    def tokenize_with_offsets(self, text: str) -> list[TokenOffset]:
        tokenizer = self._load()
        encoding = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        return [tuple(pair) for pair in encoding["offset_mapping"]]

    def count_tokens(self, text: str) -> int:
        return len(self.tokenize_with_offsets(text))


class FastTokenizer(Tokenizer):
    """Tokenizador rápido por offsets para pruebas y entornos sin el modelo.

    Divide por caracteres no alfabéticos preservando los desplazamientos exactos
    sobre el texto original; útil para verificar el fragmentador sin depender del
    modelo real.
    """

    def tokenize_with_offsets(self, text: str) -> list[TokenOffset]:
        offsets: list[TokenOffset] = []
        index = 0
        length = len(text)
        while index < length:
            if text[index].isspace():
                index += 1
                continue
            start = index
            while index < length and not text[index].isspace():
                index += 1
            offsets.append((start, index))
        return offsets

    def count_tokens(self, text: str) -> int:
        return len(self.tokenize_with_offsets(text))


def coerce_tokenizer(tokenizer: Tokenizer | Sequence[str]) -> Tokenizer:
    """Acepta un tokenizador o una secuencia de tokens precalculada."""
    if isinstance(tokenizer, Sequence) and not hasattr(tokenizer, "tokenize_with_offsets"):
        return _SequenceTokenizer(tokenizer)
    return tokenizer  # type: ignore[return-value]


class _SequenceTokenizer:
    def __init__(self, tokens: Sequence[str]) -> None:
        self._tokens = list(tokens)

    def tokenize_with_offsets(self, text: str) -> list[TokenOffset]:
        offsets: list[TokenOffset] = []
        cursor = 0
        for token in self._tokens:
            start = text.find(token, cursor)
            if start == -1:
                continue
            end = start + len(token)
            offsets.append((start, end))
            cursor = end
        return offsets

    def count_tokens(self, text: str) -> int:
        return len(self.tokenize_with_offsets(text))
