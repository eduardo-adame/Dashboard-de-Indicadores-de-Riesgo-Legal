"""Pruebas del alcance documental previo a consumidores de retrieval."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.security.models import AuthenticatedPrincipal, AuthorizationError, AuthorizedDocumentScope
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


def _principal(
    *,
    roles: frozenset[str] = frozenset({"ANALISTA"}),
    permissions: frozenset[str] = frozenset({"document.query"}),
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        account_id=uuid4(),
        session_id=uuid4(),
        username="scope-user",
        authorization_version=3,
        roles=roles,
        permissions=permissions,
    )


class _RepositoryStub:
    def __init__(
        self,
        current: AuthenticatedPrincipal,
        families: frozenset[str],
    ) -> None:
        self.current = current
        self.families = families
        self.calls: list[tuple[object, ...]] = []

    def principal_for_session(self, connection, account_id, session_id, authorization_version):
        self.calls.append(("principal", connection, account_id, session_id, authorization_version))
        return self.current

    def active_document_scope_families(self, connection, role_ids):
        self.calls.append(("families", connection, role_ids))
        return self.families

    def transaction(self):  # pragma: no cover - fallaría antes si el servicio intentara usarla.
        raise AssertionError("authorized_document_scope no debe abrir otra transacción")


class _CursorStub:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows
        self.executed: tuple[str, tuple[object, ...]] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        self.executed = statement, parameters

    def fetchall(self) -> list[dict[str, str]]:
        return self.rows


class _ConnectionStub:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.cursor_stub = _CursorStub(rows)
        self.cursor_calls = 0

    def cursor(self) -> _CursorStub:
        self.cursor_calls += 1
        return self.cursor_stub


@pytest.mark.contract
def test_document_scope_builds_composable_database_predicate() -> None:
    account_id = uuid4()
    scope = AuthorizedDocumentScope(account_id, frozenset({"ANALISTA"}), frozenset({"LITIGIOS"}))
    predicate, parameters = scope.database_predicate("document")
    assert "deny_rule.decision = 'DENY'" in predicate
    assert "allow_rule.decision = 'ALLOW'" in predicate
    assert parameters["scope_user_id"] == account_id
    assert parameters["scope_role_ids"] == ["ANALISTA"]
    assert parameters["scope_families"] == ["LITIGIOS"]
    assert scope.includes_family("LITIGIOS") is True
    assert scope.includes_family("CUMPLIMIENTO") is False


@pytest.mark.robustness
def test_document_scope_rejects_an_unsafe_sql_alias() -> None:
    scope = AuthorizedDocumentScope(uuid4(), frozenset(), frozenset())
    with pytest.raises(ValueError):
        scope.database_predicate("document; DROP TABLE app.document")


@pytest.mark.contract
def test_authorized_document_scope_uses_revalidated_roles_and_caller_connection() -> None:
    stale = _principal(roles=frozenset({"JURIDICO"}))
    current = AuthenticatedPrincipal(
        stale.account_id,
        stale.session_id,
        stale.username,
        stale.authorization_version,
        frozenset({"ANALISTA", "TI"}),
        frozenset({"document.query"}),
    )
    repository = _RepositoryStub(current, frozenset({"LITIGIOS", "CUMPLIMIENTO"}))
    connection = object()

    scope = SecurityService(repository).authorized_document_scope(connection, stale)  # type: ignore[arg-type]

    assert scope == AuthorizedDocumentScope(
        current.account_id,
        frozenset({"ANALISTA", "TI"}),
        frozenset({"LITIGIOS", "CUMPLIMIENTO"}),
    )
    assert repository.calls == [
        ("principal", connection, stale.account_id, stale.session_id, stale.authorization_version),
        ("families", connection, current.roles),
    ]


@pytest.mark.contract
def test_authorized_document_scope_allows_an_empty_scope_when_functionally_authorized() -> None:
    principal = _principal(roles=frozenset())
    repository = _RepositoryStub(principal, frozenset())

    scope = SecurityService(repository).authorized_document_scope(object(), principal)  # type: ignore[arg-type]

    assert scope.role_ids == frozenset()
    assert scope.allowed_families == frozenset()


@pytest.mark.contract
def test_authorized_document_scope_requires_document_query_before_loading_scope() -> None:
    principal = _principal(permissions=frozenset())
    repository = _RepositoryStub(principal, frozenset({"LITIGIOS"}))

    with pytest.raises(AuthorizationError, match="Acceso no autorizado"):
        SecurityService(repository).authorized_document_scope(object(), principal)  # type: ignore[arg-type]

    assert [call[0] for call in repository.calls] == ["principal"]


@pytest.mark.contract
def test_active_document_scope_families_reads_only_active_grants_for_current_roles() -> None:
    connection = _ConnectionStub(
        [{"source_family": "CUMPLIMIENTO"}, {"source_family": "LITIGIOS"}]
    )

    families = SecurityRepository.active_document_scope_families(
        connection,  # type: ignore[arg-type]
        frozenset({"TI", "ANALISTA"}),
    )

    assert families == frozenset({"CUMPLIMIENTO", "LITIGIOS"})
    assert connection.cursor_stub.executed is not None
    statement, parameters = connection.cursor_stub.executed
    assert "WHERE active AND role_id = ANY(%s)" in statement
    assert parameters == (["ANALISTA", "TI"],)


@pytest.mark.robustness
def test_active_document_scope_families_avoids_an_untyped_empty_array_query() -> None:
    connection = _ConnectionStub([])

    assert SecurityRepository.active_document_scope_families(
        connection, frozenset()  # type: ignore[arg-type]
    ) == frozenset()
    assert connection.cursor_calls == 0
