"""Persistencia PostgreSQL conflict-safe del índice documental certificado."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.rag.indexing import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    INDEX_CONTRACT_VERSION,
    MAX_CHUNK_TOKENS,
    OVERLAP_TOKENS,
    IndexChunk,
    IndexingInvariantError,
)


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


class IndexRepository:
    """Acceso mínimo a las tablas físicas de corpus y certificación."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    def persist_certified_index(
        self,
        *,
        document_version_id: UUID,
        consolidated_text: str,
        source_text_sha256: bytes,
        chunks: tuple[IndexChunk, ...],
    ) -> tuple[tuple[UUID, ...], bool]:
        with self.transaction() as connection:
            version = connection.execute(
                """SELECT id, consolidated_text, content_sha256, processing_state
                     FROM app.document_version WHERE id = %s FOR UPDATE""",
                (document_version_id,),
            ).fetchone()
            if version is None:
                raise IndexingInvariantError("versión documental inexistente")
            if (
                version["consolidated_text"] != consolidated_text
                or bytes(version["content_sha256"]) != source_text_sha256
                or version["processing_state"] not in {"PROCESANDO", "LISTA"}
            ):
                raise IndexingInvariantError(
                    "la fuente documental cambió durante la inferencia"
                )

            existing_certificate = connection.execute(
                """SELECT index_contract_version, embedding_model,
                          embedding_dimensions, chunk_size_tokens,
                          chunk_overlap_tokens, expected_chunk_count,
                          embedded_chunk_count, lexical_processed_chunk_count,
                          zero_lexeme_chunk_count, source_text_sha256
                     FROM app.document_index_certificate
                    WHERE document_version_id = %s""",
                (document_version_id,),
            ).fetchone()
            if existing_certificate is not None:
                self._assert_existing_certificate(
                    existing_certificate,
                    source_text_sha256=source_text_sha256,
                    chunks=chunks,
                )
                return (
                    self._assert_existing_chunks(
                        connection, document_version_id, chunks
                    ),
                    True,
                )

            for chunk in chunks:
                vector_literal = _vector_literal(chunk.embedding)
                connection.execute(
                    """INSERT INTO app.document_chunk
                       (id, document_version_id, ordinal, content, token_count,
                        page_start, page_end, section, clause, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                       ON CONFLICT DO NOTHING""",
                    (
                        chunk.fragment_id,
                        document_version_id,
                        chunk.ordinal,
                        chunk.content,
                        chunk.token_count,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.section,
                        chunk.clause,
                        vector_literal,
                    ),
                )
                persisted = connection.execute(
                    """SELECT id, document_version_id, ordinal, content, token_count,
                              page_start, page_end, section, clause,
                              embedding = %s::vector AS embedding_matches
                         FROM app.document_chunk
                        WHERE document_version_id = %s AND ordinal = %s""",
                    (vector_literal, document_version_id, chunk.ordinal),
                ).fetchone()
                self._assert_chunk(persisted, document_version_id, chunk)

                for term in chunk.terms:
                    connection.execute(
                        """INSERT INTO app.chunk_term
                           (fragment_id, normalized_lexeme, term_frequency)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (
                            chunk.fragment_id,
                            term.normalized_lexeme,
                            term.term_frequency,
                        ),
                    )
                persisted_terms = connection.execute(
                    """SELECT normalized_lexeme, term_frequency
                         FROM app.chunk_term WHERE fragment_id = %s
                         ORDER BY normalized_lexeme""",
                    (chunk.fragment_id,),
                ).fetchall()
                observed_terms = tuple(
                    (row["normalized_lexeme"], row["term_frequency"])
                    for row in persisted_terms
                )
                expected_terms = tuple(
                    (term.normalized_lexeme, term.term_frequency)
                    for term in chunk.terms
                )
                if observed_terms != expected_terms:
                    raise IndexingInvariantError("términos léxicos incompatibles")

                connection.execute(
                    """INSERT INTO app.document_chunk_index_receipt
                       (fragment_id, index_contract_version, lexical_term_count,
                        processed_at)
                       VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT DO NOTHING""",
                    (chunk.fragment_id, INDEX_CONTRACT_VERSION, len(chunk.terms)),
                )
                receipt = connection.execute(
                    """SELECT index_contract_version, lexical_term_count
                         FROM app.document_chunk_index_receipt
                        WHERE fragment_id = %s""",
                    (chunk.fragment_id,),
                ).fetchone()
                if receipt is None or (
                    receipt["index_contract_version"] != INDEX_CONTRACT_VERSION
                    or receipt["lexical_term_count"] != len(chunk.terms)
                ):
                    raise IndexingInvariantError("receipt léxico incompatible")

            connection.execute(
                """UPDATE app.document_version
                      SET processing_state = 'LISTA',
                          completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
                    WHERE id = %s""",
                (document_version_id,),
            )
            zero_lexeme_count = sum(not chunk.terms for chunk in chunks)
            connection.execute(
                """INSERT INTO app.document_index_certificate
                   (document_version_id, index_contract_version, embedding_model,
                    embedding_dimensions, chunk_size_tokens, chunk_overlap_tokens,
                    expected_chunk_count, embedded_chunk_count,
                    lexical_processed_chunk_count, zero_lexeme_chunk_count,
                    source_text_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    document_version_id,
                    INDEX_CONTRACT_VERSION,
                    EMBEDDING_MODEL,
                    EMBEDDING_DIMENSIONS,
                    MAX_CHUNK_TOKENS,
                    OVERLAP_TOKENS,
                    len(chunks),
                    len(chunks),
                    len(chunks),
                    zero_lexeme_count,
                    source_text_sha256,
                ),
            )
            return tuple(chunk.fragment_id for chunk in chunks), False

    @staticmethod
    def _assert_chunk(row, document_version_id: UUID, chunk: IndexChunk) -> None:
        if row is None or any(
            (
                row["id"] != chunk.fragment_id,
                row["document_version_id"] != document_version_id,
                row["ordinal"] != chunk.ordinal,
                row["content"] != chunk.content,
                row["token_count"] != chunk.token_count,
                row["page_start"] != chunk.page_start,
                row["page_end"] != chunk.page_end,
                row["section"] != chunk.section,
                row["clause"] != chunk.clause,
                not row["embedding_matches"],
            )
        ):
            raise IndexingInvariantError("identidad de fragmento incompatible")

    @staticmethod
    def _assert_existing_certificate(
        row,
        *,
        source_text_sha256: bytes,
        chunks: tuple[IndexChunk, ...],
    ) -> None:
        expected = {
            "index_contract_version": INDEX_CONTRACT_VERSION,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimensions": EMBEDDING_DIMENSIONS,
            "chunk_size_tokens": MAX_CHUNK_TOKENS,
            "chunk_overlap_tokens": OVERLAP_TOKENS,
            "expected_chunk_count": len(chunks),
            "embedded_chunk_count": len(chunks),
            "lexical_processed_chunk_count": len(chunks),
            "zero_lexeme_chunk_count": sum(not chunk.terms for chunk in chunks),
        }
        if (
            any(row[key] != value for key, value in expected.items())
            or bytes(row["source_text_sha256"]) != source_text_sha256
        ):
            raise IndexingInvariantError("certificado concurrente incompatible")

    def _assert_existing_chunks(
        self,
        connection: psycopg.Connection,
        document_version_id: UUID,
        chunks: tuple[IndexChunk, ...],
    ) -> tuple[UUID, ...]:
        total = connection.execute(
            "SELECT count(*) FROM app.document_chunk WHERE document_version_id = %s",
            (document_version_id,),
        ).fetchone()["count"]
        if total != len(chunks):
            raise IndexingInvariantError("cantidad de fragmentos certificada incompatible")

        fragment_ids: list[UUID] = []
        for chunk in chunks:
            vector_literal = _vector_literal(chunk.embedding)
            row = connection.execute(
                """SELECT id, document_version_id, ordinal, content, token_count,
                          page_start, page_end, section, clause,
                          embedding = %s::vector AS embedding_matches
                     FROM app.document_chunk
                    WHERE document_version_id = %s AND ordinal = %s""",
                (vector_literal, document_version_id, chunk.ordinal),
            ).fetchone()
            self._assert_chunk(row, document_version_id, chunk)

            terms = connection.execute(
                """SELECT normalized_lexeme, term_frequency
                     FROM app.chunk_term
                    WHERE fragment_id = %s
                    ORDER BY normalized_lexeme""",
                (chunk.fragment_id,),
            ).fetchall()
            observed_terms = tuple(
                (term["normalized_lexeme"], term["term_frequency"])
                for term in terms
            )
            expected_terms = tuple(
                (term.normalized_lexeme, term.term_frequency)
                for term in chunk.terms
            )
            if observed_terms != expected_terms:
                raise IndexingInvariantError("términos certificados incompatibles")

            receipt = connection.execute(
                """SELECT index_contract_version, lexical_term_count
                     FROM app.document_chunk_index_receipt
                    WHERE fragment_id = %s""",
                (chunk.fragment_id,),
            ).fetchone()
            if receipt is None or (
                receipt["index_contract_version"] != INDEX_CONTRACT_VERSION
                or receipt["lexical_term_count"] != len(chunk.terms)
            ):
                raise IndexingInvariantError("receipt certificado incompatible")
            fragment_ids.append(chunk.fragment_id)
        return tuple(fragment_ids)

    def version_is_active(self, document_version_id: UUID) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT EXISTS(
                       SELECT 1 FROM app.document d
                       JOIN app.document_version v
                         ON v.id_documento = d.id_documento
                        AND v.id = d.active_version_id
                       JOIN app.document_index_certificate cert
                         ON cert.document_version_id = v.id
                      WHERE v.id = %s AND d.invalidated_at IS NULL
                   ) AS active""",
                (document_version_id,),
            ).fetchone()
            return bool(row["active"])

    def active_certified_fragment_count(self, document_version_id: UUID) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT count(*) AS total
                     FROM app.document_chunk c
                     JOIN app.document_version v ON v.id = c.document_version_id
                     JOIN app.document d
                       ON d.id_documento = v.id_documento
                      AND d.active_version_id = v.id
                     JOIN app.document_index_certificate cert
                       ON cert.document_version_id = v.id
                    WHERE v.id = %s AND d.invalidated_at IS NULL
                      AND v.processing_state = 'LISTA'""",
                (document_version_id,),
            ).fetchone()
            return int(row["total"])
