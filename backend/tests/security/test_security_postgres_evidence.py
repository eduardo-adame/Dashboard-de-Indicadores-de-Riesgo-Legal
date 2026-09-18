"""Evidencia PostgreSQL para ACL persistida y auditoría transaccional."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from alembic import command
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from psycopg.rows import dict_row

from app.security.models import AuthenticationError, AuthorizationContext, DocumentAuthorizationResult
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService
from tests.data.test_postgres_migrations import _config, _test_url


@pytest.fixture(scope="module")
def security_database():
    url = _test_url(); config = _config(url); engine = sa.create_engine(url)
    command.downgrade(config, "base"); command.upgrade(config, "head")
    try: yield engine, make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    finally: command.downgrade(config, "base"); engine.dispose()


def _service(conninfo: str) -> SecurityService:
    import base64
    key = base64.urlsafe_b64encode(b"p" * 32).decode().rstrip("=")
    return SecurityService(SecurityRepository(conninfo), JwtService(issuer="issuer", audience="audience", keyring={"current": key}, active_kid="current"))


def _seed_actor(engine, service: SecurityService):
    account_id = uuid4(); password = "contraseña válida 123"; username = f"ti-{account_id.hex}"
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :username, 'TI', :hash)"), {"id": account_id, "username": username, "hash": service.passwords.hash(password)})
        connection.execute(sa.text("INSERT INTO app.user_role (user_id, role_id, active) VALUES (:id, 'TI', true)"), {"id": account_id})
    token, _ = service.login(username, password)
    return account_id, service.authenticated_principal(token)


@pytest.mark.requires_db
@pytest.mark.contract
def test_persisted_acl_precedence_subjects_and_trimming(security_database) -> None:
    engine, conninfo = security_database; service = _service(conninfo); account_id, principal = _seed_actor(engine, service)
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.document (id_documento, name, document_type, source_family) VALUES ('doc-deny', 'Deny', 'T', 'LITIGIOS'), ('doc-allow', 'Allow', 'T', 'CUMPLIMIENTO'), ('doc-hidden', 'Hidden', 'T', 'CUMPLIMIENTO')"))
    context = AuthorizationContext(principal, uuid4())
    assert service.document_authorization(context, document_id="doc-hidden", source_family="CUMPLIMIENTO").result is DocumentAuthorizationResult.DEFAULT_DENY
    service.set_scope_grant(principal, "TI", "LITIGIOS", True)
    token, _ = service.login(principal.username, "contraseña válida 123"); principal = service.authenticated_principal(token); context = AuthorizationContext(principal, uuid4())
    assert service.document_authorization(context, document_id="doc-deny", source_family="LITIGIOS").permitted
    service.set_document_exception(principal, account_id=account_id, role_id=None, document_id="doc-allow", decision="ALLOW", active=True)
    token, _ = service.login(principal.username, "contraseña válida 123"); principal = service.authenticated_principal(token); context = AuthorizationContext(principal, uuid4())
    assert service.document_authorization(context, document_id="doc-allow", source_family="CUMPLIMIENTO").permitted
    service.set_document_exception(principal, account_id=None, role_id="TI", document_id="doc-deny", decision="DENY", active=True)
    token, _ = service.login(principal.username, "contraseña válida 123"); context = AuthorizationContext(service.authenticated_principal(token), uuid4())
    decision = service.document_authorization(context, document_id="doc-deny", source_family="LITIGIOS")
    assert decision.result is DocumentAuthorizationResult.EXPLICIT_DENY
    predicate, params = decision.scope.database_predicate("d")
    with engine.connect() as connection:
        ids = connection.exec_driver_sql(f"SELECT d.id_documento FROM app.document d WHERE {predicate}", params).scalars().all()
    assert "doc-deny" not in ids and "doc-allow" in ids and "doc-hidden" not in ids


@pytest.mark.requires_db
@pytest.mark.contract
def test_postgres_audit_failure_rolls_back_security_mutation(security_database, monkeypatch) -> None:
    engine, conninfo = security_database; service = _service(conninfo); _, principal = _seed_actor(engine, service)
    def fail_audit(*args, **kwargs): raise RuntimeError("controlled audit failure")
    monkeypatch.setattr(service.repository, "write_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="controlled audit failure"):
        service.create_account(principal, username="rollback-user", display_name="Rollback", password="contraseña válida 123", roles=frozenset({"JURIDICO"}))
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.user_account WHERE username = 'rollback-user'")).scalar_one() == 0
        assert connection.execute(sa.text("SELECT count(*) FROM audit.event WHERE resource_identifier = 'rollback-user'")).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.contract
def test_transactional_revalidation_and_required_audit_share_one_database_transaction(security_database, monkeypatch) -> None:
    engine, conninfo = security_database; service = _service(conninfo); _, principal = _seed_actor(engine, service)
    target_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :username, 'Target', :hash)"),
            {"id": target_id, "username": f"target-{target_id.hex}", "hash": service.passwords.hash("contraseña válida 123")},
        )

    def fail_audit(*args, **kwargs):
        raise RuntimeError("controlled audit failure")

    monkeypatch.setattr(service.repository, "write_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="controlled audit failure"):
        with service.repository.transaction() as connection:
            current = service.revalidate_functional_access(connection, principal, "user.update")
            assert current.account_id == principal.account_id
            assert service.repository.update_account(connection, target_id, "Changed")
            service.repository.write_audit_event(
                connection,
                actor=current,
                action="PROTECTED_MUTATION",
                resource_type="USER",
                resource_identifier=str(target_id),
                result="SUCCESS",
                correlation_id=uuid4(),
            )

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT display_name FROM app.user_account WHERE id = :id"), {"id": target_id}
        ).scalar_one() == "Target"


@pytest.mark.requires_db
@pytest.mark.contract
def test_transactional_revalidation_serializes_revocation_before_a_later_mutation(security_database) -> None:
    engine, conninfo = security_database; service = _service(conninfo); account_id, principal = _seed_actor(engine, service)

    with service.repository.transaction() as protected_connection:
        service.revalidate_functional_access(protected_connection, principal, "user.update")
        with psycopg.connect(conninfo, row_factory=dict_row) as revocation_connection:
            with revocation_connection.transaction():
                revocation_connection.execute("SET LOCAL lock_timeout = '200ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    service.repository.invalidate_sessions(revocation_connection, [account_id])

    with service.repository.transaction() as revocation_connection:
        service.repository.invalidate_sessions(revocation_connection, [account_id])

    with service.repository.transaction() as later_connection:
        with pytest.raises(AuthenticationError):
            service.revalidate_functional_access(later_connection, principal, "user.update")


@pytest.mark.requires_db
@pytest.mark.contract
def test_transactional_denial_is_audited_without_confirming_a_mutation(security_database) -> None:
    engine, conninfo = security_database; service = _service(conninfo); account_id, principal = _seed_actor(engine, service)

    with service.repository.transaction() as revocation_connection:
        service.repository.invalidate_sessions(revocation_connection, [account_id])

    with service.repository.transaction() as connection:
        with pytest.raises(AuthenticationError):
            service.revalidate_functional_access(connection, principal, "user.update")
        service.repository.write_audit_event(
            connection,
            actor=principal,
            action="AUTHORIZATION_DENIED",
            resource_type="CAPABILITY",
            resource_identifier="user.update",
            result="DENIED",
            correlation_id=uuid4(),
            safe_cause_code="INVALID_SESSION",
        )

    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE action = 'AUTHORIZATION_DENIED' AND actor_user_id = :id"),
            {"id": account_id},
        ).scalar_one() == 1
