"""Acceso transaccional a las entidades analíticas existentes."""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.projection.models import SourceRecordProvenance


_ENTITY_TABLES = {
    "CONTRATO": ("contract_record", "id_contrato"),
    "LITIGIO": ("litigation", "id_litigio"),
    "OBLIGACION": ("compliance_obligation", "id_obligacion"),
    "INCIDENTE": ("incident", "id_incidente"),
    "ASUNTO": ("legal_matter", "id_asunto"),
}


class ProjectionRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @staticmethod
    def source_record_provenance(connection: psycopg.Connection, source_record_id: UUID) -> SourceRecordProvenance | None:
        row = connection.execute(
            "SELECT id, ingest_file_id FROM app.source_record WHERE id = %s",
            (source_record_id,),
        ).fetchone()
        if row is None:
            return None
        return SourceRecordProvenance(source_record_id=row["id"], ingest_file_id=row["ingest_file_id"])

    @staticmethod
    def application_for_target(connection: psycopg.Connection, *, source_record_id: UUID, entity_type: str, business_id: str):
        return connection.execute(
            """SELECT result, payload_sha256, operation_id
                 FROM app.record_application
                WHERE source_record_id = %s AND entity_type = %s AND business_id = %s""",
            (source_record_id, entity_type, business_id),
        ).fetchone()

    @staticmethod
    def entity_row(connection: psycopg.Connection, *, entity_type: str, business_id: str):
        table, identifier = _ENTITY_TABLES[entity_type]
        return connection.execute(
            f"SELECT * FROM app.{table} WHERE {identifier} = %s", (business_id,)
        ).fetchone()

    @staticmethod
    def related_exists(connection: psycopg.Connection, *, table: str, identifier: str) -> bool:
        allowed = {"contract_record": "id_contrato", "compliance_obligation": "id_obligacion"}
        column = allowed[table]
        return connection.execute(
            f"SELECT 1 FROM app.{table} WHERE {column} = %s", (identifier,)
        ).fetchone() is not None

    @staticmethod
    def insert_entity(connection: psycopg.Connection, *, table: str, values: dict[str, object]) -> bool:
        columns = tuple(values)
        placeholders = ", ".join(["%s"] * len(columns))
        row = connection.execute(
            f"INSERT INTO app.{table} ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            tuple(values[column] for column in columns),
        ).fetchone()
        return row is not None

    @staticmethod
    def insert_application(
        connection: psycopg.Connection,
        *,
        source_record_id: UUID,
        entity_type: str,
        business_id: str,
        payload_sha256: bytes,
        result: str,
        operation_id: UUID,
        correlation_id: UUID,
    ) -> bool:
        row = connection.execute(
            """INSERT INTO app.record_application
               (id, source_record_id, entity_type, business_id, payload_sha256, result, operation_id, correlation_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (uuid4(), source_record_id, entity_type, business_id, payload_sha256, result, operation_id, correlation_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def insert_conflict_quarantine(
        connection: psycopg.Connection,
        *,
        ingest_file_id: UUID,
        source_record_id: UUID,
        entity_type: str,
        business_id: str,
        original_payload: dict[str, object],
        candidate_payload: dict[str, object],
        operation_id: UUID,
        correlation_id: UUID,
    ) -> bool:
        row = connection.execute(
            """INSERT INTO app.quarantine_item
               (id, ingest_file_id, source_record_id, entity_type, business_id, cause_code, state,
                original_payload, candidate_payload, operation_id, correlation_id)
               VALUES (%s, %s, %s, %s, %s, 'IDENTITY_CONFLICT', 'Pendiente', %s::jsonb, %s::jsonb, %s, %s)
               ON CONFLICT (operation_id) DO NOTHING
               RETURNING id""",
            (
                uuid4(), ingest_file_id, source_record_id, entity_type, business_id,
                json.dumps(original_payload, ensure_ascii=False, default=_json_default),
                json.dumps(candidate_payload, ensure_ascii=False, default=_json_default),
                operation_id, correlation_id,
            ),
        ).fetchone()
        return row is not None


def _json_default(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
