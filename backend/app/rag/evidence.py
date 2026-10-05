"""Fusión RRF y clasificación determinista de evidencia."""
from __future__ import annotations

from decimal import Decimal

from app.rag.models import EvidenceState, RankedFragment, RetrievedCandidate


def fuse_candidates(
    vector: tuple[RetrievedCandidate, ...],
    bm25: tuple[RetrievedCandidate, ...],
) -> tuple[RankedFragment, ...]:
    """Conserva rangos originales y limita el contexto a cinco fragmentos."""
    combined: dict[object, tuple[RetrievedCandidate, int | None, int | None]] = {}
    for candidate in vector:
        combined[candidate.fragment_id] = (candidate, candidate.rank, None)
    for candidate in bm25:
        previous = combined.get(candidate.fragment_id)
        combined[candidate.fragment_id] = (
            previous[0] if previous else candidate,
            previous[1] if previous else None,
            candidate.rank,
        )
    ranked = []
    for candidate, vector_rank, bm25_rank in combined.values():
        score = sum(
            (Decimal(1) / Decimal(60 + rank) for rank in (vector_rank, bm25_rank) if rank is not None),
            Decimal(0),
        )
        ranked.append(RankedFragment(
            candidate.fragment_id,
            candidate.citation.document_id,
            candidate.fragment_text,
            candidate.citation,
            vector_rank,
            bm25_rank,
            score,
        ))
    ranked.sort(key=lambda item: (
        -item.rrf_score,
        min(rank for rank in (item.vector_rank, item.bm25_rank) if rank is not None),
        item.vector_rank is None,
        item.vector_rank if item.vector_rank is not None else 0,
        item.bm25_rank is None,
        item.bm25_rank if item.bm25_rank is not None else 0,
        str(item.fragment_id),
    ))
    return tuple(ranked[:5])


def classify_evidence(
    vector: tuple[RetrievedCandidate, ...],
    bm25: tuple[RetrievedCandidate, ...],
    final: tuple[RankedFragment, ...],
) -> EvidenceState:
    if not vector and not bm25:
        return EvidenceState.SIN_EVIDENCIA
    if any(
        item.citation.complete and (
            (item.vector_rank is not None and item.vector_rank <= 3)
            or (item.bm25_rank is not None and item.bm25_rank <= 3)
        )
        for item in final
    ):
        return EvidenceState.EVIDENCIA_SUFICIENTE
    return EvidenceState.EVIDENCIA_INSUFICIENTE
