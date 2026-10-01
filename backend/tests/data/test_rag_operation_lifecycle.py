"""Contrato PostgreSQL del ciclo de vida terminal de una operación RAG."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION = "0016_rag_operation_lifecycle"
PREVIOUS_REVISION = "0015_runtime_privs"


def _source_url() -> str:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {
        "riesgo_legal",
        "riesgo_legal_test",
    }:
        pytest.fail("a dedicated RAG lifecycle test database is required")
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


@contextmanager
def _isolated_database(target: str):
    source = _source_url()
    parsed = make_url(source)
    database_name = f"rag_operation_lifecycle_test_{uuid.uuid4().hex}"
    admin = sa.create_engine(source, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
        engine = sa.create_engine(isolated_url)
        config = _config(isolated_url)
        command.upgrade(config, target)
        yield engine, config
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()


@pytest.fixture(scope="module")
def database():
    with _isolated_database("head") as isolated:
        yield isolated


@pytest.fixture()
def connection(database):
    engine, _ = database
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _seed_principal(conn) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO app.user_account (id, username, display_name, password_hash) "
            "VALUES (:id, :username, 'RAG lifecycle fixture', 'test-only-hash')"
        ),
        {"id": user_id, "username": f"rag_lifecycle_{user_id.hex}"},
    )
    conn.execute(
        sa.text(
            "INSERT INTO app.access_session "
            "(id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
            "VALUES (:id, :user_id, :digest, 1, 'ACTIVE', CURRENT_TIMESTAMP + interval '1 hour')"
        ),
        {"id": session_id, "user_id": user_id, "digest": bytes(32)},
    )
    return user_id, session_id


def _operation_values(**overrides):
    values = {
        "id": uuid.uuid4(),
        "query": bytes(range(32)),
        "state": "EVIDENCIA_SUFICIENTE",
        "response": "Respuesta autorizada",
        "message": None,
        "context": None,
        "operation": uuid.uuid4(),
        "correlation": uuid.uuid4(),
        "operation_status": "COMPLETED",
        "generation_status": "SUCCEEDED",
        "cause": None,
    }
    values.update(overrides)
    return values


def _insert_operation(conn, user_id, session_id, **overrides):
    values = _operation_values(**overrides)
    values.update(user_id=user_id, session_id=session_id)
    conn.execute(
        sa.text(
            "INSERT INTO app.rag_operation "
            "(id, user_id, session_id, query_sha256, state, generated_response, "
            "safe_result_message, context_reference_id, operation_id, correlation_id, "
            "operation_status, generation_status, safe_cause_code) "
            "VALUES (:id, :user_id, :session_id, :query, :state, :response, :message, "
            ":context, :operation, :correlation, :operation_status, :generation_status, :cause)"
        ),
        values,
    )
    return values


def _insert_legacy_operation(conn, user_id, session_id, **overrides):
    values = _operation_values(**overrides)
    values.update(user_id=user_id, session_id=session_id)
    conn.execute(
        sa.text(
            "INSERT INTO app.rag_operation "
            "(id, user_id, session_id, query_sha256, state, generated_response, "
            "safe_result_message, context_reference_id, operation_id, correlation_id) "
            "VALUES (:id, :user_id, :session_id, :query, :state, :response, :message, "
            ":context, :operation, :correlation)"
        ),
        values,
    )
    return values


def _assert_rejected(conn, user_id, session_id, **overrides):
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(IntegrityError):
            _insert_operation(conn, user_id, session_id, **overrides)
    finally:
        savepoint.rollback()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_revision_chain_has_single_0016_head(database):
    engine, config = database
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == PREVIOUS_REVISION
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == REVISION


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_lifecycle_columns_and_nullability(database):
    engine, _ = database
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns("rag_operation", schema="app")}
    assert columns["state"]["nullable"] is True
    assert columns["operation_status"]["nullable"] is False
    assert columns["generation_status"]["nullable"] is False
    assert columns["safe_cause_code"]["nullable"] is True


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"state": "EVIDENCIA_SUFICIENTE", "operation_status": "FAILED", "generation_status": "FAILED", "cause": "PROVIDER_TIMEOUT", "response": None, "message": "Proveedor no disponible"},
        {"state": "EVIDENCIA_INSUFICIENTE", "generation_status": "NOT_REQUESTED", "response": None, "message": "Evidencia insuficiente"},
        {"state": "SIN_EVIDENCIA", "generation_status": "NOT_REQUESTED", "response": None, "message": "Sin evidencia"},
        {"state": "SIN_AUTORIZACION", "generation_status": "NOT_REQUESTED", "response": None, "message": "Sin autorización"},
        {"state": None, "operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "cause": "RETRIEVAL_FAILED", "response": None, "message": "No fue posible recuperar evidencia"},
    ],
)
def test_exact_terminal_matrix_accepts_only_valid_cases(connection, overrides):
    user_id, session_id = _seed_principal(connection)
    _insert_operation(connection, user_id, session_id, **overrides)


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_status": "STARTED"},
        {"generation_status": "PENDING"},
        {"cause": "UNKNOWN_CAUSE"},
        {"response": ""},
        {"state": "EVIDENCIA_INSUFICIENTE", "generation_status": "NOT_REQUESTED", "response": None, "message": None},
        {"state": "SIN_EVIDENCIA", "generation_status": "SUCCEEDED", "response": None, "message": "Sin evidencia"},
        {"state": "SIN_AUTORIZACION", "operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "response": None, "message": "Sin autorización", "cause": "INTERNAL_FAILURE"},
        {"state": None, "operation_status": "COMPLETED", "generation_status": "NOT_REQUESTED", "response": None, "message": "Fallo previo", "cause": "INTERNAL_FAILURE"},
        {"state": "EVIDENCIA_SUFICIENTE", "operation_status": "FAILED", "generation_status": "FAILED", "response": None, "message": "Fallo", "cause": None},
        {"state": "EVIDENCIA_SUFICIENTE", "operation_status": "COMPLETED", "generation_status": "SUCCEEDED", "message": "No debe coexistir"},
    ],
)
def test_invalid_terminal_combinations_are_rejected(connection, overrides):
    user_id, session_id = _seed_principal(connection)
    _assert_rejected(connection, user_id, session_id, **overrides)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_reject_null_state_completed_generation_succeeded(connection):
    user_id, session_id = _seed_principal(connection)
    _assert_rejected(
        connection,
        user_id,
        session_id,
        state=None,
        operation_status="COMPLETED",
        generation_status="SUCCEEDED",
        cause=None,
        response="Respuesta que no debe persistirse",
        message=None,
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(
    "overrides",
    [
        {"operation_status": "COMPLETED", "generation_status": "FAILED", "cause": "INTERNAL_FAILURE", "response": None, "message": "Fallo"},
        {"operation_status": "FAILED", "generation_status": "FAILED", "cause": "INTERNAL_FAILURE", "response": None, "message": "Fallo"},
        {"operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "cause": None, "response": None, "message": "Fallo"},
        {"operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "cause": "INTERNAL_FAILURE", "response": "Respuesta inválida", "message": "Fallo"},
        {"operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "cause": "INTERNAL_FAILURE", "response": None, "message": None},
        {"operation_status": "FAILED", "generation_status": "NOT_REQUESTED", "cause": "INTERNAL_FAILURE", "response": None, "message": ""},
    ],
)
def test_null_state_invalid_matrix_is_rejected(connection, overrides):
    user_id, session_id = _seed_principal(connection)
    _assert_rejected(connection, user_id, session_id, state=None, **overrides)


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(
    "cause",
    [
        "EMBEDDING_UNAVAILABLE",
        "RETRIEVAL_FAILED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "INVALID_PROVIDER_OUTPUT",
        "CONTEXT_LIMIT_EXCEEDED",
        "INTERNAL_FAILURE",
    ],
)
def test_safe_cause_whitelist_is_complete(connection, cause):
    user_id, session_id = _seed_principal(connection)
    _insert_operation(
        connection,
        user_id,
        session_id,
        operation_status="FAILED",
        generation_status="FAILED",
        cause=cause,
        response=None,
        message="Fallo seguro",
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_operation_id_uniqueness_is_preserved(connection):
    user_id, session_id = _seed_principal(connection)
    operation_id = uuid.uuid4()
    _insert_operation(connection, user_id, session_id, operation=operation_id)
    _assert_rejected(connection, user_id, session_id, operation=operation_id)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_rag_final_fragment_relationship_is_preserved(database):
    engine, _ = database
    foreign_keys = sa.inspect(engine).get_foreign_keys("rag_final_fragment", schema="app")
    assert any(
        fk["referred_table"] == "rag_operation"
        and fk["constrained_columns"] == ["rag_operation_id"]
        and fk["options"].get("ondelete") == "RESTRICT"
        for fk in foreign_keys
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_upgrade_backfills_unambiguous_legacy_rows():
    with _isolated_database(PREVIOUS_REVISION) as (engine, config):
        with engine.begin() as conn:
            user_id, session_id = _seed_principal(conn)
            expected = {}
            for state, response, message in (
                ("EVIDENCIA_SUFICIENTE", "Respuesta", None),
                ("EVIDENCIA_INSUFICIENTE", None, "Insuficiente"),
                ("SIN_EVIDENCIA", None, "Sin evidencia"),
                ("SIN_AUTORIZACION", None, "Sin autorización"),
            ):
                row = _insert_legacy_operation(
                    conn,
                    user_id,
                    session_id,
                    state=state,
                    response=response,
                    message=message,
                )
                expected[row["id"]] = "SUCCEEDED" if state == "EVIDENCIA_SUFICIENTE" else "NOT_REQUESTED"
        command.upgrade(config, REVISION)
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, operation_status, generation_status, safe_cause_code "
                "FROM app.rag_operation"
            )).mappings()
            assert {
                row["id"]: (row["operation_status"], row["generation_status"], row["safe_cause_code"])
                for row in rows
            } == {key: ("COMPLETED", value, None) for key, value in expected.items()}


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(
    "legacy",
    [
        {"state": "EVIDENCIA_SUFICIENTE", "response": None, "message": None},
        {"state": "EVIDENCIA_SUFICIENTE", "response": "Respuesta", "message": "Ambiguo"},
        {"state": "SIN_EVIDENCIA", "response": None, "message": None},
    ],
)
def test_upgrade_aborts_for_ambiguous_legacy_rows(legacy):
    with _isolated_database(PREVIOUS_REVISION) as (engine, config):
        with engine.begin() as conn:
            user_id, session_id = _seed_principal(conn)
            row = _insert_legacy_operation(conn, user_id, session_id, **legacy)
        with pytest.raises(DBAPIError, match="MIGRATION_0016_UPGRADE_ABORT"):
            command.upgrade(config, REVISION)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == PREVIOUS_REVISION
            assert conn.execute(sa.text("SELECT count(*) FROM app.rag_operation WHERE id = :id"), {"id": row["id"]}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_downgrade_preserves_representable_history_and_reupgrade():
    with _isolated_database(REVISION) as (engine, config):
        with engine.begin() as conn:
            user_id, session_id = _seed_principal(conn)
            first = _insert_operation(conn, user_id, session_id)
            second = _insert_operation(
                conn,
                user_id,
                session_id,
                state="SIN_EVIDENCIA",
                generation_status="NOT_REQUESTED",
                response=None,
                message="Sin evidencia",
            )
        command.downgrade(config, PREVIOUS_REVISION)
        with engine.connect() as conn:
            columns = {column["name"] for column in sa.inspect(conn).get_columns("rag_operation", schema="app")}
            assert {"operation_status", "generation_status", "safe_cause_code"}.isdisjoint(columns)
            assert conn.execute(
                sa.text("SELECT count(*) FROM app.rag_operation WHERE id IN (:first, :second)"),
                {"first": first["id"], "second": second["id"]},
            ).scalar_one() == 2
        command.upgrade(config, REVISION)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == REVISION


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_downgrade_aborts_without_losing_nonrepresentable_evidence():
    with _isolated_database(REVISION) as (engine, config):
        with engine.begin() as conn:
            user_id, session_id = _seed_principal(conn)
            row = _insert_operation(
                conn,
                user_id,
                session_id,
                operation_status="FAILED",
                generation_status="FAILED",
                cause="PROVIDER_TIMEOUT",
                response=None,
                message="Proveedor no disponible",
            )
        with pytest.raises(DBAPIError, match="MIGRATION_0016_DOWNGRADE_ABORT"):
            command.downgrade(config, PREVIOUS_REVISION)
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == REVISION
            assert conn.execute(sa.text("SELECT count(*) FROM app.rag_operation WHERE id = :id"), {"id": row["id"]}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_schema_roles_and_runtime_grants_remain_unchanged():
    with _isolated_database(PREVIOUS_REVISION) as (engine, config):
        inspector = sa.inspect(engine)
        before_event = [(item["name"], str(item["type"]), item["nullable"]) for item in inspector.get_columns("event", schema="audit")]
        before_resource = [(item["name"], str(item["type"]), item["nullable"]) for item in inspector.get_columns("event_resource", schema="audit")]
        with engine.connect() as conn:
            before_roles = tuple(conn.execute(sa.text(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname IN ('riesgo_legal_runtime', 'riesgo_legal_audit_reader') ORDER BY rolname"
            )))
            before_privileges = tuple(conn.execute(sa.text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'riesgo_legal_runtime' AND table_schema = 'app' "
                "AND table_name = 'rag_operation' ORDER BY privilege_type"
            )).scalars())
        command.upgrade(config, REVISION)
        inspector = sa.inspect(engine)
        after_event = [(item["name"], str(item["type"]), item["nullable"]) for item in inspector.get_columns("event", schema="audit")]
        after_resource = [(item["name"], str(item["type"]), item["nullable"]) for item in inspector.get_columns("event_resource", schema="audit")]
        with engine.connect() as conn:
            after_roles = tuple(conn.execute(sa.text(
                "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname IN ('riesgo_legal_runtime', 'riesgo_legal_audit_reader') ORDER BY rolname"
            )))
            after_privileges = tuple(conn.execute(sa.text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'riesgo_legal_runtime' AND table_schema = 'app' "
                "AND table_name = 'rag_operation' ORDER BY privilege_type"
            )).scalars())
        assert after_event == before_event
        assert after_resource == before_resource
        assert after_roles == before_roles
        assert after_privileges == before_privileges
