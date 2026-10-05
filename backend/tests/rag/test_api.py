"""Pruebas HTTP focales de la ruta RAG."""
from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.rag.api import application_service_for
from app.rag.service import RagApplicationError, RagApplicationResult
from app.security.api import current_principal
from app.security.models import AuthenticatedPrincipal, ROLE_PERMISSIONS


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "synthetic", 1, frozenset({"JURIDICO"}), ROLE_PERMISSIONS["JURIDICO"])


class _Service:
    def __init__(self, status=200, state="SIN_EVIDENCIA") -> None:
        self.status = status
        self.state = state
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return RagApplicationResult(self.status, {
            "operation_id": uuid4(), "correlation_id": uuid4(), "state": self.state,
            "operation_status": "COMPLETED", "generation_status": "NOT_REQUESTED",
            "generated_response": None, "safe_result_message": "Mensaje seguro",
            "context_reference_id": None, "fragments": [], "citations": [],
        })


def test_openapi_exposes_one_rag_query_route() -> None:
    routes = [path for path in app.openapi()["paths"] if "rag" in path]
    assert routes == ["/api/rag/query"]
    assert "post" in app.openapi()["paths"][routes[0]]


def test_missing_token_returns_401_without_service_call() -> None:
    service = _Service()
    app.dependency_overrides[application_service_for] = lambda: service
    try:
        response = TestClient(app).post("/api/rag/query", json={"query": "Consulta sintética"})
        assert response.status_code == 401
        assert service.calls == []
    finally:
        app.dependency_overrides.clear()


def test_valid_request_passes_principal_query_and_idempotency_key() -> None:
    service = _Service()
    principal = _principal()
    key = uuid4()
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[application_service_for] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/rag/query", json={"query": "Consulta sintética"},
            headers={"Idempotency-Key": str(key)},
        )
        assert response.status_code == 200
        assert response.json()["state"] == "SIN_EVIDENCIA"
        assert service.calls[0]["principal"] is principal
        assert service.calls[0]["idempotency_key"] == key
    finally:
        app.dependency_overrides.clear()


def test_explicit_denial_returns_403_without_fragments() -> None:
    service = _Service(403, "SIN_AUTORIZACION")
    app.dependency_overrides[current_principal] = _principal
    app.dependency_overrides[application_service_for] = lambda: service
    try:
        response = TestClient(app).post("/api/rag/query", json={"query": "Consulta sintética"})
        assert response.status_code == 403
        assert response.json()["fragments"] == []
        assert response.json()["citations"] == []
        assert response.json()["generated_response"] is None
    finally:
        app.dependency_overrides.clear()


def test_invalid_query_and_safe_service_error() -> None:
    class FailingService:
        def execute(self, **_kwargs):
            raise RagApplicationError("La operación no pudo completarse", 503)

    app.dependency_overrides[current_principal] = _principal
    app.dependency_overrides[application_service_for] = lambda: FailingService()
    try:
        client = TestClient(app)
        assert client.post("/api/rag/query", json={"query": ""}).status_code == 422
        response = client.post("/api/rag/query", json={"query": "Consulta"})
        assert response.status_code == 503
        assert response.json() == {"detail": "La operación no pudo completarse"}
    finally:
        app.dependency_overrides.clear()
