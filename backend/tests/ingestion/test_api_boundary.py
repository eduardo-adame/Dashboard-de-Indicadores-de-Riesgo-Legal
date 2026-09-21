from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from fastapi.testclient import TestClient

from app.ingestion.models import IngestionResult, RoutingTarget, SourceFamily
from app.ingestion.models import IdempotencyConflictError
from app.main import app
from app.security.models import AuthenticatedPrincipal, AuthorizationError


class SecurityStub:
    def __init__(self, principal: AuthenticatedPrincipal) -> None:
        self.principal = principal

    def authenticated_principal(self, token: str) -> AuthenticatedPrincipal:
        return self.principal

    def require_functional_permission(self, principal: AuthenticatedPrincipal, capability: str):
        return principal


class DenyingSecurityStub(SecurityStub):
    def require_functional_permission(self, principal: AuthenticatedPrincipal, capability: str):
        raise AuthorizationError("denied")


class IngestionStub:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []

    def ingest_stream(self, stream, **kwargs) -> IngestionResult:
        self.uploads.append({**kwargs, "content": stream.read()})
        identifier = uuid4()
        return IngestionResult(identifier, uuid4(), uuid4(), "COMPLETADO", "CSV", SourceFamily.LITIGATION, RoutingTarget.VALIDATION, None)

    def run_location(self, root, controlled_location, principal):
        return []


class ConflictingIngestionStub(IngestionStub):
    def ingest_stream(self, stream, **kwargs) -> IngestionResult:
        raise IdempotencyConflictError("conflict")


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.upload", "ingest.execute"}))


def test_upload_requires_authentication_and_delegates_to_the_ingestion_case_use() -> None:
    principal = _principal()
    ingestion = IngestionStub()
    app.state.security_service = SecurityStub(principal)
    app.state.ingestion_service = ingestion
    try:
        with TestClient(app) as client:
            denied = client.post(
                "/api/ingestion/uploads",
                files={"file": ("litigation.csv", b"id,amount\nL-1,20", "text/csv")},
                data={"controlled_location": "litigation"},
                headers={"Idempotency-Key": "upload-1"},
            )
            assert denied.status_code == 401
            response = client.post(
                "/api/ingestion/uploads",
                files={"file": ("litigation.csv", b"id,amount\nL-1,20", "text/csv")},
                data={"controlled_location": "litigation"},
                headers={"Authorization": "Bearer valid", "Idempotency-Key": "upload-1"},
            )
        assert response.status_code == 201
        assert response.json()["routing_target"] == "VALIDATION"
        assert ingestion.uploads[0]["capability"] == "ingest.upload"
        assert ingestion.uploads[0]["content"] == b"id,amount\nL-1,20"
    finally:
        del app.state.security_service
        del app.state.ingestion_service


def test_manual_run_is_an_adapter_for_the_same_case_use() -> None:
    principal = _principal()
    ingestion = IngestionStub()
    app.state.security_service = SecurityStub(principal)
    app.state.ingestion_service = ingestion
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/ingestion/runs",
                json={"controlled_location": "compliance"},
                headers={"Authorization": "Bearer valid"},
            )
        assert response.status_code == 200
        assert response.json() == {"processed": 0, "results": []}
    finally:
        del app.state.security_service
        del app.state.ingestion_service


def test_upload_returns_a_controlled_conflict_for_reused_key_with_different_content() -> None:
    app.state.security_service = SecurityStub(_principal())
    app.state.ingestion_service = ConflictingIngestionStub()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/ingestion/uploads",
                files={"file": ("litigation.csv", b"id,amount\nL-1,30", "text/csv")},
                data={"controlled_location": "litigation"},
                headers={"Authorization": "Bearer valid", "Idempotency-Key": "upload-1"},
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "La clave de idempotencia corresponde a otro contenido"}
    finally:
        del app.state.security_service
        del app.state.ingestion_service


def test_direct_api_call_cannot_bypass_functional_authorization() -> None:
    app.state.security_service = DenyingSecurityStub(_principal())
    app.state.ingestion_service = IngestionStub()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/ingestion/uploads",
                files={"file": ("litigation.csv", b"id,amount\nL-1,20", "text/csv")},
                data={"controlled_location": "litigation"},
                headers={"Authorization": "Bearer valid", "Idempotency-Key": "upload-2"},
            )
        assert response.status_code == 403
        assert not app.state.ingestion_service.uploads
    finally:
        del app.state.security_service
        del app.state.ingestion_service
