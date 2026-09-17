"""Evidencia PostgreSQL para ACL persistida y auditoría transaccional."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from alembic import command
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.security.models import AuthorizationContext, DocumentAuthorizationResult
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
