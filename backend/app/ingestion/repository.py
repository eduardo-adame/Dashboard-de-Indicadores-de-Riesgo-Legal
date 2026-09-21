"""Persistencia transaccional del pipeline de ingesta."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ingestion.models import DetectionResult, ExtractedRecordSet, StagedFile


class IngestionRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @staticmethod
    def lock_source(connection: psycopg.Connection, source_locator: str) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (source_locator,))

    @staticmethod
    def find_existing(
        connection: psycopg.Connection,
        *,
        source_locator: str,
        actor_identifier: str,
        idempotency_key: str | None,
        content_sha256: bytes,
    ) -> tuple[dict[str, object] | None, bool]:
        if idempotency_key:
            row = connection.execute(
                """SELECT id, operation_id, correlation_id, state, exchange_format, source_family,
                          safe_cause_code, content_sha256
                   FROM app.ingest_file
                   WHERE actor_identifier = %s AND idempotency_key = %s FOR UPDATE""",
                (actor_identifier, idempotency_key),
            ).fetchone()
            if row is not None:
                return row, bytes(row["content_sha256"]) != content_sha256
        row = connection.execute(
            """SELECT id, operation_id, correlation_id, state, exchange_format, source_family,
                      safe_cause_code, content_sha256
               FROM app.ingest_file
               WHERE source_locator = %s AND content_sha256 = %s FOR UPDATE""",
            (source_locator, content_sha256),
        ).fetchone()
        return row, False

    @staticmethod
    def next_revision(connection: psycopg.Connection, source_locator: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(max(source_revision), 0) + 1 AS revision FROM app.ingest_file WHERE source_locator = %s",
            (source_locator,),
        ).fetchone()
        return int(row["revision"])

    @staticmethod
    def insert_file(
        connection: psycopg.Connection,
        *,
        file_id: UUID,
        object_id: UUID,
        operation_id: UUID,
        correlation_id: UUID,
        staged: StagedFile,
        source_locator: str,
        source_revision: int,
        actor_identifier: str,
        idempotency_key: str | None,
        family: str,
        detection: DetectionResult,
        state: str,
        technical_result: str,
        mime_type: str,
        safe_cause_code: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO app.stored_object
               (id, storage_kind, locator, sha256, mime_type, byte_size, original_name)
               VALUES (%s, 'FILESYSTEM', %s, %s, %s, %s, %s)""",
            (object_id, staged.locator, staged.sha256, mime_type, staged.byte_size, staged.original_name),
        )
        connection.execute(
            """INSERT INTO app.ingest_file
               (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                declared_extension, detected_format, format_classification, technical_result, safe_cause_code,
                declared_name, source_locator, source_revision, actor_identifier, idempotency_key, content_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                file_id, object_id, family, detection.exchange_format.value if detection.exchange_format else None,
                state, operation_id, correlation_id, detection.declared_extension, detection.detected_format,
                "SUPPORTED" if detection.supported else "UNSUPPORTED", technical_result,
                safe_cause_code, staged.original_name, source_locator, source_revision,
                actor_identifier, idempotency_key, staged.sha256,
            ),
        )

    @staticmethod
    def insert_resource_limit_rejection(
        connection: psycopg.Connection,
        *,
        file_id: UUID,
        operation_id: UUID,
        correlation_id: UUID,
        original_name: str,
        source_locator: str,
        source_revision: int,
        actor_identifier: str,
        idempotency_key: str | None,
        family: str,
        observed_byte_size: int,
    ) -> None:
        connection.execute(
            """INSERT INTO app.ingest_file
               (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                declared_extension, detected_format, format_classification, technical_result, safe_cause_code,
                declared_name, source_locator, source_revision, actor_identifier, idempotency_key, content_sha256,
                content_sha256_complete, observed_byte_size)
               VALUES (%s, NULL, %s, NULL, 'RECHAZADO', %s, %s, %s, 'NOT_INSPECTED_RESOURCE_LIMIT',
                       'UNDETERMINED', 'REJECTED', 'RESOURCE_LIMIT_EXCEEDED', %s, %s, %s, %s, %s, NULL, false, %s)""",
            (
                file_id, family, operation_id, correlation_id, Path(original_name).suffix.lower() or None,
                original_name, source_locator, source_revision, actor_identifier, idempotency_key, observed_byte_size,
            ),
        )

    @staticmethod
    def insert_rows(connection: psycopg.Connection, file_id: UUID, records: ExtractedRecordSet) -> None:
        connection.execute(
            "UPDATE app.ingest_file SET extraction_metadata = %s::jsonb WHERE id = %s",
            (
                json.dumps(
                    {"headers": records.headers, "sheets": records.sheets, "sheet_headers": records.sheet_headers},
                    ensure_ascii=False,
                    default=str,
                ),
                file_id,
            ),
        )
        headers_by_sheet = dict(records.sheet_headers)
        for row in records.rows:
            row_headers = headers_by_sheet.get(row.sheet or "", records.headers)
            payload = {
                str(row_headers[index]) if index < len(row_headers) else f"column_{index + 1}": value
                for index, value in enumerate(row.values)
            }
            encoded = json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)
            record_sha256 = hashlib.sha256(encoded.encode("utf-8")).digest()
            connection.execute(
                """INSERT INTO app.source_record
                   (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state)
                   VALUES (gen_random_uuid(), %s, %s, %s, %s::jsonb, %s, 'EXTRAIDO')""",
                (file_id, row.row_number, row.sheet, encoded, record_sha256),
            )

    @staticmethod
    def insert_quarantine(
        connection: psycopg.Connection,
        *,
        file_id: UUID,
        object_id: UUID,
        cause_code: str,
        operation_id: UUID,
        correlation_id: UUID,
    ) -> UUID:
        row = connection.execute(
            """INSERT INTO app.quarantine_item
               (id, ingest_file_id, cause_code, state, original_object_id, operation_id, correlation_id)
               VALUES (gen_random_uuid(), %s, %s, 'Pendiente', %s, %s, %s) RETURNING id""",
            (file_id, cause_code, object_id, operation_id, correlation_id),
        ).fetchone()
        return row["id"]
