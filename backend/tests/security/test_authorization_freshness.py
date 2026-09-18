"""Pruebas de revalidación transaccional de autorización."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.security.models import AuthenticatedPrincipal, AuthenticationError, AuthorizationError
from app.security.service import SecurityService
from app.security.tokens import JwtService


class RepositoryStub:
    def __init__(self, current: AuthenticatedPrincipal | None) -> None:
        self.current = current
        self.created = False
        self.audit_actions: list[str] = []
        self.transaction_calls = 0
        self.connections: list[object] = []

    @contextmanager
    def transaction(self):
        self.transaction_calls += 1
        yield self

    def principal_for_session(self, connection, account_id, session_id, authorization_version):
        self.connections.append(connection)
        return self.current

    def create_account(self, connection, **kwargs):
        self.created = True
        return uuid4()

    def set_roles(self, connection, account_id, roles):
        return None

    def write_audit_event(self, connection, **kwargs):
        self.audit_actions.append(kwargs["action"])


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "ti", 1, frozenset({"TI"}), frozenset({"user.create"}))


def service(repository: RepositoryStub) -> SecurityService:
    key = base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("=")
    return SecurityService(repository, JwtService(issuer="issuer", audience="audience", keyring={"current": key}, active_kid="current"))


@pytest.mark.contract
def test_mutation_confirms_when_authorization_is_current_in_transaction() -> None:
    actor = principal()
    repository = RepositoryStub(actor)
    service(repository).create_account(actor, username="new", display_name="Nuevo", password="contraseña válida 123", roles=frozenset({"JURIDICO"}))
    assert repository.created is True


@pytest.mark.contract
def test_mutation_does_not_confirm_when_authorization_was_revoked_before_transaction() -> None:
    actor = principal()
    repository = RepositoryStub(None)
    with pytest.raises(AuthenticationError):
        service(repository).create_account(actor, username="new", display_name="Nuevo", password="contraseña válida 123", roles=frozenset({"JURIDICO"}))
    assert repository.created is False


@pytest.mark.contract
def test_stale_authorization_version_cannot_authorize_a_mutation() -> None:
    actor = principal()
    stale = AuthenticatedPrincipal(actor.account_id, actor.session_id, actor.username, 2, actor.roles, actor.permissions)
    repository = RepositoryStub(None)
    with pytest.raises(AuthenticationError):
        service(repository).create_account(stale, username="new", display_name="Nuevo", password="contraseña válida 123", roles=frozenset({"JURIDICO"}))
    assert repository.created is False


@pytest.mark.contract
def test_rejected_session_is_audited_without_persisting_token_material() -> None:
    actor = principal()
    repository = RepositoryStub(None)
    jwt_service = service(repository).jwt_service
    token = jwt_service.issue(account_id=actor.account_id, session_id=actor.session_id, authorization_version=actor.authorization_version)
    with pytest.raises(AuthenticationError):
        service(repository).authenticated_principal(token)
    assert repository.audit_actions == ["AUTH_SESSION_VALIDATION"]


@pytest.mark.contract
def test_invalid_token_is_audited_with_a_safe_category() -> None:
    repository = RepositoryStub(None)
    with pytest.raises(AuthenticationError):
        service(repository).authenticated_principal("not-a-jwt")
    assert repository.audit_actions == ["AUTH_TOKEN_VALIDATION"]


@pytest.mark.contract
def test_transactional_revalidation_uses_the_callers_connection_and_current_principal() -> None:
    actor = principal()
    current = AuthenticatedPrincipal(
        actor.account_id,
        actor.session_id,
        actor.username,
        actor.authorization_version,
        frozenset({"ANALISTA"}),
        frozenset({"ingest.upload"}),
    )
    repository = RepositoryStub(current)
    connection = object()

    result = service(repository).revalidate_functional_access(
        connection, actor, "ingest.upload"
    )

    assert result is current
    assert repository.connections == [connection]
    assert repository.transaction_calls == 0


@pytest.mark.contract
def test_transactional_revalidation_rejects_revoked_or_removed_permission() -> None:
    actor = principal()
    connection = object()

    with pytest.raises(AuthenticationError):
        service(RepositoryStub(None)).revalidate_functional_access(
            connection, actor, "ingest.upload"
        )

    current_without_permission = AuthenticatedPrincipal(
        actor.account_id,
        actor.session_id,
        actor.username,
        actor.authorization_version,
        frozenset({"JURIDICO"}),
        frozenset({"document.query"}),
    )
    with pytest.raises(AuthorizationError):
        service(RepositoryStub(current_without_permission)).revalidate_functional_access(
            connection, actor, "ingest.upload"
        )
