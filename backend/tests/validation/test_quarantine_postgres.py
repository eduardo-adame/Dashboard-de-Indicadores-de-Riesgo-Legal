"""Evidencia PostgreSQL de cuarentena: idempotencia por registro y concurrencia."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService
from app.validation.models import QuarantineCause, QuarantineState
from app.validation.quarantine import QuarantineError
from app.validation.repository import ValidationRepository
from app.validation.service import TabularRecord, ValidationContext, ValidationService


BACKEND_ROOT = Path(__file__).resolve().parents[2]
_HEADERS = ("ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision")
_AUDIT_LEGAL_MATTER_HEADERS = ("ID_Asunto", "Tipo_Asunto", "Estado", "Fecha")
_AUDIT_INCIDENT_HEADERS = ("ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad")
_AUDIT_BOTH_HEADERS = _AUDIT_INCIDENT_HEADERS + _AUDIT_LEGAL_MATTER_HEADERS


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    if "test" not in (make_url(value).database or "").lower():
        pytest.fail("La evidencia requiere una base desechable")
    return value


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _namespace() -> UUID:
    return uuid4()


@pytest.fixture
def database():
    url = _url()
    parsed = make_url(url)
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = sa.create_engine(url)
    account_id, session_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :u, 'Analyst', 'hash')"), {"id": account_id, "u": f"validator-{account_id.hex[:6]}"})
        connection.execute(sa.text("INSERT INTO app.user_role (user_id, role_id) VALUES (:id, 'ANALISTA')"), {"id": account_id})
        connection.execute(
            sa.text("""INSERT INTO app.access_session
                (id, user_id, refresh_token_sha256, authorization_version, state, expires_at)
                VALUES (:session, :user, :hash, 1, 'ACTIVE', :expires)"""),
            {"session": session_id, "user": account_id, "hash": bytes(32), "expires": datetime.now(UTC) + timedelta(hours=1)},
        )
    principal = AuthenticatedPrincipal(account_id, session_id, "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute", "quarantine.read", "quarantine.reinject", "quarantine.discard"}))
    conninfo = _conninfo(url)
    encoded_secret = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    security = SecurityService(
        SecurityRepository(conninfo),
        JwtService(issuer="issuer", audience="audience", keyring={"key": encoded_secret}, active_kid="key"),
    )
    repository = ValidationRepository(conninfo, security.repository)
    service = ValidationService(repository, security)
    try:
        yield engine, service, principal, conninfo
    finally:
        engine.dispose()


def _seed_file_and_records(engine: sa.Engine, count: int) -> tuple[UUID, list[UUID]]:
    file_id = uuid4()
    object_id = uuid4()
    record_ids = [uuid4() for _ in range(count)]
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 10, 'data.csv')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-data.csv", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("""INSERT INTO app.ingest_file
                (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                 declared_extension, detected_format, format_classification, technical_result,
                 declared_name, source_locator, source_revision, actor_identifier, content_sha256)
                VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'CSV', 'COMPLETADO', :operation, :correlation,
                        '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'data.csv', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}/data.csv", "sha": bytes(32)},
        )
        for index, record_id in enumerate(record_ids):
            connection.execute(
                sa.text("""INSERT INTO app.source_record
                    (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state)
                    VALUES (:id, :file, :row, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"""),
                {"id": record_id, "file": file_id, "row": index + 2, "sha": bytes(32)},
            )
    return file_id, record_ids


def _seed_audit_file_and_records(engine: sa.Engine, count: int) -> tuple[UUID, list[UUID]]:
    file_id = uuid4()
    object_id = uuid4()
    record_ids = [uuid4() for _ in range(count)]
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 10, 'audit.csv')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-audit.csv", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("""INSERT INTO app.ingest_file
                (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                 declared_extension, detected_format, format_classification, technical_result,
                 declared_name, source_locator, source_revision, actor_identifier, content_sha256)
                VALUES (:id, :object, 'AUDITORIA_INTERNA', 'CSV', 'COMPLETADO', :operation, :correlation,
                        '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'audit.csv', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}/audit.csv", "sha": bytes(32)},
        )
        for index, record_id in enumerate(record_ids):
            connection.execute(
                sa.text("""INSERT INTO app.source_record
                    (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state)
                    VALUES (:id, :file, :row, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"""),
                {"id": record_id, "file": file_id, "row": index + 2, "sha": bytes(32)},
            )
    return file_id, record_ids


def _invalid_record(position: int, source_record_id: UUID) -> TabularRecord:
    return TabularRecord(
        position=position,
        source_record_id=source_record_id,
        values_by_name={
            "ID_Contrato": f"C-{position}",
            "Fecha_Solicitud": "2026-01-10",
            "Fecha_Firma": "2026-01-15",
            "Fecha_Vencimiento": "2026-06-30",
            "Estado_Revision": "Pendiente",  # fuera de catálogo → cuarentena
        },
        width=5,
    )


def _audit_legal_matter_record(position: int, source_record_id: UUID, *, valid: bool) -> TabularRecord:
    return TabularRecord(
        position=position,
        source_record_id=source_record_id,
        values_by_name={
            "ID_Asunto": f"A-{position}",
            "Tipo_Asunto": "Auditoría" if valid else "Otro",
            "Estado": "Abierto",
            "Fecha": "2026-03-01",
        },
        width=4,
    )


def _audit_incident_record(position: int, source_record_id: UUID) -> TabularRecord:
    return TabularRecord(
        position=position,
        source_record_id=source_record_id,
        values_by_name={
            "ID_Incidente": f"I-{position}",
            "Fecha_Evento": "2026-03-01",
            "Area": "Jurídica",
            "Nivel_Severidad": "alto",
        },
        width=4,
    )


def _audit_both_record(position: int, source_record_id: UUID) -> TabularRecord:
    incident = _audit_incident_record(position, source_record_id)
    legal_matter = _audit_legal_matter_record(position, source_record_id, valid=True)
    return TabularRecord(
        position=position,
        source_record_id=source_record_id,
        values_by_name=incident.values_by_name | legal_matter.values_by_name,
        width=8,
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_multiple_invalid_records_create_multiple_quarantine_items(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_file_and_records(engine, 3)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    records = tuple(_invalid_record(i + 1, rid) for i, rid in enumerate(record_ids))
    result = service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    assert len(result.quarantined) == 3
    with engine.connect() as connection:
        total = connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()
    assert total == 3


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_retry_does_not_duplicate_quarantine_items(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_file_and_records(engine, 2)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    records = tuple(_invalid_record(i + 1, rid) for i, rid in enumerate(record_ids))
    service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    with engine.connect() as connection:
        total = connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()
    assert total == 2


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_reinject_and_discard_serialize(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_file_and_records(engine, 1)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=(_invalid_record(1, record_ids[0]),), context=context)
    with engine.connect() as connection:
        item_id = connection.execute(sa.text("SELECT id FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()

    def do_discard() -> str:
        try:
            service.discard(item_id=item_id, justification="duplicado", context=context)
            return "discarded"
        except (QuarantineError, Exception):
            return "rejected"

    def do_reinject() -> str:
        corrected = {
            "ID_Contrato": "C-1", "Fecha_Solicitud": "2026-01-10", "Fecha_Firma": "2026-01-15",
            "Fecha_Vencimiento": "2026-06-30", "Estado_Revision": "No iniciado",
        }
        try:
            service.reinject(item_id=item_id, corrected_payload=corrected, context=context)
            return "reinjected"
        except (QuarantineError, Exception):
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [f.result() for f in [executor.submit(do_discard), executor.submit(do_reinject)]]
    # Sólo una transición debe confirmarse; la otra se rechaza.
    assert outcomes.count("rejected") == 1
    with engine.connect() as connection:
        state = connection.execute(sa.text("SELECT state FROM app.quarantine_item WHERE id = :id"), {"id": item_id}).scalar_one()
    assert state in (QuarantineState.DESCARTADO.value, QuarantineState.REINYECTADO.value)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_quarantine_query_filters(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_file_and_records(engine, 2)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    records = tuple(_invalid_record(i + 1, rid) for i, rid in enumerate(record_ids))
    service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)

    by_file = service.list_quarantine(context=context, ingest_file_id=file_id)
    assert len(by_file) == 2
    pending = service.list_quarantine(context=context, ingest_file_id=file_id, state=QuarantineState.PENDIENTE.value)
    assert len(pending) == 2
    assert service.list_quarantine(context=context, ingest_file_id=uuid4()) == []


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_failure_rolls_back_quarantine(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_file_and_records(engine, 1)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    records = (_invalid_record(1, record_ids[0]),)

    original = service.repository.write_audit_event
    service.repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failure"))
    try:
        with pytest.raises(RuntimeError):
            service.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    finally:
        service.repository.write_audit_event = original

    with engine.connect() as connection:
        total = connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()
    assert total == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_legal_matter_only_is_conforming_without_quarantine(database) -> None:
    engine, service, principal, _ = database
    cases = (
        (_AUDIT_INCIDENT_HEADERS, _audit_incident_record),
        (_AUDIT_LEGAL_MATTER_HEADERS, lambda position, record_id: _audit_legal_matter_record(position, record_id, valid=True)),
        (_AUDIT_BOTH_HEADERS, _audit_both_record),
    )
    for headers, record_factory in cases:
        file_id, record_ids = _seed_audit_file_and_records(engine, 1)
        context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
        result = service.validate(
            file_id=file_id,
            family=SourceFamily.INTERNAL_AUDIT,
            headers=headers,
            records=(record_factory(1, record_ids[0]),),
            context=context,
        )
        assert result.conforming_positions == (1,)
        with engine.connect() as connection:
            total = connection.execute(
                sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"),
                {"id": file_id},
            ).scalar_one()
        assert total == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_invalid_audit_legal_matter_creates_one_quarantine_and_audit(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_audit_file_and_records(engine, 1)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    result = service.validate(
        file_id=file_id,
        family=SourceFamily.INTERNAL_AUDIT,
        headers=_AUDIT_LEGAL_MATTER_HEADERS,
        records=(_audit_legal_matter_record(1, record_ids[0], valid=False),),
        context=context,
    )
    assert result.quarantined == ((1, QuarantineCause.OUT_OF_CATALOG),)
    with engine.connect() as connection:
        quarantine_count = connection.execute(
            sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"),
            {"id": file_id},
        ).scalar_one()
        audit_count = connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE correlation_id = :id AND action = 'QUARANTINE_CREATED'"),
            {"id": context.correlation_id},
        ).scalar_one()
    assert quarantine_count == 1
    assert audit_count == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_failure_rolls_back_audit_quarantine_mutation(database) -> None:
    engine, service, principal, _ = database
    file_id, record_ids = _seed_audit_file_and_records(engine, 1)
    context = ValidationContext(operation_id=_namespace(), correlation_id=uuid4(), actor=principal)
    original = service.repository.write_audit_event
    service.repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failure"))
    try:
        with pytest.raises(RuntimeError):
            service.validate(
                file_id=file_id,
                family=SourceFamily.INTERNAL_AUDIT,
                headers=_AUDIT_LEGAL_MATTER_HEADERS,
                records=(_audit_legal_matter_record(1, record_ids[0], valid=False),),
                context=context,
            )
    finally:
        service.repository.write_audit_event = original
    with engine.connect() as connection:
        total = connection.execute(
            sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"),
            {"id": file_id},
        ).scalar_one()
    assert total == 0
