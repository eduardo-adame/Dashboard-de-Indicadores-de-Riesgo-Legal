"""Persistencia durable del candidato documental.

Guarda y reconstruye el resultado de la extracción documental inicial sin volver
a abrir el binario. La reconstrucción es fiel por página: conserva el texto
nativo por página, la necesidad de OCR por página y el estado de procesamiento.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ingestion.models import DocumentCandidate, DocumentPage


class DocumentCandidateRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @staticmethod
    def persist(connection: psycopg.Connection, *, file_id: UUID, candidate: DocumentCandidate) -> None:
        """Persiste el candidato y sus páginas de forma idempotente."""
        connection.execute(
            """INSERT INTO app.document_candidate
               (ingest_file_id, processing_state, native_text)
               VALUES (%s, %s, %s)
               ON CONFLICT (ingest_file_id) DO UPDATE
                 SET processing_state = EXCLUDED.processing_state,
                     native_text = EXCLUDED.native_text,
                     updated_at = CURRENT_TIMESTAMP""",
            (file_id, candidate.processing_state, candidate.native_text),
        )
        connection.execute(
            "DELETE FROM app.document_candidate_page WHERE ingest_file_id = %s",
            (file_id,),
        )
        for page in candidate.pages:
            connection.execute(
                """INSERT INTO app.document_candidate_page
                   (id, ingest_file_id, page_number, native_text, requires_ocr)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s)""",
                (file_id, page.page_number, page.native_text, page.requires_ocr),
            )

    @staticmethod
    def load(connection: psycopg.Connection, file_id: UUID) -> DocumentCandidate | None:
        """Reconstruye el candidato preservando el texto por página."""
        header = connection.execute(
            "SELECT processing_state, native_text FROM app.document_candidate WHERE ingest_file_id = %s",
            (file_id,),
        ).fetchone()
        if header is None:
            return None
        page_rows = connection.execute(
            """SELECT page_number, native_text, requires_ocr
                 FROM app.document_candidate_page
                WHERE ingest_file_id = %s
                ORDER BY page_number""",
            (file_id,),
        ).fetchall()
        pages = tuple(
            DocumentPage(
                page_number=row["page_number"],
                native_text=row["native_text"],
                requires_ocr=row["requires_ocr"],
            )
            for row in page_rows
        )
        return DocumentCandidate(
            pages=pages,
            native_text=header["native_text"],
            processing_state=header["processing_state"],
        )
