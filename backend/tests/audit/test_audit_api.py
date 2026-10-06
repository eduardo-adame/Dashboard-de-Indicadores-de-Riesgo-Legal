"""Pruebas HTTP de transporte sin sustituir evidencia de permisos PostgreSQL."""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.audit.api import router
from app.audit.models import AuditPageResponse, AuditUnavailableError
from app.security.models import AuthenticatedPrincipal, AuthenticationError, AuthorizationError


class SecurityStub:
    def authenticated_principal(self, token):
        if token != "synthetic-token":
            raise AuthenticationError("No válido")
        return AuthenticatedPrincipal(uuid4(), uuid4(), "synthetic", 1, frozenset(), frozenset())


class QueryStub:
    def __init__(self, failure=None):
        self.failure, self.calls = failure, 0

    def list_events(self, principal, query):
        self.calls += 1
        query.decode_cursor(None)
        if self.failure:
            raise self.failure
        return AuditPageResponse(items=[])


def client(failure=None):
    app = FastAPI()
    app.include_router(router)
    app.state.security_service = SecurityStub()
    app.state.audit_service = QueryStub(failure)
    return TestClient(app), app.state.audit_service


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid"}])
def test_authentication_is_required(headers):
    connection, service = client()
    response = connection.get("/api/audit/events", headers=headers)
    assert response.status_code == 401 and service.calls == 0


def test_read_response_is_minimized():
    connection, _ = client()
    response = connection.get("/api/audit/events", headers={"Authorization": "Bearer synthetic-token"})
    assert response.status_code == 200 and response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("error,status", [(AuthorizationError("restricted-id"), 403), (AuditUnavailableError("internal-password"), 503)])
def test_safe_error_contract(error, status):
    connection, _ = client(error)
    response = connection.get("/api/audit/events", headers={"Authorization": "Bearer synthetic-token"})
    assert response.status_code == status
    assert "restricted-id" not in response.text and "internal-password" not in response.text


@pytest.mark.parametrize("params", [{"limit": 201}, {"cursor": "%%%"}, {"occurred_from": "2030-01-01"}])
def test_invalid_query_returns_422(params):
    connection, _ = client()
    assert connection.get("/api/audit/events", params=params, headers={"Authorization": "Bearer synthetic-token"}).status_code == 422


def test_audit_publishes_only_read_route():
    connection, _ = client()
    operations = connection.app.openapi()["paths"]["/api/audit/events"]
    assert set(operations) == {"get"}
    assert connection.delete("/api/audit/events").status_code == 405
