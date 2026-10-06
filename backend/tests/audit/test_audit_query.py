"""Contratos puros de alcance, parámetros y cursores de auditoría."""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.audit.models import AuditEventResponse, AuditQuery
from app.audit.repository import AuditRepository, OWN_ACTIONS
from app.config import Settings


def test_own_scope_is_applied_in_sql_before_pagination():
    actor = uuid4()
    statement, parameters = AuditRepository.query_sql(AuditQuery(limit=2), actor)
    assert "e.actor_user_id = %s AND e.action = ANY(%s)" in statement
    assert statement.index("e.actor_user_id") < statement.index("ORDER BY")
    assert parameters == [actor, list(OWN_ACTIONS), 3]
    assert "RAG_QUERY" not in OWN_ACTIONS and "AUTHORIZATION_DENIED" not in OWN_ACTIONS
    assert "QUARANTINE_VIEW" in OWN_ACTIONS


def test_ti_scope_contains_no_actor_restriction():
    statement, parameters = AuditRepository.query_sql(AuditQuery(), None)
    assert "e.actor_user_id =" not in statement
    assert parameters == [101]


def test_sql_injection_remains_bound_parameter():
    query = AuditQuery(action="'; DROP TABLE audit.event; --")
    statement, parameters = AuditRepository.query_sql(query, None)
    assert query.action not in statement and query.action in parameters
    assert "FROM audit.event" in statement


def test_cursor_is_tied_to_filters_and_authorized_actor():
    query, actor = AuditQuery(action="QUARANTINE_VIEW"), uuid4()
    moment, event = datetime.now(UTC), uuid4()
    cursor = query.encode_cursor(moment, event, actor)
    assert query.model_copy(update={"cursor": cursor}).decode_cursor(actor) == (moment, event)
    with pytest.raises(ValueError):
        query.model_copy(update={"cursor": cursor}).decode_cursor(uuid4())
    with pytest.raises(ValueError):
        AuditQuery(cursor=cursor).decode_cursor(actor)


@pytest.mark.parametrize("cursor", ["", "%%%", "bnVsbA==", "e30=", "W10="])
def test_malformed_cursor_is_safe(cursor):
    with pytest.raises(ValueError, match="Cursor no válido"):
        AuditQuery(cursor=cursor).decode_cursor(None)


@pytest.mark.parametrize("values", [{"limit": 0}, {"limit": 201}, {"occurred_from": "2030-01-01"},
                                      {"occurred_from": "2030-02-01T00:00:00Z", "occurred_to": "2030-01-01T00:00:00Z"}])
def test_invalid_filters_rejected(values):
    with pytest.raises(ValidationError):
        AuditQuery(**values)


def test_response_does_not_include_accidental_fields():
    values = dict(id=uuid4(), occurred_at=datetime.now(UTC), actor_type="PROCESS", actor_identifier="synthetic",
                  action="PROACTIVE_ANALYSIS", resource_type="ANALYSIS", result="SUCCESS", operation_id=uuid4(),
                  correlation_id=uuid4(), password="not-returned", legal_text="not-returned")
    response = AuditEventResponse(**values).model_dump()
    assert "password" not in response and "legal_text" not in response


def test_reader_missing_configuration_has_no_fallback():
    settings = Settings(_env_file=None, audit_postgres_user="", audit_postgres_password=None)
    assert settings.audit_psycopg_conninfo is None
