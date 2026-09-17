"""Pruebas del alcance documental previo a consumidores de retrieval."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.security.models import AuthorizedDocumentScope


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
