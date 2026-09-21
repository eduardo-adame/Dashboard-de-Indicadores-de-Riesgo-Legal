"""Persistencia transaccional de identidad, versiones y corpus documental.

Materializa el ciclo de vida documental: una versión candidata se procesa por
completo antes de sustituir a la vigente. La activación de la nueva versión y la
coherencia del corpus ocurren en una única transacción; la versión anterior deja
de ser recuperable y se conserva como historial.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.documents.models import (
    DocumentNotFoundError,
    DocumentRef,
    DocumentVersionRef,
    DocumentVersionState,
    StaleWriteError,
    VersionNotReadyError,
)
from app.documents.versioning import assert_expected_active, ensure_candidate_ready


class DocumentsRepository:
    def __init__(self, conninfo: str, security_repository) -> None:
        self._conninfo = conninfo
        self._security_repository = security_repository

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    # -- identidad ----------------------------------------------------------
    @staticmethod
    def create_or_get_document(
        connection: psycopg.Connection,
        *,
        id_documento: str,
        name: str,
        document_type: str,
        source_family: str,
        document_date=None,
    ) -> DocumentRef:
        row = connection.execute(
            "SELECT id_documento FROM app.document WHERE id_documento = %s FOR UPDATE",
            (id_documento,),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO app.document
                   (id_documento, name, document_type, source_family, document_date)
                   VALUES (%s, %s, %s, %s, %s)""",
                (id_documento, name, document_type, source_family, document_date),
            )
        return DocumentsRepository.get_document(connection, id_documento)

    @staticmethod
    def get_document(connection: psycopg.Connection, id_documento: str) -> DocumentRef:
        row = connection.execute(
            """SELECT id_documento, name, document_type, source_family, active_version_id, invalidated_at
                 FROM app.document WHERE id_documento = %s""",
            (id_documento,),
        ).fetchone()
        if row is None:
            raise DocumentNotFoundError(f"documento inexistente: {id_documento}")
        return DocumentRef(
            id_documento=row["id_documento"],
            name=row["name"],
            document_type=row["document_type"],
            source_family=row["source_family"],
            active_version_id=row["active_version_id"],
            invalidated=row["invalidated_at"] is not None,
        )

    # -- versiones ----------------------------------------------------------
    @staticmethod
    def next_version_number(connection: psycopg.Connection, id_documento: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(max(version_number), 0) + 1 AS number FROM app.document_version WHERE id_documento = %s",
            (id_documento,),
        ).fetchone()
        return int(row["number"])

    @staticmethod
    def create_candidate_version(
        connection: psycopg.Connection,
        *,
        id_documento: str,
        stored_object_id: UUID,
        content_sha256: bytes,
        operation_id: UUID,
        correlation_id: UUID,
        source_record_id: UUID | None = None,
    ) -> DocumentVersionRef:
        version_id = uuid4()
        number = DocumentsRepository.next_version_number(connection, id_documento)
        connection.execute(
            """INSERT INTO app.document_version
               (id, id_documento, version_number, stored_object_id, source_record_id,
                processing_state, content_sha256, operation_id, correlation_id)
               VALUES (%s, %s, %s, %s, %s, 'PENDIENTE', %s, %s, %s)""",
            (version_id, id_documento, number, stored_object_id, source_record_id,
             content_sha256, operation_id, correlation_id),
        )
        return DocumentVersionRef(version_id, id_documento, number, DocumentVersionState.PENDIENTE, operation_id, correlation_id)

    @staticmethod
    def set_version_state(
        connection: psycopg.Connection,
        version_id: UUID,
        state: DocumentVersionState,
    ) -> None:
        completed = state in (DocumentVersionState.LISTA, DocumentVersionState.RECHAZADA, DocumentVersionState.FALLIDA)
        connection.execute(
            """UPDATE app.document_version
                  SET processing_state = %s,
                      completed_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE completed_at END
                WHERE id = %s""",
            (state.value, completed, version_id),
        )

    @staticmethod
    def insert_chunks(connection: psycopg.Connection, version_id: UUID, chunks: Sequence[dict]) -> None:
        for ordinal, chunk in enumerate(chunks):
            connection.execute(
                """INSERT INTO app.document_chunk
                   (id, document_version_id, ordinal, content, token_count, page_start, page_end, section, clause)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    version_id,
                    ordinal,
                    chunk["content"],
                    chunk["token_count"],
                    chunk.get("page_start"),
                    chunk.get("page_end"),
                    chunk.get("section"),
                    chunk.get("clause"),
                ),
            )

    # -- activación atómica -------------------------------------------------
    @staticmethod
    def activate_processed_candidate(
        connection: psycopg.Connection,
        *,
        id_documento: str,
        candidate_version_id: UUID,
        expected_active_version_id: UUID | None,
        actor,
        operation_id: UUID,
        correlation_id: UUID,
    ) -> DocumentRef:
        """Activa atómicamente la versión candidata y su corpus.

        Comprueba que el candidato terminó su procesamiento y que la versión
        activa observada sigue siendo la vigente. La activación y la coherencia
        del corpus ocurren en la misma transacción; la versión anterior deja de
        ser recuperable.
        """
        document = connection.execute(
            "SELECT active_version_id, invalidated_at FROM app.document WHERE id_documento = %s FOR UPDATE",
            (id_documento,),
        ).fetchone()
        if document is None:
            raise DocumentNotFoundError(f"documento inexistente: {id_documento}")

        candidate = connection.execute(
            "SELECT processing_state, id_documento FROM app.document_version WHERE id = %s",
            (candidate_version_id,),
        ).fetchone()
        if candidate is None or candidate["id_documento"] != id_documento:
            raise DocumentNotFoundError("versión candidata inexistente para el documento")
        ensure_candidate_ready(DocumentVersionState(candidate["processing_state"]))

        assert_expected_active(expected_active_version_id, document["active_version_id"])

        connection.execute(
            "UPDATE app.document SET active_version_id = %s, invalidated_at = NULL, updated_at = CURRENT_TIMESTAMP WHERE id_documento = %s",
            (candidate_version_id, id_documento),
        )
        return DocumentRef(
            id_documento=id_documento,
            name="",
            document_type="",
            source_family="",
            active_version_id=candidate_version_id,
        )

    @staticmethod
    def invalidate_document(connection: psycopg.Connection, id_documento: str) -> None:
        row = connection.execute(
            "SELECT id_documento FROM app.document WHERE id_documento = %s FOR UPDATE",
            (id_documento,),
        ).fetchone()
        if row is None:
            raise DocumentNotFoundError(f"documento inexistente: {id_documento}")
        connection.execute(
            "UPDATE app.document SET active_version_id = NULL, invalidated_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id_documento = %s",
            (id_documento,),
        )

    @staticmethod
    def active_chunk_count(connection: psycopg.Connection, id_documento: str) -> int:
        row = connection.execute(
            """SELECT count(*) AS total FROM app.active_document_chunk c
               JOIN app.document_version v ON v.id = c.document_version_id
              WHERE v.id_documento = %s""",
            (id_documento,),
        ).fetchone()
        return int(row["total"])

    def write_audit_event(self, connection: psycopg.Connection, **kwargs) -> None:
        self._security_repository.write_audit_event(connection, **kwargs)

    @staticmethod
    def insert_ocr_run(
        connection: psycopg.Connection,
        *,
        document_version_id: UUID,
        attempt_number: int,
        estado_ocr: str,
        confianza_agregada: float | None,
        total_page_count: int | None,
        processed_page_count: int | None,
        granularity: str | None,
        operation_id: UUID,
        correlation_id: UUID,
    ) -> None:
        connection.execute(
            """INSERT INTO app.ocr_run
               (id, document_version_id, attempt_number, estado_ocr, confianza_agregada,
                total_page_count, ocr_processed_page_count, granularity, is_final,
                operation_id, correlation_id)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, true, %s, %s)""",
            (
                document_version_id,
                attempt_number,
                estado_ocr,
                confianza_agregada,
                total_page_count,
                processed_page_count,
                granularity,
                operation_id,
                correlation_id,
            ),
        )
