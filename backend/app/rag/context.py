"""Contexto estructurado formado únicamente con fragmentos autorizados."""
from __future__ import annotations

from app.rag.models import RankedFragment, StructuredContext


def build_structured_context(fragments: tuple[RankedFragment, ...]) -> StructuredContext:
    return StructuredContext(fragments=fragments)
