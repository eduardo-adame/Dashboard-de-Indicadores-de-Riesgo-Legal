"""Integración de migraciones sobre una base PostgreSQL desechable.

Definir DATA_TEST_DATABASE_URL con una URL postgresql+psycopg cuyo nombre de base
contenga ``test``. La guarda evita destruir accidentalmente una base real.
"""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISIONS = (
    "0001_foundation",
    "0002_ingestion_domain",
    "0003_documents_corpus",
    "0004_analytics_rag_jobs",
    "0005_security_audit_grants",
    "0006_security_priv",
    "0007_ingestion_rejections",
    "0008_ingestion_resource_receipts",
    "0009_coordination_dispatch",
    "0010_document_candidate",
    "0011_document_manage_capability",
    "0012_kpi_observation_semantics",
)


def _test_url() -> str:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower():
        pytest.fail("DATA_TEST_DATABASE_URL debe apuntar a una base desechable cuyo nombre contenga 'test'")
    return url


def _config(url: str) -> Config:
    parsed = make_url(url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    return config


@pytest.fixture(scope="module")
def migrated_database() -> tuple[sa.Engine, Config]:
    url = _test_url()
    config = _config(url)
    engine = sa.create_engine(url)
    command.downgrade(config, "base")
    try:
        yield engine, config
    finally:
        command.downgrade(config, "base")
        engine.dispose()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_every_revision_is_valid_and_round_trips(migrated_database: tuple[sa.Engine, Config]) -> None:
    engine, config = migrated_database
    expected = {
        "0001_foundation": {"stored_object"},
        "0002_ingestion_domain": {"quarantine_item", "contract_record", "litigation"},
        "0003_documents_corpus": {"document", "document_version", "document_chunk", "chunk_term"},
        "0004_analytics_rag_jobs": {"analytic_run", "kpi_observation", "rag_operation", "rag_final_fragment"},
        "0005_security_audit_grants": {"user_account", "access_session", "document_exception"},
        "0006_security_priv": {"user_account", "access_session", "document_exception"},
        "0007_ingestion_rejections": {"ingest_file", "stored_object"},
        "0008_ingestion_resource_receipts": {"ingest_file", "stored_object"},
        "0009_coordination_dispatch": {"coordination_dispatch", "ingest_file"},
        "0010_document_candidate": {"document_candidate", "document_candidate_page"},
        "0011_document_manage_capability": {"permission", "role_permission"},
        # 0012 alinea kpi_observation; no crea tablas nuevas. El mapa comprueba
        # únicamente existencia de tablas, por lo que el conjunto es vacío.
        "0012_kpi_observation_semantics": set(),
    }
    previous = "base"
    for revision in REVISIONS:
        command.upgrade(config, revision)
        inspector = sa.inspect(engine)
        assert expected[revision].issubset(set(inspector.get_table_names(schema="app")))
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == revision

        # Cada revisión debe poder deshacerse y aplicarse de nuevo sin depender
        # de objetos futuros. La base es desechable y aún no contiene datos reales.
        command.downgrade(config, previous)
        command.upgrade(config, revision)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == revision
        previous = revision

    command.downgrade(config, "0004_analytics_rag_jobs")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == REVISIONS[-1]


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_contract_constraints_atomic_version_and_audit_privileges(
    migrated_database: tuple[sa.Engine, Config],
) -> None:
    engine, config = migrated_database
    command.upgrade(config, "head")

    ids = {name: uuid.uuid4() for name in ("object1", "object2", "object3", "ingest", "source", "quarantine", "document", "v1", "v2", "v3")}
    operation = uuid.uuid4()
    correlation = uuid.uuid4()
    digest = bytes(32)

    with engine.begin() as connection:
        for index, object_key in enumerate(("object1", "object2", "object3"), start=1):
            connection.execute(
                sa.text(
                    "INSERT INTO app.stored_object "
                    "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                    "VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/pdf', 1, :name)"
                ),
                {"id": ids[object_key], "locator": f"documents/{index}.pdf", "sha": digest, "name": f"{index}.pdf"},
            )
        connection.execute(
            sa.text(
                "INSERT INTO app.ingest_file "
                "(id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id, "
                " declared_extension, detected_format, declared_name, source_locator, source_revision, "
                " actor_identifier, content_sha256) "
                "VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'PDF', 'CUARENTENA', :operation, :correlation, "
                " '.pdf', 'PDF', '1.pdf', 'documents/1.pdf', 1, 'test', :sha)"
            ),
            {"id": ids["ingest"], "object": ids["object1"], "operation": operation, "correlation": correlation, "sha": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.source_record "
                "(id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state) "
                "VALUES (:id, :file, 1, 'LEGACY', '{\"id\": \"DOC-1\"}'::jsonb, :sha, 'CUARENTENA')"
            ),
            {"id": ids["source"], "file": ids["ingest"], "sha": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.quarantine_item "
                "(id, ingest_file_id, source_record_id, entity_type, business_id, cause_code, state, "
                " original_payload, candidate_payload, operation_id, correlation_id) "
                "VALUES (:id, :file, :source, 'DOCUMENTO', 'DOC-1', 'IDENTITY_CONFLICT', 'Pendiente', "
                " '{\"version\": 1}'::jsonb, '{\"version\": 2}'::jsonb, :operation, :correlation)"
            ),
            {
                "id": ids["quarantine"], "file": ids["ingest"], "source": ids["source"],
                "operation": uuid.uuid4(), "correlation": correlation,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.document (id_documento, name, document_type, source_family) "
                "VALUES ('DOC-1', 'Contrato uno', 'Contrato', 'CONTRATOS_DOCUMENTOS')"
            )
        )
        for version_id, number, object_id, state in (
            (ids["v1"], 1, ids["object1"], "LISTA"),
            (ids["v2"], 2, ids["object2"], "FALLIDA"),
            (ids["v3"], 3, ids["object3"], "LISTA"),
        ):
            connection.execute(
                sa.text(
                    "INSERT INTO app.document_version "
                    "(id, id_documento, version_number, stored_object_id, processing_state, content_sha256, "
                    " operation_id, correlation_id, completed_at) "
                    "VALUES (:id, 'DOC-1', :number, :object, :state, :sha, :operation, :correlation, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": version_id, "number": number, "object": object_id, "state": state,
                    "sha": digest, "operation": uuid.uuid4(), "correlation": correlation,
                },
            )
        connection.execute(sa.text("UPDATE app.document SET active_version_id = :version WHERE id_documento = 'DOC-1'"), {"version": ids["v1"]})
        ocr_run_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO app.ocr_run "
                "(id, document_version_id, attempt_number, estado_ocr, resultado_ocr, confianza_agregada, "
                " total_page_count, ocr_processed_page_count, granularity, is_final, operation_id, correlation_id) "
                "VALUES (:id, :version, 1, 'Exitoso', 'texto', 0.90, 12, 10, 'WORD', true, :operation, :correlation)"
            ),
            {"id": ocr_run_id, "version": ids["v1"], "operation": uuid.uuid4(), "correlation": correlation},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.ocr_confidence_sample (ocr_run_id, unit_number, unit_type, confidence) "
                "VALUES (:id, 1, 'WORD', 0.90), (:id, 2, 'DOCUMENT', 0.90)"
            ),
            {"id": ocr_run_id},
        )

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT active_version_id FROM app.document WHERE id_documento = 'DOC-1'")).scalar_one() == ids["v1"]
        assert connection.execute(
            sa.text("SELECT total_page_count, ocr_processed_page_count FROM app.ocr_run WHERE id = :id"),
            {"id": ocr_run_id},
        ).one() == (12, 10)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO app.ocr_run "
                    "(id, document_version_id, attempt_number, estado_ocr, confianza_agregada, "
                    " total_page_count, ocr_processed_page_count, operation_id, correlation_id) "
                    "VALUES (:id, :version, 2, 'Exitoso', 0.90, 2, 3, :operation, :correlation)"
                ),
                {"id": uuid.uuid4(), "version": ids["v1"], "operation": uuid.uuid4(), "correlation": correlation},
            )

    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE app.document SET active_version_id = :version WHERE id_documento = 'DOC-1'"), {"version": ids["v3"]})
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT active_version_id FROM app.document WHERE id_documento = 'DOC-1'")).scalar_one() == ids["v3"]
        assert connection.execute(sa.text("SELECT has_table_privilege('riesgo_legal_runtime', 'audit.event', 'UPDATE')")).scalar_one() is False
        assert connection.execute(sa.text("SELECT has_table_privilege('riesgo_legal_runtime', 'audit.event', 'DELETE')")).scalar_one() is False
        assert connection.execute(sa.text("SELECT has_table_privilege('riesgo_legal_runtime', 'audit.event', 'INSERT')")).scalar_one() is True

    # A domain mutation and its mandatory audit event share one transaction.
    rolled_back_object = uuid.uuid4()
    duplicate_event = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO audit.event "
                "(id, actor_type, actor_identifier, action, resource_type, result, operation_id, correlation_id) "
                "VALUES (:id, 'PROCESS', 'test', 'seed', 'TEST', 'OK', :operation, :correlation)"
            ),
            {"id": duplicate_event, "operation": uuid.uuid4(), "correlation": correlation},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO app.stored_object "
                    "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                    "VALUES (:id, 'FILESYSTEM', 'rollback/object.pdf', :sha, 'application/pdf', 1, 'object.pdf')"
                ),
                {"id": rolled_back_object, "sha": digest},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO audit.event "
                    "(id, actor_type, actor_identifier, action, resource_type, result, operation_id, correlation_id) "
                    "VALUES (:id, 'PROCESS', 'test', 'duplicate', 'TEST', 'OK', :operation, :correlation)"
                ),
                {"id": duplicate_event, "operation": uuid.uuid4(), "correlation": correlation},
            )
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.stored_object WHERE id = :id"), {"id": rolled_back_object}).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_runtime_role_cannot_mutate_audit(migrated_database: tuple[sa.Engine, Config]) -> None:
    engine, config = migrated_database
    command.upgrade(config, "head")
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(sa.text("SET LOCAL ROLE riesgo_legal_runtime"))
        with pytest.raises(DBAPIError):
            connection.execute(sa.text("UPDATE audit.event SET result = 'CHANGED'"))
        transaction.rollback()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_unsupported_format_is_recordable_but_never_processable(
    migrated_database: tuple[sa.Engine, Config],
) -> None:
    engine, config = migrated_database
    command.upgrade(config, "head")
    object_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/octet-stream', 4, 'payload.bin')"
            ),
            {"id": object_id, "locator": f"ingestion-test/{object_id}.bin", "sha": bytes(32)},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.ingest_file "
                "(id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id, "
                " declared_extension, detected_format, format_classification, technical_result, safe_cause_code, "
                " declared_name, source_locator, source_revision, actor_identifier, content_sha256) "
                "VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', NULL, 'RECHAZADO', :operation, :correlation, "
                " '.bin', 'BINARY', 'UNSUPPORTED', 'REJECTED', 'UNSUPPORTED_FORMAT', "
                " 'payload.bin', 'test/payload.bin', 1, 'test', :sha)"
            ),
            {
                "id": uuid.uuid4(), "object": object_id,
                "operation": uuid.uuid4(), "correlation": uuid.uuid4(), "sha": bytes(32),
            },
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO app.ingest_file "
                    "(id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id, "
                    " declared_extension, detected_format, format_classification, technical_result, safe_cause_code, "
                    " declared_name, source_locator, source_revision, actor_identifier, content_sha256) "
                    "VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', NULL, 'PROCESANDO', :operation, :correlation, "
                    " '.bin', 'BINARY', 'UNSUPPORTED', 'REJECTED', 'UNSUPPORTED_FORMAT', "
                    " 'payload.bin', 'test/invalid.bin', 1, 'test', :sha)"
                ),
                {
                    "id": uuid.uuid4(), "object": object_id,
                    "operation": uuid.uuid4(), "correlation": uuid.uuid4(), "sha": bytes(32),
                },
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """INSERT INTO app.ingest_file
                       (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                        declared_extension, detected_format, format_classification, technical_result, safe_cause_code,
                        declared_name, source_locator, source_revision, actor_identifier, content_sha256,
                        content_sha256_complete, observed_byte_size)
                       VALUES (:id, NULL, 'LITIGIOS', NULL, 'RECHAZADO', :operation, :correlation,
                               '.csv', 'NOT_INSPECTED_RESOURCE_LIMIT', 'UNDETERMINED', 'REJECTED',
                               'OTHER_CAUSE', 'payload.csv', 'test/invalid-receipt.csv', 1, 'test', NULL, false, 6)"""
                ),
                {"id": uuid.uuid4(), "operation": uuid.uuid4(), "correlation": uuid.uuid4()},
            )


def _insert_supported_ingest_file(connection, *, file_id, object_id) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO app.stored_object "
            "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
            "VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 10, 'data.csv')"
        ),
        {"id": object_id, "locator": f"objects/ab/{object_id}-data.csv", "sha": bytes(32)},
    )
    connection.execute(
        sa.text(
            """INSERT INTO app.ingest_file
               (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                declared_extension, detected_format, format_classification, technical_result,
                declared_name, source_locator, source_revision, actor_identifier, content_sha256)
               VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'CSV', 'COMPLETADO', :operation, :correlation,
                       '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'data.csv', :locator, 1, 'test', :sha)"""
        ),
        {
            "id": file_id,
            "object": object_id,
            "operation": uuid.uuid4(),
            "correlation": uuid.uuid4(),
            "locator": f"test/{file_id}/data.csv",
            "sha": bytes(32),
        },
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_coordination_dispatch_identity_and_constraints(
    migrated_database: tuple[sa.Engine, Config],
) -> None:
    engine, config = migrated_database
    command.upgrade(config, "head")

    file_a = uuid.uuid4()
    file_b = uuid.uuid4()
    with engine.begin() as connection:
        _insert_supported_ingest_file(connection, file_id=file_a, object_id=uuid.uuid4())
        _insert_supported_ingest_file(connection, file_id=file_b, object_id=uuid.uuid4())

    def _insert_dispatch(connection, *, file_id, operation_id, target="VALIDATION", state="NEW"):
        connection.execute(
            sa.text(
                """INSERT INTO app.coordination_dispatch
                   (id, operation_id, file_id, downstream_target, state, correlation_id)
                   VALUES (gen_random_uuid(), :operation, :file, :target, :state, :correlation)"""
            ),
            {
                "operation": operation_id,
                "file": file_id,
                "target": target,
                "state": state,
                "correlation": uuid.uuid4(),
            },
        )

    operation_id = uuid.uuid4()
    with engine.begin() as connection:
        _insert_dispatch(connection, file_id=file_a, operation_id=operation_id)

    # Identidad idempotente (file_id, downstream_target)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_dispatch(connection, file_id=file_a, operation_id=uuid.uuid4())

    # Identidad estable de operación (operation_id único)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_dispatch(connection, file_id=file_b, operation_id=operation_id)

    # Estado inválido
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_dispatch(connection, file_id=file_b, operation_id=uuid.uuid4(), state="UNKNOWN")

    # Destino inválido
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_dispatch(connection, file_id=file_b, operation_id=uuid.uuid4(), target="OTHER")

    # FK hacia ingest_file
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_dispatch(connection, file_id=uuid.uuid4(), operation_id=uuid.uuid4())


def _current_revision(engine: sa.Engine) -> str | None:
    with engine.connect() as connection:
        if connection.execute(sa.text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
            return None
        return connection.execute(
            sa.text("SELECT version_num FROM public.alembic_version")
        ).scalar_one_or_none()


@pytest.fixture()
def analytical_database() -> tuple[sa.Engine, Config]:
    """Base desechable para la semántica de ``kpi_observation``.

    No usa ``downgrade base``; cada prueba fija el estado con ``upgrade`` y
    ``downgrade`` dirigidos sobre las revisiones analíticas.
    """
    url = _test_url()
    config = _config(url)
    engine = sa.create_engine(url)
    try:
        yield engine, config
    finally:
        engine.dispose()


def _insert_analytic_run(connection) -> uuid.UUID:
    run_id = uuid.uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO app.analytic_run "
            "(id, run_type, state, result, operation_id, correlation_id, completed_at) "
            "VALUES (:id, 'KPI_RECALCULATION', 'COMPLETED', 'OK', :operation, :correlation, "
            " CURRENT_TIMESTAMP)"
        ),
        {"id": run_id, "operation": uuid.uuid4(), "correlation": uuid.uuid4()},
    )
    return run_id


def _observation_insert():
    return sa.text(
        "INSERT INTO app.kpi_observation "
        "(id, analytic_run_id, kpi_code, period_start, period_end, dimensions, value, "
        " availability, as_of_date) "
        "VALUES (:id, :analytic_run_id, :kpi_code, :period_start, :period_end, "
        " CAST(:dimensions AS jsonb), :value, :availability, :as_of_date)"
    )


def _observation_params(run_id: uuid.UUID, **overrides) -> dict:
    params: dict = {
        "id": uuid.uuid4(),
        "analytic_run_id": run_id,
        "kpi_code": "KPI-RC-01",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "dimensions": "{}",
        "value": 10.0,
        "availability": "DISPONIBLE",
        "as_of_date": "2026-01-31",
    }
    params.update(overrides)
    return params


def _assert_constraint_violation(connection, statement, params, expected_constraint: str) -> None:
    """Ejecuta una sentencia inválida aislada en SAVEPOINT y verifica la constraint."""
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            connection.execute(statement, params)
        original = exc_info.value.orig
        assert isinstance(original, psycopg.errors.CheckViolation)
        assert original.diag.constraint_name == expected_constraint
    finally:
        savepoint.rollback()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_0012_refuses_upgrade_when_kpi_observation_has_rows(
    analytical_database: tuple[sa.Engine, Config],
) -> None:
    engine, config = analytical_database
    if _current_revision(engine) == "0012_kpi_observation_semantics":
        command.downgrade(config, "0011_document_manage_capability")
    command.upgrade(config, "0011_document_manage_capability")

    observation_id = uuid.uuid4()
    with engine.begin() as connection:
        run_id = _insert_analytic_run(connection)
        connection.execute(
            sa.text(
                "INSERT INTO app.kpi_observation "
                "(id, analytic_run_id, kpi_code, period_start, period_end, dimensions, value, availability) "
                "VALUES (:id, :run_id, 'KPI-RC-01', '2026-01-01', '2026-01-31', "
                " CAST('{}' AS jsonb), 10.0, 'DISPONIBLE')"
            ),
            {"id": observation_id, "run_id": run_id},
        )

    with pytest.raises(RuntimeError) as exc_info:
        command.upgrade(config, "0012_kpi_observation_semantics")
    assert "MIGRATION_0012_ABORT" in str(exc_info.value)

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT version_num FROM public.alembic_version")
        ).scalar_one() == "0011_document_manage_capability"

    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("kpi_observation", schema="app")}
    assert "as_of_date" not in columns
    constraints = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("kpi_observation", schema="app")
    }
    assert "ck_kpi_observation_period_start_monthly" not in constraints
    assert "ck_kpi_observation_period_end_monthly" not in constraints
    assert "ck_kpi_observation_dimensions_object" not in constraints

    with engine.begin() as connection:
        connection.execute(
            sa.text("DELETE FROM app.kpi_observation WHERE id = :id"), {"id": observation_id}
        )
        connection.execute(sa.text("DELETE FROM app.analytic_run WHERE id = :id"), {"id": run_id})

    command.upgrade(config, "0012_kpi_observation_semantics")


@pytest.fixture()
def kpi_observation_context(analytical_database: tuple[sa.Engine, Config]):
    engine, config = analytical_database
    command.upgrade(config, "0012_kpi_observation_semantics")
    with engine.begin() as connection:
        run_id = _insert_analytic_run(connection)
    try:
        yield engine, run_id
    finally:
        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM app.kpi_observation"))
            connection.execute(sa.text("DELETE FROM app.analytic_run"))


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_kpi_observation_period_start_must_be_first_day(kpi_observation_context) -> None:
    engine, run_id = kpi_observation_context
    statement = _observation_insert()
    # period_end coherente con la aritmética para aislar la constraint objetivo.
    params = _observation_params(run_id, period_start="2026-01-15", period_end="2026-02-14")
    with engine.begin() as connection:
        _assert_constraint_violation(
            connection, statement, params, "ck_kpi_observation_period_start_monthly"
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_kpi_observation_period_end_must_be_last_day(kpi_observation_context) -> None:
    engine, run_id = kpi_observation_context
    statement = _observation_insert()
    params = _observation_params(run_id, period_end="2026-01-30")
    with engine.begin() as connection:
        _assert_constraint_violation(
            connection, statement, params, "ck_kpi_observation_period_end_monthly"
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_kpi_observation_dimensions_must_be_object(kpi_observation_context) -> None:
    engine, run_id = kpi_observation_context
    statement = _observation_insert()
    params = _observation_params(run_id, dimensions="[]")
    with engine.begin() as connection:
        _assert_constraint_violation(
            connection, statement, params, "ck_kpi_observation_dimensions_object"
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_kpi_observation_dimensions_object_passes(kpi_observation_context) -> None:
    engine, run_id = kpi_observation_context
    statement = _observation_insert()
    params = _observation_params(run_id, dimensions='{"nivel_severidad": "alto"}')
    with engine.begin() as connection:
        connection.execute(statement, params)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_kpi_observation_as_of_date_is_not_null(kpi_observation_context) -> None:
    engine, run_id = kpi_observation_context
    statement = _observation_insert()
    params = _observation_params(run_id, as_of_date=None)
    with engine.begin() as connection:
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(IntegrityError) as exc_info:
                connection.execute(statement, params)
            original = exc_info.value.orig
            assert isinstance(original, psycopg.errors.NotNullViolation)
            assert original.diag.column_name == "as_of_date"
        finally:
            savepoint.rollback()

