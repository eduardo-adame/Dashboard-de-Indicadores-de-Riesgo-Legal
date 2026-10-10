"""Contrato de transporte administrativo; la autorización real se prueba en PostgreSQL."""
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.documents.api import router, document_http_service_for
from app.documents.api_models import OcrQuery
from app.documents.http_service import (
    DocumentHttpConflict, DocumentHttpUnavailable, UpstreamCommittedKpiRetryable,
    _cursor, _decode_cursor, reprocess_operation_id,
)
from app.security.api import current_principal
from app.security.models import AuditPersistenceError, AuthorizationError


@pytest.fixture
def client():
    application = FastAPI()
    application.include_router(router)
    service = SimpleNamespace(list_ocr=lambda query, principal: {"items": [], "next_cursor": None},
                              reprocess=lambda *args: None)
    application.dependency_overrides[document_http_service_for] = lambda: service
    application.dependency_overrides[current_principal] = lambda: SimpleNamespace(account_id=uuid4())
    return TestClient(application), service, application


def test_ocr_collection_preserves_operational_nulls_without_protected_text(client):
    http, service, _ = client
    identity = uuid4()
    service.list_ocr = lambda *args: {"items": [{"document_id": "synthetic", "document_version_id": identity,
        "document_name": "synthetic.pdf", "file_name": None, "processing_state": "PENDIENTE",
        "ocr_state": "Pendiente", "ocr_applicable": True, "reprocess_eligible": True,
        "processed_at": None, "confidence": None,
        "total_page_count": None, "ocr_processed_page_count": None, "granularity": None,
        "outcome": "Pendiente"}], "next_cursor": None}
    response = http.get("/api/documents/ocr")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["confidence"] is None and item["processed_at"] is None
    assert not {"consolidated_text", "resultado_ocr", "locator", "stored_object_id"}.intersection(item)


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"state": "Exitoso"},
    {"processed_from": "2030-01-01T00:00:00"},
    {"processed_from": "2031-01-01T00:00:00Z", "processed_to": "2030-01-01T00:00:00Z"},
    {"unexpected": "yes"}])
def test_query_rejects_invalid_filters_before_service(client, params):
    http, service, _ = client
    service.list_ocr = lambda *args: pytest.fail("No debe llegar al servicio")
    assert http.get("/api/documents/ocr", params=params).status_code == 422


@pytest.mark.parametrize("error,status", [(AuthorizationError("protected"), 403),
    (AuditPersistenceError("protected"), 503), (DocumentHttpConflict("protected"), 409),
    (DocumentHttpUnavailable("protected"), 503), (ValueError("protected"), 422)])
def test_document_errors_are_safe(client, error, status):
    http, service, _ = client
    def failing(*args):
        raise error
    service.list_ocr = failing
    response = http.get("/api/documents/ocr")
    assert response.status_code == status
    assert "protected" not in response.text


def test_jwt_absent_is_401_for_both_routes(client):
    http, _, application = client
    application.dependency_overrides.pop(current_principal)
    assert http.get("/api/documents/ocr").status_code == 401
    assert http.post("/api/documents/unknown/ocr/reprocess", json={
        "source_document_version_id": str(uuid4()), "request_id": str(uuid4())}).status_code == 401


def test_reprocess_preserves_request_identity_and_typed_result(client):
    http, service, _ = client
    source, request_id, operation, output = uuid4(), uuid4(), uuid4(), uuid4()
    def run(document_id, payload, principal):
        assert document_id == "document" and payload.source_document_version_id == source
        assert payload.request_id == request_id
        return {"operation_id": operation, "document_id": document_id, "document_version_id": output,
                "ocr_state": "Rechazado por baja confianza", "processing_state": "RECHAZADA",
                "kpi_job_id": uuid4(), "kpi_job_state": "COMPLETED"}
    service.reprocess = run
    response = http.post("/api/documents/document/ocr/reprocess", json={
        "source_document_version_id": str(source), "request_id": str(request_id)})
    assert response.status_code == 200 and response.json()["document_version_id"] == str(output)


def test_kpi_retry_error_does_not_claim_upstream_rollback(client):
    http, service, _ = client
    operation = uuid4()
    def run(*args):
        raise UpstreamCommittedKpiRetryable(operation)
    service.reprocess = run
    response = http.post("/api/documents/document/ocr/reprocess", json={
        "source_document_version_id": str(uuid4()), "request_id": str(uuid4())})
    assert response.status_code == 503
    assert response.json()["detail"] == {"cause": "UPSTREAM_COMMITTED_KPI_RETRYABLE", "operation_id": str(operation)}


def test_reprocess_input_forbids_context_spoofing(client):
    http, _, _ = client
    assert http.post("/api/documents/document/ocr/reprocess", json={
        "source_document_version_id": str(uuid4()), "request_id": str(uuid4()),
        "actor": "TI"}).status_code == 422


def test_operation_and_cursor_identity_are_stable_and_filter_bound():
    actor, source, request = uuid4(), uuid4(), uuid4()
    identity = reprocess_operation_id(actor, "doc", source, request)
    assert identity == reprocess_operation_id(actor, "doc", source, request)
    assert identity != reprocess_operation_id(uuid4(), "doc", source, request)
    stamp, version = datetime.now(UTC), uuid4()
    query = OcrQuery(document_id="doc")
    cursor = _cursor({"created_at": stamp, "document_version_id": version}, query)
    assert _decode_cursor(query.model_copy(update={"cursor": cursor})) == (stamp, version)
    with pytest.raises(ValueError):
        _decode_cursor(OcrQuery(document_id="other", cursor=cursor))
