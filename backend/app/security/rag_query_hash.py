"""Contrato canónico único del hash de consulta para operación RAG y auditoría."""
from __future__ import annotations

from hashlib import sha256
import unicodedata


def canonical_rag_query_sha256(query: str) -> bytes:
    if not isinstance(query, str):
        raise ValueError("RAG query must be text")
    canonical = unicodedata.normalize("NFC", query.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not canonical:
        raise ValueError("RAG query must not be empty")
    return sha256(canonical.encode("utf-8")).digest()
