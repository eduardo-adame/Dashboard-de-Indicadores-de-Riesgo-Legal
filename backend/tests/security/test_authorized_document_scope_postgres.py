"""Evidencia PostgreSQL del alcance documental autorizado y caller-owned."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import secrets
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.security.models import AuthenticatedPrincipal, AuthenticationError, AuthorizationError
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from scripts.provision_runtime_role import provision


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def authorized_scope_database() -> tuple[dict[str, object], dict[str, object]]:
    """Crea una base y un LOGIN runtime desechables sobre PostgreSQL real."""
    source_url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not source_url:
        pytest.fail("DATA_TEST_DATABASE_URL es obligatoria para esta evidencia")
    parsed = make_url(source_url)
    source_database = parsed.database or ""
    if "test" not in source_database.lower() or source_database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("DATA_TEST_DATABASE_URL debe ser una base administradora dedicada de pruebas")

    database_name = f"authorized_scope_test_{uuid4().hex}"
    test_url = parsed.set(database=database_name).render_as_string(hide_password=False)
    runtime_login = f"rl_scope_{uuid4().hex}"
    runtime_password = secrets.token_urlsafe(32)
    owner = {
        "host": parsed.host,
        "port": parsed.port or 5432,
        "dbname": database_name,
        "user": parsed.username,
        "password": parsed.password,
    }
    runtime = {
        **owner,
        "user": runtime_login,
        "password": runtime_password,
    }
    previous = {
        key: os.environ.get(key)
        for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    }
    admin = sa.create_engine(source_url, isolation_level="AUTOCOMMIT")
    created = False
    provisioned = False
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        created = True
        os.environ.update(
            POSTGRES_HOST=parsed.host or "localhost",
            POSTGRES_PORT=str(parsed.port or 5432),
            POSTGRES_USER=parsed.username or "",
            POSTGRES_PASSWORD=parsed.password or "",
            POSTGRES_DB=database_name,
        )
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        command.upgrade(config, "0016_rag_operation_lifecycle")
        with psycopg.connect(**owner, autocommit=True) as connection:
            provision(connection, login=runtime_login, password=runtime_password)
        provisioned = True
        yield owner, runtime
    finally:
        if provisioned:
            source_owner = {
                "host": parsed.host,
                "port": parsed.port or 5432,
                "dbname": source_database,
                "user": parsed.username,
                "password": parsed.password,
            }
            with psycopg.connect(**source_owner, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime_login)))
        if created:
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def clean_scope_rows(authorized_scope_database) -> None:
    """Aísla los casos sin recrear el esquema entre assertions."""
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        connection.execute("DELETE FROM audit.event_resource")
        connection.execute("DELETE FROM audit.event")
        connection.execute("DELETE FROM app.document_exception")
        connection.execute("DELETE FROM app.document_scope_grant")
        connection.execute("DELETE FROM app.access_session")
        connection.execute("DELETE FROM app.user_role")
        connection.execute("DELETE FROM app.user_account")
        connection.execute("DELETE FROM app.document")


def _seed_identity(
    connection: psycopg.Connection,
    *,
    roles: tuple[str, ...] = ("ANALISTA",),
    account_version: int = 1,
    session_version: int | None = None,
    session_state: str = "ACTIVE",
    expired: bool = False,
) -> AuthenticatedPrincipal:
    account_id = uuid4()
    session_id = uuid4()
    now = datetime.now(UTC)
    started_at = now - timedelta(days=2) if expired else now
    expires_at = now - timedelta(days=1) if expired else now + timedelta(hours=4)
    invalidated_at = None if session_state == "ACTIVE" else now
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO app.user_account
               (id, username, display_name, password_hash, authorization_version)
               VALUES (%s, %s, %s, 'not-used', %s)""",
            (account_id, f"scope-{account_id}", "Scope User", account_version),
        )
        cursor.execute(
            """INSERT INTO app.access_session
               (id, user_id, refresh_token_sha256, authorization_version, state,
                started_at, expires_at, invalidated_at, correlation_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                session_id,
                account_id,
                b"s" * 32,
                account_version if session_version is None else session_version,
                session_state,
                started_at,
                expires_at,
                invalidated_at,
                uuid4(),
            ),
        )
        for role in roles:
            cursor.execute(
                "INSERT INTO app.user_role (user_id, role_id) VALUES (%s, %s)",
                (account_id, role),
            )
    return AuthenticatedPrincipal(
        account_id,
        session_id,
        f"scope-{account_id}",
        account_version,
        frozenset(roles),
        frozenset({"document.query"}),
    )


def _grant(connection: psycopg.Connection, role: str, family: str, *, active: bool = True) -> None:
    connection.execute(
        """INSERT INTO app.document_scope_grant (id, role_id, source_family, active)
           VALUES (%s, %s, %s, %s)""",
        (uuid4(), role, family, active),
    )


def _document(connection: psycopg.Connection, document_id: str, family: str) -> None:
    connection.execute(
        """INSERT INTO app.document (id_documento, name, document_type, source_family)
           VALUES (%s, %s, 'TEST', %s)""",
        (document_id, document_id, family),
    )


def _exception(
    connection: psycopg.Connection,
    document_id: str,
    decision: str,
    *,
    account_id: UUID | None = None,
    role_id: str | None = None,
    active: bool = True,
) -> None:
    connection.execute(
        """INSERT INTO app.document_exception
           (id, user_id, role_id, id_documento, decision, active, revoked_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (uuid4(), account_id, role_id, document_id, decision, active, None if active else datetime.now(UTC)),
    )


def _service() -> SecurityService:
    return SecurityService(SecurityRepository("caller-owned"))


def _authorized_documents(connection: psycopg.Connection, scope) -> list[str]:
    predicate, parameters = scope.database_predicate("d")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT d.id_documento FROM app.document d WHERE {predicate} ORDER BY d.id_documento",
            parameters,
        )
        return [str(row["id_documento"]) for row in cursor.fetchall()]


@pytest.mark.requires_db
@pytest.mark.parametrize(
    ("role", "family"),
    [
        ("JURIDICO", "CONTRATOS_DOCUMENTOS"),
        ("ANALISTA", "LITIGIOS"),
        ("TI", "AUDITORIA_INTERNA"),
    ],
)
def test_each_authenticated_mvp_role_gets_its_current_family_scope(
    authorized_scope_database,
    role: str,
    family: str,
) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection, roles=(role,))
        _grant(connection, role, family)

        scope = _service().authorized_document_scope(connection, principal)

        assert scope.account_id == principal.account_id
        assert scope.role_ids == frozenset({role})
        assert scope.allowed_families == frozenset({family})


@pytest.mark.requires_db
def test_deny_precedes_family_grant_across_current_roles(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection, roles=("JURIDICO", "ANALISTA"))
        _grant(connection, "JURIDICO", "LITIGIOS")
        _document(connection, "LIT-ALLOWED", "LITIGIOS")
        _document(connection, "LIT-DENIED", "LITIGIOS")
        _exception(connection, "LIT-DENIED", "DENY", role_id="ANALISTA")

        scope = _service().authorized_document_scope(connection, principal)

        assert _authorized_documents(connection, scope) == ["LIT-ALLOWED"]


@pytest.mark.requires_db
def test_explicit_deny_overrides_explicit_allow_and_family_grant(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection, roles=("JURIDICO", "ANALISTA"))
        _grant(connection, "JURIDICO", "CUMPLIMIENTO")
        _document(connection, "CMP-DENIED", "CUMPLIMIENTO")
        _exception(connection, "CMP-DENIED", "ALLOW", account_id=principal.account_id)
        _exception(connection, "CMP-DENIED", "DENY", role_id="ANALISTA")

        scope = _service().authorized_document_scope(connection, principal)

        assert _authorized_documents(connection, scope) == []


@pytest.mark.requires_db
def test_explicit_allow_includes_document_without_family_grant(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _document(connection, "AUDIT-ALLOWED", "AUDITORIA_INTERNA")
        _exception(connection, "AUDIT-ALLOWED", "ALLOW", account_id=principal.account_id)

        scope = _service().authorized_document_scope(connection, principal)

        assert scope.allowed_families == frozenset()
        assert _authorized_documents(connection, scope) == ["AUDIT-ALLOWED"]


@pytest.mark.requires_db
def test_empty_authorized_scope_is_not_a_functional_denial(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _document(connection, "DEFAULT-DENIED", "CUMPLIMIENTO")

        scope = _service().authorized_document_scope(connection, principal)

        assert scope.allowed_families == frozenset()
        assert _authorized_documents(connection, scope) == []


@pytest.mark.requires_db
def test_inactive_grant_other_family_and_default_deny_are_excluded(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _grant(connection, "ANALISTA", "LITIGIOS", active=True)
        _grant(connection, "ANALISTA", "CUMPLIMIENTO", active=False)
        _document(connection, "LIT-VISIBLE", "LITIGIOS")
        _document(connection, "CMP-INACTIVE", "CUMPLIMIENTO")
        _document(connection, "AUD-DEFAULT-DENY", "AUDITORIA_INTERNA")

        scope = _service().authorized_document_scope(connection, principal)

        assert scope.allowed_families == frozenset({"LITIGIOS"})
        assert _authorized_documents(connection, scope) == ["LIT-VISIBLE"]


@pytest.mark.requires_db
def test_inactive_role_does_not_contribute_its_family_grant(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        stale = _seed_identity(connection, roles=("JURIDICO", "ANALISTA"))
        _grant(connection, "JURIDICO", "CONTRATOS_DOCUMENTOS")
        _grant(connection, "ANALISTA", "LITIGIOS")
        connection.execute(
            """UPDATE app.user_role SET active = false, revoked_at = CURRENT_TIMESTAMP
               WHERE user_id = %s AND role_id = 'ANALISTA'""",
            (stale.account_id,),
        )

        scope = _service().authorized_document_scope(connection, stale)

        assert scope.role_ids == frozenset({"JURIDICO"})
        assert scope.allowed_families == frozenset({"CONTRATOS_DOCUMENTOS"})


@pytest.mark.requires_db
def test_functional_denial_returns_no_scope_metadata_and_writes_no_audit(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection, roles=())
        before = connection.execute("SELECT count(*) AS count FROM audit.event").fetchone()["count"]

        with pytest.raises(AuthorizationError, match="^Acceso no autorizado$") as raised:
            _service().authorized_document_scope(connection, principal)

        after = connection.execute("SELECT count(*) AS count FROM audit.event").fetchone()["count"]
        assert raised.value.args == ("Acceso no autorizado",)
        assert after == before


@pytest.mark.requires_db
@pytest.mark.parametrize(
    ("session_state", "expired", "account_version", "session_version", "principal_version"),
    [
        ("INVALIDATED", False, 1, 1, 1),
        ("ACTIVE", True, 1, 1, 1),
        ("ACTIVE", False, 2, 2, 1),
    ],
)
def test_invalid_session_or_authorization_version_fails_closed(
    authorized_scope_database,
    session_state: str,
    expired: bool,
    account_version: int,
    session_version: int,
    principal_version: int,
) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        current = _seed_identity(
            connection,
            session_state=session_state,
            expired=expired,
            account_version=account_version,
            session_version=session_version,
        )
        stale = AuthenticatedPrincipal(
            current.account_id,
            current.session_id,
            current.username,
            principal_version,
            current.roles,
            current.permissions,
        )

        with pytest.raises(AuthenticationError, match="^Credenciales o sesión no válidas$"):
            _service().authorized_document_scope(connection, stale)


@pytest.mark.requires_db
def test_role_change_is_reloaded_instead_of_trusting_stale_claims(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        stale = _seed_identity(connection, roles=("JURIDICO",))
        connection.execute(
            """UPDATE app.user_role SET active = false, revoked_at = CURRENT_TIMESTAMP
               WHERE user_id = %s AND role_id = 'JURIDICO'""",
            (stale.account_id,),
        )
        connection.execute(
            "INSERT INTO app.user_role (user_id, role_id) VALUES (%s, 'ANALISTA')",
            (stale.account_id,),
        )
        _grant(connection, "ANALISTA", "CUMPLIMIENTO")

        scope = _service().authorized_document_scope(connection, stale)

        assert scope.role_ids == frozenset({"ANALISTA"})
        assert scope.allowed_families == frozenset({"CUMPLIMIENTO"})


@pytest.mark.requires_db
def test_scope_reuses_caller_transaction_without_committing_or_rolling_back(authorized_scope_database) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _grant(connection, "ANALISTA", "LITIGIOS")
        assert connection.info.transaction_status == TransactionStatus.INTRANS

        scope = _service().authorized_document_scope(connection, principal)

        assert scope.allowed_families == frozenset({"LITIGIOS"})
        assert connection.info.transaction_status == TransactionStatus.INTRANS


@pytest.mark.requires_db
@pytest.mark.parametrize("candidate_order", ["d.id_documento ASC", "d.name DESC"])
def test_scope_predicate_filters_before_vector_or_lexical_candidate_ordering(
    authorized_scope_database,
    candidate_order: str,
) -> None:
    owner, _ = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _grant(connection, "ANALISTA", "LITIGIOS")
        _document(connection, "AUTHORIZED-A", "LITIGIOS")
        _document(connection, "AUTHORIZED-B", "LITIGIOS")
        _document(connection, "UNAUTHORIZED", "CUMPLIMIENTO")
        scope = _service().authorized_document_scope(connection, principal)
        predicate, parameters = scope.database_predicate("d")

        with connection.cursor() as cursor:
            cursor.execute(
                f"""WITH authorized_corpus AS MATERIALIZED (
                       SELECT d.id_documento, d.name
                       FROM app.document d
                       WHERE {predicate}
                     )
                     SELECT d.id_documento
                     FROM authorized_corpus d
                     ORDER BY {candidate_order}
                     LIMIT 10""",
                parameters,
            )
            identifiers = {str(row["id_documento"]) for row in cursor.fetchall()}

        assert identifiers == {"AUTHORIZED-A", "AUTHORIZED-B"}


@pytest.mark.requires_db
def test_runtime_login_can_revalidate_and_filter_authorized_documents(authorized_scope_database) -> None:
    owner, runtime = authorized_scope_database
    with psycopg.connect(**owner, row_factory=dict_row) as connection:
        principal = _seed_identity(connection)
        _grant(connection, "ANALISTA", "CONTRATOS_DOCUMENTOS")
        _document(connection, "CONTRACT-VISIBLE", "CONTRATOS_DOCUMENTOS")
        _document(connection, "LIT-HIDDEN", "LITIGIOS")
        connection.commit()

    with psycopg.connect(**runtime, row_factory=dict_row) as connection:
        scope = _service().authorized_document_scope(connection, principal)
        assert scope.allowed_families == frozenset({"CONTRATOS_DOCUMENTOS"})
        assert _authorized_documents(connection, scope) == ["CONTRACT-VISIBLE"]
        assert connection.info.transaction_status == TransactionStatus.INTRANS
