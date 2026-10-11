"""Frontera HTTP: correlación, campos no confiables y error seguro, sin evidencia SQL.

SRS_REQUIRED: permisos y confirmación de auditoría; ROBUSTNESS: entrada inválida.
Los dobles aíslan la frontera de transporte; no prueban persistencia ni JWT reales.
"""
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.coordination.api import router as coordination_router
from app.coordination.service import DispatchOutcome
from app.ingestion.api import router as ingestion_router
from app.ingestion.models import IngestionResult, RoutingTarget, SourceFamily
from app.security.models import AuditPersistenceError, AuthenticatedPrincipal, AuthorizationError


pytestmark = pytest.mark.contract
_HEADERS = {"Authorization": "Bearer synthetic", "Idempotency-Key": "synthetic-key"}
_UNTRUSTED = {
    "actor_type": "PROCESS", "process_identifier": "airflow.claimed_by_client",
    "automatic_discovery": True, "manual_ocr_reprocess": True,
    "actor": "client-actor",
}


class _Security:
    def __init__(self, denied=False):
        self.denied = denied
        self.principal = AuthenticatedPrincipal(uuid4(), uuid4(), "synthetic", 1, frozenset({"ANALISTA"}), frozenset())

    def authenticated_principal(self, _token):
        return self.principal

    def require_functional_permission(self, _principal, _capability):
        if self.denied:
            raise AuthorizationError("Categoría sintética privada")
        return self.principal


class _Operations:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def _record(self, method, values):
        self.calls.append((method, values))
        if self.fail:
            raise AuditPersistenceError("Información privada sintética SQL y almacenamiento")

    def run_location(self, root, location, principal, **kwargs):
        self._record("run", {"root": root, "location": location, "principal": principal, **kwargs})
        return []

    def ingest_stream(self, stream, **kwargs):
        self._record("upload", kwargs)
        return IngestionResult(uuid4(), uuid4(), uuid4(), "COMPLETADO", "CSV", SourceFamily.LITIGATION, RoutingTarget.VALIDATION, None)

    def dispatch(self, **kwargs):
        self._record("dispatch", kwargs)
        return DispatchOutcome("DOCUMENT", uuid4(), uuid4(), "COMPLETED")


def _client(*, fail=False, denied=False):
    operations, security = _Operations(fail), _Security(denied)
    application = FastAPI()
    application.include_router(ingestion_router)
    application.include_router(coordination_router)
    application.state.ingestion_service = operations
    application.state.coordination_service = operations
    application.state.security_service = security
    application.state.security_settings = SimpleNamespace(ingestion_controlled_root="synthetic-controlled")
    return TestClient(application), operations, security


def _request(client, route, *, headers=None, extras=None, correlation=None):
    if route == "uploads":
        return client.post(
            "/api/ingestion/uploads", files={"file": ("sample.csv", b"id,amount\nL-1,20\n", "text/csv")},
            data={"controlled_location": "litigation", **(extras or {})}, headers=headers,
        )
    payload = ({"controlled_location": "litigation"} if route == "runs" else {"file_id": str(uuid4())})
    if correlation is not None:
        payload["correlation_id"] = correlation
    payload.update(extras or {})
    prefix = "ingestion" if route == "runs" else "coordination"
    return client.post(f"/api/{prefix}/{route}", json=payload, headers=headers)


@pytest.mark.parametrize("route", ["runs", "dispatch"])
def test_invalid_correlation_uuid_is_422_without_execution(route):
    client, operations, _ = _client()
    with client:
        response = _request(client, route, headers=_HEADERS, correlation="not-a-uuid")
    assert response.status_code == 422 and operations.calls == []


@pytest.mark.parametrize("route,status", [("runs", 200), ("dispatch", 200), ("uploads", 201)])
def test_client_cannot_select_process_actor_or_manual_ocr_context(route, status):
    client, operations, security = _client()
    correlation = uuid4()
    with client:
        response = _request(client, route, headers=_HEADERS, extras=_UNTRUSTED, correlation=str(correlation))
    assert response.status_code == status
    _, values = operations.calls[0]
    assert not set(_UNTRUSTED).intersection(values.keys() - {"actor"})
    if route == "dispatch":
        assert values["actor"] is security.principal
    else:
        assert values["principal"] is security.principal and "actor" not in values
    if route != "uploads":
        assert values["correlation_id"] == correlation
    else:
        assert values["capability"] == "ingest.upload"
        assert values["source_locator"] == f"manual/{security.principal.account_id}/synthetic-key"


@pytest.mark.parametrize("route", ["runs", "uploads", "dispatch"])
@pytest.mark.parametrize("authenticated,denied,status", [(False, False, 401), (True, True, 403)])
def test_authentication_or_permission_failure_never_executes_case_use(route, authenticated, denied, status):
    client, operations, _ = _client(denied=denied)
    with client:
        headers = _HEADERS if authenticated else {"Idempotency-Key": "synthetic-key"}
        response = _request(client, route, headers=headers)
    assert response.status_code == status and operations.calls == []
    assert "Categoría sintética" not in response.text


@pytest.mark.parametrize("route,detail", [
    ("runs", "La ingesta no pudo confirmarse"),
    ("uploads", "La ingesta no pudo confirmarse"),
    ("dispatch", "El procesamiento no pudo confirmarse"),
])
def test_audit_persistence_failure_is_safe_unconfirmed_503(route, detail):
    client, operations, _ = _client(fail=True)
    with client:
        response = _request(client, route, headers=_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": detail}
    assert len(operations.calls) == 1
    assert "Información privada" not in response.text and "SQL" not in response.text
    assert "operation_id" not in response.json() and "state" not in response.json()
