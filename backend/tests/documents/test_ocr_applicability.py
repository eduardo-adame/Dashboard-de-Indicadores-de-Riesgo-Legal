"""Aplicabilidad y elegibilidad OCR en la frontera HTTP, sin conexiones reales."""
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.documents.api import document_http_service_for, router
from app.documents.api_models import OcrItem, OcrQuery
from app.documents.http_service import DocumentHttpService
from app.documents.repository import DocumentsRepository
from app.security.api import current_principal


def _row(**changes):
    stamp = datetime.now(UTC)
    row = {
        "document_id": "synthetic", "document_version_id": uuid4(),
        "document_name": "synthetic.pdf", "file_name": "synthetic.pdf",
        "processing_state": "PENDIENTE", "ocr_state": None,
        "processed_at": None, "confidence": None, "total_page_count": None,
        "ocr_processed_page_count": None, "granularity": None, "created_at": stamp,
        "invalidated_at": None, "file_state": "COMPLETADO", "technical_result": "ACCEPTED",
        "quarantined": False, "requires_ocr": True, "source_verified": True,
        "ingest_file_id": uuid4(), "stored_object_id": uuid4(),
    }
    row.update(changes)
    return row


class _Repository:
    _conninfo = "synthetic-not-connected"

    def __init__(self, rows):
        self.rows, self.result = rows, None

    @contextmanager
    def transaction(self):
        yield object()

    def list_ocr_operations(self, connection, **kwargs):
        return self.rows

    def ocr_reprocess_source(self, connection, document_id, version_id):
        source = self.rows[0]
        return {**source, "estado_ocr": source["ocr_state"]}

    def ocr_operation_result(self, connection, operation_id):
        return self.result


def _client(rows, monkeypatch):
    repository = _Repository(rows)
    principal = SimpleNamespace(account_id=uuid4())
    security = SimpleNamespace(revalidate_functional_access=lambda connection, actor, capability: actor)
    calls = []

    def runner(context, file_id):
        calls.append(file_id)
        repository.result = {
            "document_id": "synthetic", "document_version_id": uuid4(), "ocr_state": "Exitoso",
            "processing_state": "LISTA", "kpi_job_id": None, "kpi_job_state": None,
        }

    @contextmanager
    def coordinator(*args, **kwargs):
        yield SimpleNamespace(execute=lambda *args: None)

    monkeypatch.setattr("app.documents.http_service.psycopg.connect", coordinator)
    service = DocumentHttpService(repository, security, runner=runner, kpi_integration=object())
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[current_principal] = lambda: principal
    application.dependency_overrides[document_http_service_for] = lambda: service
    return TestClient(application), calls


@pytest.mark.parametrize("name", ["synthetic.docx", "text.pdf"])
def test_native_document_has_no_ocr_state_or_reprocess_action(monkeypatch, name):
    http, calls = _client([_row(file_name=name, requires_ocr=False, processing_state="LISTA")], monkeypatch)
    response = http.get("/api/documents/ocr")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ocr_state"] is item["outcome"] is None
    assert item["ocr_applicable"] is item["reprocess_eligible"] is False
    assert item["confidence"] is None
    rejected = http.post("/api/documents/synthetic/ocr/reprocess", json={
        "source_document_version_id": item["document_version_id"], "request_id": str(uuid4()),
    })
    assert rejected.status_code == 409
    assert calls == []


@pytest.mark.parametrize("state,processing,eligible", [
    (None, "PENDIENTE", True), ("Pendiente", "PENDIENTE", True),
    ("Pendiente", "PROCESANDO", True), ("Exitoso", "LISTA", False),
    ("Rechazado por baja confianza", "RECHAZADA", True),
])
def test_list_and_reprocess_share_ocr_eligibility(monkeypatch, state, processing, eligible):
    http, calls = _client([_row(ocr_state=state, processing_state=processing)], monkeypatch)
    response = http.get("/api/documents/ocr")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ocr_state"] == item["outcome"] == state
    assert item["ocr_applicable"] is True
    assert item["reprocess_eligible"] is eligible
    result = http.post("/api/documents/synthetic/ocr/reprocess", json={
        "source_document_version_id": item["document_version_id"], "request_id": str(uuid4()),
    })
    assert result.status_code == (200 if eligible else 409)
    assert len(calls) == int(eligible)


@pytest.mark.parametrize("changes", [
    {"invalidated_at": datetime.now(UTC)}, {"file_state": "RECHAZADO"},
    {"technical_result": "REJECTED"}, {"quarantined": True},
    {"processing_state": "FALLIDA"}, {"source_verified": False},
])
def test_blocked_source_is_not_advertised_or_processed(monkeypatch, changes):
    http, calls = _client([_row(**changes)], monkeypatch)
    item = http.get("/api/documents/ocr").json()["items"][0]
    assert item["reprocess_eligible"] is False
    result = http.post("/api/documents/synthetic/ocr/reprocess", json={
        "source_document_version_id": item["document_version_id"], "request_id": str(uuid4()),
    })
    assert result.status_code == 409
    assert calls == []


def test_listing_does_not_expose_eligibility_provenance_or_storage(monkeypatch):
    http, _ = _client([_row(ocr_state="Pendiente")], monkeypatch)
    item = http.get("/api/documents/ocr").json()["items"][0]
    assert not {"source_verified", "invalidated_at", "quarantined", "requires_ocr",
                "file_state", "technical_result", "ingest_file_id", "stored_object_id"}.intersection(item)


def test_ocr_state_filter_compares_only_persisted_status():
    class Connection:
        def execute(self, statement, values):
            self.statement, self.values = statement, values
            return SimpleNamespace(fetchall=lambda: [])

    connection = Connection()
    DocumentsRepository.list_ocr_operations(connection, query=OcrQuery(state="Pendiente"))
    assert "COALESCE(o.estado_ocr" not in connection.statement
    assert "o.estado_ocr = %s" in connection.statement
    assert connection.values == ["Pendiente", 101]


@pytest.mark.parametrize("field", ["ocr_applicable", "reprocess_eligible"])
def test_ocr_schema_requires_backend_action_flags(field):
    row = _row()
    payload = {key: row[key] for key in OcrItem.model_fields
               if key not in {"outcome", "ocr_applicable", "reprocess_eligible"}}
    payload.update(outcome=None, ocr_applicable=True, reprocess_eligible=True)
    del payload[field]
    with pytest.raises(ValidationError):
        OcrItem(**payload)


@pytest.mark.parametrize("field", ["ocr_state", "outcome"])
def test_ocr_schema_does_not_create_presentation_states(field):
    row = _row()
    payload = {key: row[key] for key in OcrItem.model_fields
               if key not in {"outcome", "ocr_applicable", "reprocess_eligible"}}
    payload.update(outcome=None, ocr_applicable=True, reprocess_eligible=True)
    payload[field] = "No aplica"
    with pytest.raises(ValidationError):
        OcrItem(**payload)
