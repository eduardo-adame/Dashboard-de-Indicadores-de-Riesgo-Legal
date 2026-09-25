from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app
from app.security.api import current_principal
from app.security.models import AuthenticatedPrincipal, AuthorizationError
from app.validation.api import reinjection_service_for


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
