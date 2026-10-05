"""Consultas exactas sobre el corpus certificado y autorizado."""
from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.rag.indexing import EMBEDDING_DIMENSIONS, INDEX_CONTRACT_VERSION
from app.rag.models import CitationMetadata, RetrievedCandidate, RetrievalError
from app.security.models import AuthorizedDocumentScope


_CORPUS = """
WITH eligible_versions AS MATERIALIZED (
    SELECT v.id AS version_id, d.id_documento AS document_id,
           d.name AS document_name, d.document_type, d.document_date,
           cert.index_contract_version
      FROM app.document d
      JOIN app.document_version v ON v.id = d.active_version_id
                                 AND v.id_documento = d.id_documento
      JOIN app.document_index_certificate cert ON cert.document_version_id = v.id
      JOIN app.ingest_file source ON source.id::text = d.id_documento
                                  AND source.stored_object_id = v.stored_object_id
     WHERE d.invalidated_at IS NULL
       AND v.processing_state = 'LISTA'
       AND cert.index_contract_version = %(index_contract)s
       AND cert.embedding_model = 'BAAI/bge-m3'
       AND cert.embedding_dimensions = 1024
       AND cert.source_text_sha256 = sha256(convert_to(v.consolidated_text, 'UTF8'))
       AND source.state = 'COMPLETADO'
       AND source.technical_result = 'ACCEPTED'
       AND NOT EXISTS (
           SELECT 1 FROM app.quarantine_item q
            WHERE q.ingest_file_id = source.id AND q.state = 'Pendiente'
       )
       AND ({scope_predicate})
),
authorized_corpus AS MATERIALIZED (
    SELECT c.id AS fragment_id, c.content AS fragment_text, {embedding_projection}
           ev.document_id, ev.document_name, ev.document_type, ev.document_date,
           c.page_start, c.page_end, c.section, c.clause
      FROM eligible_versions ev
      JOIN app.document_chunk c ON c.document_version_id = ev.version_id
      JOIN app.document_chunk_index_receipt receipt ON receipt.fragment_id = c.id
     WHERE c.embedding IS NOT NULL
       AND vector_dims(c.embedding) = 1024
       AND receipt.index_contract_version = ev.index_contract_version
)
"""


def validate_query_vector(values: Sequence[float]) -> tuple[float, ...]:
    try:
        components = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        raise RetrievalError("Consulta vectorial no disponible") from None
    if (
        len(components) != EMBEDDING_DIMENSIONS
        or not all(isfinite(value) for value in components)
        or not any(value != 0.0 for value in components)
    ):
        raise RetrievalError("Consulta vectorial no disponible")
    return components


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(repr(value) for value in validate_query_vector(values)) + "]"


def _candidate(row: dict[str, object], rank: int, score: float) -> RetrievedCandidate:
    citation = CitationMetadata(
        document_id=str(row["document_id"]),
        document_name=str(row["document_name"]),
        document_type=str(row["document_type"]),
        document_date=row["document_date"] if isinstance(row["document_date"], date) else None,
        fragment_id=row["fragment_id"],
        page_start=row["page_start"],
        page_end=row["page_end"],
        section=row["section"],
        clause=row["clause"],
    )
    return RetrievedCandidate(row["fragment_id"], str(row["fragment_text"]), citation, rank, score)


class RetrievalRepository:
    """Usa siempre la conexión y el ámbito recibidos del servicio."""

    @staticmethod
    def _corpus(
        scope: AuthorizedDocumentScope, *, include_embedding: bool = True
    ) -> tuple[str, dict[str, object]]:
        if not isinstance(scope, AuthorizedDocumentScope):
            raise RetrievalError("Ámbito documental no válido")
        predicate, parameters = scope.database_predicate("d")
        return _CORPUS.format(
            scope_predicate=predicate,
            embedding_projection="c.embedding," if include_embedding else "",
        ), {
            **parameters, "index_contract": INDEX_CONTRACT_VERSION,
        }

    def vector_top10(
        self,
        connection: psycopg.Connection,
        scope: AuthorizedDocumentScope,
        query_embedding: Sequence[float],
    ) -> tuple[RetrievedCandidate, ...]:
        corpus, parameters = self._corpus(scope)
        sql = corpus + """
SELECT fragment_id, fragment_text, document_id, document_name,
       document_type, document_date, page_start, page_end, section, clause,
       embedding <=> %(query_vector)s::vector AS distance
  FROM authorized_corpus
 ORDER BY distance ASC, fragment_id ASC
 LIMIT 10
"""
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, {**parameters, "query_vector": _vector_literal(query_embedding)})
            rows = cursor.fetchall()
        return tuple(_candidate(row, rank, float(row["distance"])) for rank, row in enumerate(rows, 1))

    def _bm25_statement(self, scope: AuthorizedDocumentScope) -> tuple[str, dict[str, object]]:
        corpus, parameters = self._corpus(scope, include_embedding=False)
        sql = corpus + """,
terms_with_lengths AS MATERIALIZED (
    SELECT ac.fragment_id, t.normalized_lexeme,
           t.term_frequency::double precision AS tf,
           COALESCE(SUM(t.term_frequency) OVER (PARTITION BY ac.fragment_id), 0)::double precision AS dl
      FROM authorized_corpus ac
      LEFT JOIN app.chunk_term t ON t.fragment_id = ac.fragment_id
),
statistics AS MATERIALIZED (
    SELECT COUNT(*)::double precision AS n, AVG(dl)::double precision AS avgdl
      FROM (
          SELECT fragment_id, MAX(dl) AS dl
            FROM terms_with_lengths
           GROUP BY fragment_id
      ) per_fragment
),
term_frequencies AS MATERIALIZED (
    SELECT fragment_id, normalized_lexeme, tf, dl
      FROM terms_with_lengths
     WHERE normalized_lexeme = ANY(%(query_terms)s::text[])
),
document_frequency AS MATERIALIZED (
    SELECT normalized_lexeme, COUNT(*)::double precision AS df
      FROM term_frequencies GROUP BY normalized_lexeme
),
scores AS (
    SELECT tf.fragment_id,
           SUM(
             LN(1.0 + (stats.n - df.df + 0.5) / (df.df + 0.5))
             * (tf.tf * 2.2)
             / (tf.tf + 1.2 * (0.25 + 0.75 * tf.dl / stats.avgdl))
           ) AS score
      FROM term_frequencies tf
      JOIN document_frequency df USING (normalized_lexeme)
      CROSS JOIN statistics stats
     WHERE stats.n > 0 AND stats.avgdl > 0
     GROUP BY tf.fragment_id
)
SELECT ac.fragment_id, ac.fragment_text, ac.document_id, ac.document_name,
       ac.document_type, ac.document_date, ac.page_start, ac.page_end,
       ac.section, ac.clause, scores.score
  FROM scores JOIN authorized_corpus ac USING (fragment_id)
 WHERE scores.score > 0
 ORDER BY scores.score DESC, ac.fragment_id ASC
 LIMIT 10
"""
        return sql, parameters

    def bm25_top10(
        self,
        connection: psycopg.Connection,
        scope: AuthorizedDocumentScope,
        query_terms: Sequence[str],
    ) -> tuple[RetrievedCandidate, ...]:
        terms = tuple(sorted(set(query_terms)))
        if not terms:
            return ()
        sql, parameters = self._bm25_statement(scope)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, {**parameters, "query_terms": list(terms)})
            rows = cursor.fetchall()
        return tuple(_candidate(row, rank, float(row["score"])) for rank, row in enumerate(rows, 1))

    def final_fragments_still_authorized(
        self,
        connection: psycopg.Connection,
        scope: AuthorizedDocumentScope,
        fragment_ids: Sequence[UUID],
    ) -> bool:
        if not fragment_ids:
            return True
        corpus, parameters = self._corpus(scope, include_embedding=False)
        sql = corpus + """
SELECT count(*) AS total FROM authorized_corpus
 WHERE fragment_id = ANY(%(final_fragment_ids)s::uuid[])
"""
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(sql, {**parameters, "final_fragment_ids": list(fragment_ids)})
            row = cursor.fetchone()
        return row is not None and int(row["total"]) == len(set(fragment_ids))
