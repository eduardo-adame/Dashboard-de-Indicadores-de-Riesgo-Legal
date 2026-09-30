"""Pruebas puras del contrato único para el hash de consulta RAG."""
from __future__ import annotations

from hashlib import sha256

import pytest

from app.security.rag_query_hash import canonical_rag_query_sha256


def test_unicode_nfc_nfd_equivalent() -> None:
    assert canonical_rag_query_sha256("café") == canonical_rag_query_sha256("cafe\u0301")


@pytest.mark.parametrize("left,right", [("a\r\nb", "a\nb"), ("a\rb", "a\nb")])
def test_newline_forms_equivalent(left: str, right: str) -> None:
    assert canonical_rag_query_sha256(left) == canonical_rag_query_sha256(right)


def test_outer_whitespace_only_is_trimmed() -> None:
    assert canonical_rag_query_sha256("  a  b  ") == canonical_rag_query_sha256("a  b")
    assert canonical_rag_query_sha256("a  b") != canonical_rag_query_sha256("a b")


def test_unicode_hash_is_stable_32_bytes() -> None:
    expected = sha256("¿Qué ocurrió?".encode("utf-8")).digest()
    assert canonical_rag_query_sha256("¿Qué ocurrió?") == expected
    assert len(expected) == 32


@pytest.mark.parametrize("query", ["", " \t\r\n "])
def test_empty_after_strip_rejected(query: str) -> None:
    with pytest.raises(ValueError):
        canonical_rag_query_sha256(query)


def test_non_text_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_rag_query_sha256(None)  # type: ignore[arg-type]
