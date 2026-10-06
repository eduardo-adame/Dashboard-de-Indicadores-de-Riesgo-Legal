from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.security.api import current_principal
from app.security.models import AuditPersistenceError, AuthenticatedPrincipal, AuthorizationError
from app.validation.api import reinjection_service_for

import pytest
from fastapi import FastAPI
from app.validation.api import router, validation_service_for
from app.validation.quarantine import QuarantineError


def test_openapi_has_exactly_one_canonical_reinjection_endpoint() -> None:
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    matches = [path for path, methods in paths.items() if "patch" in methods and "reinject" in path]
    assert matches == ["/api/validation/quarantine/{item_id}/reinject"]


def test_coordination_has_no_legacy_reinjection_bypass() -> None:
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    assert not [path for path in paths if path.startswith("/api/coordination/") and "reinject" in path]


class _Security:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed

    def require_functional_permission(self, _principal, capability: str) -> None:
        assert capability == "quarantine.reinject"
        if not self.allowed:
            raise AuthorizationError("denied")


class _Integration:
    def reinject(self, **_kwargs):
        return SimpleNamespace(
            quarantine_state="Reinyectado", job_id=uuid4(),
            operation_id=uuid4(), state="COMPLETED",
        )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        uuid4(), uuid4(), "analyst", 1, frozenset({"ANALISTA"}),
        frozenset({"quarantine.reinject"}),
    )


def test_reinjection_endpoint_accepts_principal_with_required_capability() -> None:
    app.state.security_service = _Security(True)
    app.dependency_overrides[current_principal] = _principal
    app.dependency_overrides[reinjection_service_for] = lambda: (_Integration(), object())
    try:
        response = TestClient(app).patch(
            f"/api/validation/quarantine/{uuid4()}/reinject",
            json={"corrected_payload": {"ID_Contrato": "C-1"}},
        )
        assert response.status_code == 200
        assert response.json()["quarantine_state"] == "Reinyectado"
    finally:
        app.dependency_overrides.clear()
        del app.state.security_service


def test_reinjection_endpoint_denies_principal_without_required_capability() -> None:
    app.state.security_service = _Security(False)
    app.dependency_overrides[current_principal] = _principal
    app.dependency_overrides[reinjection_service_for] = lambda: (_Integration(), object())
    try:
        response = TestClient(app).patch(
            f"/api/validation/quarantine/{uuid4()}/reinject",
            json={"corrected_payload": {"ID_Contrato": "C-1"}},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()
        del app.state.security_service


def _http_client(service):
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[current_principal] = _principal
    application.dependency_overrides[validation_service_for] = lambda: service
    return TestClient(application)


class _QuarantineHttp:
    def quarantine_page(self, **kwargs):
        self.query = kwargs
        return [], False

    def discard_for_http(self, **kwargs):
        self.discard = kwargs
        return SimpleNamespace(id=kwargs["item_id"], state="Descartado", discard_justification=kwargs["justification"])


def test_quarantine_query_passes_all_filters_to_service() -> None:
    service = _QuarantineHttp()
    file_id = uuid4()
    response = _http_client(service).get("/api/validation/quarantine", params={
        "ingest_file_id": str(file_id), "source_family": "CONTRATOS_DOCUMENTOS",
        "rejected_from": "2026-01-01T00:00:00Z", "rejected_to": "2026-12-31T00:00:00Z",
        "cause_code": "INVALID_TYPE", "state": "Pendiente", "limit": 1,
    })
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert service.query["ingest_file_id"] == file_id
    assert service.query["source_family"] == "CONTRATOS_DOCUMENTOS"
    assert service.query["cause"] == "INVALID_TYPE"
    assert service.query["limit"] == 1


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 201}, {"state": "unknown"}, {"cause_code": "unknown"},
    {"source_family": "unknown"}, {"cursor": "not-a-cursor"},
    {"rejected_from": "2026-01-01T00:00:00"},
    {"rejected_from": "2026-02-01T00:00:00Z", "rejected_to": "2026-01-01T00:00:00Z"},
])
def test_quarantine_query_invalid_filters_are_rejected(params) -> None:
    service = _QuarantineHttp()
    assert _http_client(service).get("/api/validation/quarantine", params=params).status_code == 422
    assert not hasattr(service, "query")


def test_discard_uses_existing_domain_and_preserves_justification() -> None:
    service = _QuarantineHttp()
    identity = uuid4()
    response = _http_client(service).post(f"/api/validation/quarantine/{identity}/discard", json={"justification": " motivo "})
    assert response.status_code == 200
    assert response.json() == {"id": str(identity), "state": "Descartado", "discard_justification": " motivo "}
    assert service.discard["justification"] == " motivo "


@pytest.mark.parametrize("payload", [{}, {"justification": ""}, {"justification": "  "}, {"justification": "ok", "state": "Descartado"}])
def test_discard_rejects_missing_or_empty_justification(payload) -> None:
    service = _QuarantineHttp()
    assert _http_client(service).post(f"/api/validation/quarantine/{uuid4()}/discard", json=payload).status_code == 422
    assert not hasattr(service, "discard")


@pytest.mark.parametrize("method,path", [("get", "/api/validation/quarantine"), ("post", f"/api/validation/quarantine/{uuid4()}/discard")])
def test_quarantine_http_requires_authentication(method, path) -> None:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[validation_service_for] = lambda: _QuarantineHttp()
    response = getattr(TestClient(application), method)(path, **({"json": {"justification": "motivo"}} if method == "post" else {}))
    assert response.status_code == 401


@pytest.mark.parametrize("error,status", [(AuthorizationError("restricted"), 403), (QuarantineError("restricted-id"), 409), (RuntimeError("restricted-dsn"), 503), (AuditPersistenceError("restricted-audit"), 503)])
def test_discard_error_messages_do_not_leak_internal_details(error, status) -> None:
    class Service(_QuarantineHttp):
        def discard_for_http(self, **kwargs):
            raise error
    response = _http_client(Service()).post(f"/api/validation/quarantine/{uuid4()}/discard", json={"justification": "motivo"})
    assert response.status_code == status
    assert "restricted" not in response.text


def test_quarantine_audit_failure_exposes_no_payload() -> None:
    class Service(_QuarantineHttp):
        def quarantine_page(self, **kwargs):
            raise RuntimeError("private audit error")
    response = _http_client(Service()).get("/api/validation/quarantine")
    assert response.status_code == 503
    assert "private" not in response.text
