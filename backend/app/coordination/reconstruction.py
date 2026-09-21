"""Reconstrucción de resultados de ingesta para el despacho downstream.

Reconstruye el conjunto tabular extraído y el candidato documental a partir del
estado persistido, preservando la identidad de origen de cada registro.
"""
from __future__ import annotations

import json
from uuid import UUID

import psycopg

from app.ingestion.models import DocumentCandidate, SourceFamily
from app.validation.service import TabularRecord


def reconstruct_family(connection: psycopg.Connection, file_id: UUID) -> SourceFamily | None:
    row = connection.execute(
        "SELECT source_family FROM app.ingest_file WHERE id = %s",
        (file_id,),
    ).fetchone()
    return SourceFamily(str(row["source_family"])) if row else None


def reconstruct_headers(connection: psycopg.Connection, file_id: UUID) -> tuple[object, ...]:
    row = connection.execute(
        "SELECT extraction_metadata FROM app.ingest_file WHERE id = %s",
        (file_id,),
    ).fetchone()
    if row is None or row["extraction_metadata"] is None:
        return ()
    metadata = row["extraction_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    headers = metadata.get("headers") or []
    return tuple(headers)


def reconstruct_records(connection: psycopg.Connection, file_id: UUID) -> tuple[TabularRecord, ...]:
    """Reconstruye los registros tabulares preservando ``source_record_id``.

    Cada ``raw_payload`` ya está indexado por el nombre de columna contractual,
    de modo que el mapeo ``header[i] → value[i]`` es determinista y no depende del
    orden incidental del JSONB.
    """
    rows = connection.execute(
        """SELECT id, source_sheet, row_number, raw_payload
             FROM app.source_record
            WHERE ingest_file_id = %s
            ORDER BY source_sheet, row_number""",
        (file_id,),
    ).fetchall()
    records: list[TabularRecord] = []
    for index, row in enumerate(rows):
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload or {}
        records.append(
            TabularRecord(
                position=row["row_number"] or (index + 1),
                source_record_id=row["id"],
                values_by_name=dict(payload),
                width=len(payload),
            )
        )
    return tuple(records)


def reconstruct_document_candidate(connection: psycopg.Connection, file_id: UUID) -> DocumentCandidate | None:
    from app.ingestion.document_candidate_repository import DocumentCandidateRepository

    return DocumentCandidateRepository.load(connection, file_id)
