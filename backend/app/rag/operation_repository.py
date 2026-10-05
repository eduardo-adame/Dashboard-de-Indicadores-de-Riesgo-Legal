"""Persistencia terminal de operaciones RAG en la transacción del llamador."""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from psycopg.rows import dict_row

from app.rag.models import RankedFragment


class RagOperationRepository:
    """No abre conexiones ni confirma transacciones por cuenta propia."""

    @staticmethod
    def lock_operation(connection: psycopg.Connection, operation_id: UUID) -> None:
        # El lock de transacción solo protege la identidad lógica, no la llamada a Groq.
        key = int.from_bytes(operation_id.bytes[:8], byteorder="big", signed=True)
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))

    @staticmethod
    def find(connection: psycopg.Connection, operation_id: UUID) -> dict | None:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT id, user_id, session_id, query_sha256, state, generated_response, "
                "safe_result_message, context_reference_id, operation_id, correlation_id, "
                "operation_status, generation_status, safe_cause_code "
                "FROM app.rag_operation WHERE operation_id = %s",
                (operation_id,),
            )
            return cursor.fetchone()

    @staticmethod
    def final_fragment_ids(connection: psycopg.Connection, rag_operation_id: UUID) -> tuple[UUID, ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT fragment_id FROM app.rag_final_fragment "
                "WHERE rag_operation_id = %s ORDER BY fragment_id",
                (rag_operation_id,),
            )
            return tuple(row["fragment_id"] for row in cursor.fetchall())

    @staticmethod
    def final_fragments(connection: psycopg.Connection, rag_operation_id: UUID) -> tuple[dict, ...]:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT f.fragment_id, f.usage, c.content AS fragment_text, "
                "v.id_documento AS document_id, "
                "f.citation_document_name AS document_name, "
                "f.citation_document_type AS document_type, "
                "f.citation_document_date AS document_date, "
                "f.citation_page_start AS page_start, f.citation_page_end AS page_end, "
                "f.citation_section AS section, f.citation_clause AS clause "
                "FROM app.rag_final_fragment f "
                "JOIN app.document_chunk c ON c.id = f.fragment_id "
                "JOIN app.document_version v ON v.id = c.document_version_id "
                "WHERE f.rag_operation_id = %s ORDER BY f.fragment_id",
                (rag_operation_id,),
            )
            return tuple(cursor.fetchall())

    @staticmethod
    def valid_context_reference(connection: psycopg.Connection, reference_id: UUID) -> bool:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM app.context_reference r "
                "JOIN app.finding f ON f.id = r.finding_id WHERE r.id = %s)",
                (reference_id,),
            )
            return bool(cursor.fetchone()["exists"])

    @staticmethod
    def insert_operation(
        connection: psycopg.Connection,
        *,
        operation_id: UUID,
        user_id: UUID,
        session_id: UUID,
        query_sha256: bytes,
        correlation_id: UUID,
        context_reference_id: UUID | None,
        state: str | None,
        operation_status: str,
        generation_status: str,
        safe_cause_code: str | None,
        generated_response: str | None,
        safe_result_message: str | None,
    ) -> UUID:
        row_id = uuid5(NAMESPACE_URL, f"riesgo-legal:rag-operation:{operation_id}")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.rag_operation "
                "(id, user_id, session_id, query_sha256, state, generated_response, "
                "safe_result_message, context_reference_id, operation_id, correlation_id, "
                "operation_status, generation_status, safe_cause_code) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row_id, user_id, session_id, query_sha256, state, generated_response,
                    safe_result_message, context_reference_id, operation_id, correlation_id,
                    operation_status, generation_status, safe_cause_code,
                ),
            )
        return row_id

    @staticmethod
    def insert_fragments(
        connection: psycopg.Connection,
        rag_operation_id: UUID,
        fragments: tuple[RankedFragment, ...],
        evidence_ids: frozenset[UUID],
        *,
        state: str | None,
    ) -> tuple[tuple[str, UUID], ...]:
        resources: list[tuple[str, UUID]] = []
        with connection.cursor() as cursor:
            for fragment in fragments:
                citation = fragment.citation
                usage = (
                    "REFERENCE" if state == "EVIDENCIA_INSUFICIENTE"
                    else "EVIDENCE" if fragment.fragment_id in evidence_ids
                    else "CONTEXT"
                )
                cursor.execute(
                    "INSERT INTO app.rag_final_fragment "
                    "(rag_operation_id, fragment_id, usage, citation_document_name, "
                    "citation_document_type, citation_document_date, citation_page_start, "
                    "citation_page_end, citation_section, citation_clause) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        rag_operation_id, fragment.fragment_id, usage,
                        citation.document_name, citation.document_type, citation.document_date,
                        citation.page_start, citation.page_end, citation.section, citation.clause,
                    ),
                )
                resources.append((citation.document_id, fragment.fragment_id))
        return tuple(resources)
