"""Adaptadores documentales reales con persistencia PostgreSQL y motores sintéticos.

Los dobles aíslan OCR, tokenización y embeddings; no sustituyen repositorios,
certificación, autorización, transacciones, activación ni recálculo KPI.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
import pytest

from app.coordination.kpi_integration import KpiIntegrationService
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.coordination.runners import make_document_runner
from app.corpus.tokenizer import FastTokenizer
from app.documents.api import document_http_service_for, router
from app.documents.api_models import ReprocessRequest
from app.documents.http_service import DocumentHttpService
from app.documents.repository import DocumentsRepository
from app.security.api import current_principal
from app.security.models import AuthenticatedPrincipal
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService


class _Encoder:
    def __init__(self):
        self.calls = 0

    def encode_documents(self, texts, normalize_embeddings=False):
        self.calls += 1
        return [[0.01] * 1024 for _ in texts]


@pytest.fixture
def database(monkeypatch):
    owner = os.getenv("DATA_TEST_DATABASE_URL", "")
    runtime = os.getenv("RUNTIME_TEST_DATABASE_URL", "")
    if not owner or not runtime:
        pytest.fail("Se requieren conexiones protegidas a PostgreSQL de pruebas existente")
    assert "test" in conninfo_to_dict(owner).get("dbname", "").lower()
    assert conninfo_to_dict(owner)["dbname"] == conninfo_to_dict(runtime)["dbname"]
    with psycopg.connect(runtime) as connection:
        assert connection.execute("SELECT session_user,current_user").fetchone() == ("riesgo_legal_app", "riesgo_legal_app")
    account, session = uuid4(), uuid4()
    with psycopg.connect(owner, row_factory=dict_row) as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone()["version_num"] == "0016_rag_operation_lifecycle"
        connection.execute("INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES (%s,%s,'Prueba','hash')", (account, f"document-http-{account.hex}"))
        connection.execute("INSERT INTO app.user_role(user_id,role_id) VALUES (%s,'ANALISTA')", (account,))
        connection.execute("""INSERT INTO app.access_session(id,user_id,refresh_token_sha256,authorization_version,state,expires_at)
            VALUES (%s,%s,%s,1,'ACTIVE',%s)""", (session, account, bytes(32), datetime.now(UTC) + timedelta(hours=1)))
    principal = AuthenticatedPrincipal(account, session, f"document-http-{account.hex}", 1, frozenset({"ANALISTA"}), frozenset())
    security = SecurityService(SecurityRepository(runtime), JwtService(issuer="test", audience="test", keyring={"test": base64.urlsafe_b64encode(bytes(range(32))).decode()}, active_kid="test"))
    encoder = _Encoder()
    state = {"confidence": 0.95, "ocr_calls": 0, "runner_calls": 0}
    def ocr(_file_id, candidate):
        state["ocr_calls"] += 1
        if state.get("ocr_hook") is not None:
            state["ocr_hook"]()
        return {page.page_number: ("Cláusula sintética administrativa", state["confidence"]) for page in candidate.pages if page.requires_ocr}
    monkeypatch.setattr("app.ocr.pipeline.make_ocr_pipeline", lambda **kwargs: ocr)
    monkeypatch.setattr("app.rag.indexing.get_embedding_service", lambda: encoder)
    integration = KpiIntegrationService(conninfo=runtime, security=security, repository=KpiIntegrationRepository(runtime))
    real_runner = make_document_runner(conninfo=runtime, security=security, tokenizer_factory=FastTokenizer,
                                       storage_root="/tmp/synthetic-document-http", kpi_integration=integration)
    def runner(context, file_id):
        state["runner_calls"] += 1
        return real_runner(context, file_id)
    service = DocumentHttpService(DocumentsRepository(runtime, security.repository), security, runner=runner, kpi_integration=integration)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[current_principal] = lambda: principal
    application.dependency_overrides[document_http_service_for] = lambda: service
    return {"owner": owner, "runtime": runtime, "principal": principal, "service": service,
            "client": TestClient(application), "integration": integration, "encoder": encoder, "state": state}


def _seed(database, *, ocr_state=None, processing_state="PENDIENTE", requires_ocr=True, exchange_format="PDF"):
    file_id, stored_id, source_version = uuid4(), uuid4(), uuid4()
    file_name = f"synthetic.{exchange_format.lower()}"
    mime_type = "application/pdf" if exchange_format == "PDF" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("""INSERT INTO app.stored_object(id,storage_kind,locator,sha256,mime_type,byte_size,original_name)
            VALUES (%s,'FILESYSTEM',%s,%s,%s,10,%s)""", (stored_id, f"test/{stored_id}/{file_name}", bytes(32), mime_type, file_name))
        connection.execute("""INSERT INTO app.ingest_file(id,stored_object_id,source_family,exchange_format,state,operation_id,correlation_id,
            declared_extension,detected_format,format_classification,technical_result,declared_name,source_locator,source_revision,actor_identifier,content_sha256)
            VALUES (%s,%s,'CONTRATOS_DOCUMENTOS',%s,'COMPLETADO',%s,%s,%s,%s,'SUPPORTED','ACCEPTED',%s,%s,1,'test',%s)""",
            (file_id, stored_id, exchange_format, uuid4(), uuid4(), f".{exchange_format.lower()}", exchange_format,
             f"synthetic.{exchange_format.lower()}", f"test/{file_id}", bytes(32)))
        connection.execute("INSERT INTO app.document(id_documento,name,document_type,source_family) VALUES (%s,%s,%s,'CONTRATOS_DOCUMENTOS')", (str(file_id), file_name, exchange_format))
        connection.execute("""INSERT INTO app.document_version(id,id_documento,version_number,stored_object_id,processing_state,content_sha256,operation_id,correlation_id,completed_at)
            VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s)""", (source_version, str(file_id), stored_id, processing_state, bytes(32), uuid4(), uuid4(),
            datetime.now(UTC) if processing_state in {"LISTA", "RECHAZADA", "FALLIDA"} else None))
        connection.execute("INSERT INTO app.document_candidate(ingest_file_id,processing_state,native_text) VALUES (%s,%s,'')",
                           (file_id, "PENDING_OCR" if requires_ocr else "NATIVE_TEXT"))
        connection.execute("INSERT INTO app.document_candidate_page(id,ingest_file_id,page_number,native_text,requires_ocr) VALUES (%s,%s,1,'',%s)",
                           (uuid4(), file_id, requires_ocr))
        if ocr_state is not None:
            DocumentsRepository.insert_ocr_run(connection, document_version_id=source_version, attempt_number=1,
                estado_ocr=ocr_state, confianza_agregada=(None if ocr_state == "Pendiente" else 0.95 if ocr_state == "Exitoso" else 0.5),
                total_page_count=1, processed_page_count=1,
                granularity="WORD", operation_id=uuid4(), correlation_id=uuid4())
    return str(file_id), source_version


def _request(database, document_id, source, request_id=None):
    payload = {"source_document_version_id": str(source), "request_id": str(request_id or uuid4())}
    return database["client"].post(f"/api/documents/{document_id}/ocr/reprocess", json=payload), payload


def _counts(database, document_id):
    with psycopg.connect(database["owner"]) as connection:
        return connection.execute("""SELECT
            (SELECT count(*) FROM app.document_version WHERE id_documento=%s),
            (SELECT count(*) FROM app.document_index_certificate c JOIN app.document_version v ON v.id=c.document_version_id WHERE v.id_documento=%s),
            (SELECT count(*) FROM audit.event WHERE resource_identifier=%s AND action='DOCUMENT_VERSION_ACTIVATED'),
            (SELECT active_version_id FROM app.document WHERE id_documento=%s)""", (document_id,) * 4).fetchone()


def test_ocr_list_preserves_nulls_and_filters_operational_metadata(database):
    document, source = _seed(database)
    response = database["client"].get("/api/documents/ocr", params={"document_id": document})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["document_version_id"] == str(source)
    assert item["file_name"] == "synthetic.pdf"
    assert item["ocr_state"] is item["outcome"] is None
    assert item["ocr_applicable"] is item["reprocess_eligible"] is True
    assert all(item[key] is None for key in ("processed_at", "confidence", "total_page_count", "ocr_processed_page_count", "granularity"))
    assert not {"consolidated_text", "stored_object_id", "locator", "native_text"}.intersection(item)
    for extra in ({"state": "Pendiente"}, {"state": "Rechazado por baja confianza"},
                  {"processed_from": "2030-01-01T00:00:00Z"}):
        assert database["client"].get("/api/documents/ocr", params={"document_id": document, **extra}).json()["items"] == []


def test_ocr_list_returns_low_confidence_result_and_date_filter(database):
    document, source = _seed(database, ocr_state="Rechazado por baja confianza", processing_state="RECHAZADA")
    response = database["client"].get("/api/documents/ocr", params={"document_id": document, "state": "Rechazado por baja confianza"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["confidence"] == 0.5 and item["processed_at"] is not None
    assert item["ocr_applicable"] is item["reprocess_eligible"] is True
    assert item["total_page_count"] == item["ocr_processed_page_count"] == 1
    assert database["client"].get("/api/documents/ocr", params={"document_id": document, "processed_to": "2000-01-01T00:00:00Z"}).json()["items"] == []


@pytest.mark.parametrize("exchange_format", ["PDF", "DOCX"])
def test_native_ocr_listing_preserves_null_and_reprocess_returns_conflict(database, exchange_format):
    document, source = _seed(database, requires_ocr=False, exchange_format=exchange_format, processing_state="LISTA")
    response = database["client"].get("/api/documents/ocr", params={"document_id": document})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["ocr_state"] is item["outcome"] is None
    assert item["ocr_applicable"] is item["reprocess_eligible"] is False
    assert item["file_name"] == f"synthetic.{exchange_format.lower()}"
    assert database["client"].get("/api/documents/ocr", params={"document_id": document, "state": "Pendiente"}).json()["items"] == []
    result, _ = _request(database, document, source)
    assert result.status_code == 409
    assert database["state"]["runner_calls"] == database["state"]["ocr_calls"] == 0
    assert _counts(database, document) == (1, 0, 0, None)


@pytest.mark.parametrize("ocr_state,processing,eligible", [
    ("Pendiente", "PENDIENTE", True), ("Exitoso", "LISTA", False),
    ("Rechazado por baja confianza", "RECHAZADA", True),
])
def test_persisted_ocr_status_is_preserved_with_authoritative_eligibility(database, ocr_state, processing, eligible):
    document, source = _seed(database, ocr_state=ocr_state, processing_state=processing)
    item = database["client"].get("/api/documents/ocr", params={"document_id": document}).json()["items"][0]
    assert item["ocr_state"] == item["outcome"] == ocr_state
    assert item["ocr_applicable"] is True
    assert item["reprocess_eligible"] is eligible
    if not eligible:
        result, _ = _request(database, document, source)
        assert result.status_code == 409
        assert database["state"]["runner_calls"] == database["state"]["ocr_calls"] == 0


def test_latest_ocr_result_controls_eligibility_without_rewriting_history(database):
    document, source = _seed(database)
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("""INSERT INTO app.ocr_run
            (id,document_version_id,attempt_number,estado_ocr,is_final,operation_id,correlation_id,processed_at)
            VALUES (%s,%s,1,'Pendiente',FALSE,%s,%s,%s)""",
            (uuid4(), source, uuid4(), uuid4(), datetime.now(UTC) - timedelta(seconds=1)))
        DocumentsRepository.insert_ocr_run(connection, document_version_id=source, attempt_number=2,
            estado_ocr="Exitoso", confianza_agregada=0.95, total_page_count=1, processed_page_count=1,
            granularity="WORD", operation_id=uuid4(), correlation_id=uuid4())
    item = database["client"].get("/api/documents/ocr", params={"document_id": document}).json()["items"][0]
    assert item["ocr_state"] == "Exitoso" and item["reprocess_eligible"] is False
    assert database["client"].get("/api/documents/ocr", params={"document_id": document, "state": "Pendiente"}).json()["items"] == []
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM app.ocr_run WHERE document_version_id=%s", (source,)).fetchone()[0] == 2


def test_ocr_pagination_retains_version_identity_without_duplicates(database):
    document, first = _seed(database)
    second = uuid4()
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("""INSERT INTO app.document_version
            (id,id_documento,version_number,stored_object_id,processing_state,content_sha256,operation_id,correlation_id)
            SELECT %s,id_documento,2,stored_object_id,'PENDIENTE',content_sha256,%s,%s
            FROM app.document_version WHERE id=%s""", (second, uuid4(), uuid4(), first))
    query = {"document_id": document, "limit": 1}
    initial = database["client"].get("/api/documents/ocr", params=query)
    assert initial.status_code == 200 and initial.json()["next_cursor"] is not None
    following = database["client"].get("/api/documents/ocr", params={
        **query, "cursor": initial.json()["next_cursor"]})
    assert following.status_code == 200 and following.json()["next_cursor"] is None
    identifiers = [initial.json()["items"][0]["document_version_id"],
                   following.json()["items"][0]["document_version_id"]]
    assert set(identifiers) == {str(first), str(second)} and len(set(identifiers)) == 2


@pytest.mark.parametrize("revocation", ["JURIDICO", "ROLE_REMOVED", "SESSION_INACTIVE"])
def test_current_document_authority_denies_before_source_or_pipeline(database, revocation):
    document, source = _seed(database)
    principal = database["principal"]
    with psycopg.connect(database["owner"]) as connection:
        if revocation == "JURIDICO":
            connection.execute("UPDATE app.user_role SET role_id='JURIDICO' WHERE user_id=%s", (principal.account_id,))
        elif revocation == "ROLE_REMOVED":
            connection.execute("DELETE FROM app.user_role WHERE user_id=%s", (principal.account_id,))
        else:
            connection.execute("UPDATE app.access_session SET state='INVALIDATED',invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", (principal.session_id,))
    assert database["client"].get("/api/documents/ocr", params={"document_id": document}).status_code == 403
    response, _ = _request(database, document, source)
    assert response.status_code == 403
    assert document not in response.text and str(source) not in response.text
    assert database["state"]["runner_calls"] == database["state"]["ocr_calls"] == database["encoder"].calls == 0
    assert _counts(database, document) == (1, 0, 0, None)
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE actor_user_id=%s AND action='AUTHORIZATION_DENIED'", (principal.account_id,)).fetchone()[0] == 2


def test_real_pipeline_certifies_activates_and_persists_kpi_with_durable_retry(database):
    document, source = _seed(database)
    response, payload = _request(database, document, source)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["processing_state"] == "LISTA" and result["ocr_state"] == "Exitoso"
    assert result["kpi_job_state"] == "COMPLETED"
    repeated = database["client"].post(f"/api/documents/{document}/ocr/reprocess", json=payload)
    assert repeated.status_code == 200 and repeated.json() == result
    assert database["state"]["runner_calls"] == database["state"]["ocr_calls"] == database["encoder"].calls == 1
    versions, certificates, audits, active = _counts(database, document)
    assert (versions, certificates, audits, str(active)) == (2, 1, 1, result["document_version_id"])
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM app.ocr_run WHERE operation_id=%s", (result["operation_id"],)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM app.job_run WHERE operation_id=%s", (result["operation_id"],)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM app.kpi_observation WHERE kpi_code='KPI-CD-03'").fetchone()[0] >= 1


def test_low_confidence_reprocess_remains_unactivated_and_retry_does_not_repeat_ocr(database):
    document, source = _seed(database, ocr_state="Rechazado por baja confianza", processing_state="RECHAZADA")
    database["state"]["confidence"] = 0.4
    response, payload = _request(database, document, source)
    assert response.status_code == 200, response.text
    assert response.json()["processing_state"] == "RECHAZADA"
    assert response.json()["ocr_state"] == "Rechazado por baja confianza"
    assert database["client"].post(f"/api/documents/{document}/ocr/reprocess", json=payload).json() == response.json()
    assert database["state"]["ocr_calls"] == 1
    assert database["encoder"].calls == 0
    assert _counts(database, document) == (2, 0, 0, None)


@pytest.mark.parametrize("ineligible", ["quarantine", "rejected_file", "wrong_object", "missing_candidate", "wrong_document", "invalidated", "failed_version"])
def test_ineligible_source_is_rejected_without_pipeline_or_new_version(database, ineligible):
    document, source = _seed(database)
    with psycopg.connect(database["owner"]) as connection:
        if ineligible == "quarantine":
            connection.execute("""INSERT INTO app.quarantine_item(id,ingest_file_id,cause_code,state,operation_id,correlation_id,original_payload)
                VALUES (%s,%s,'TECHNICAL_READ_FAILURE','Pendiente',%s,%s,'{}'::jsonb)""", (uuid4(), document, uuid4(), uuid4()))
        elif ineligible == "rejected_file":
            connection.execute("UPDATE app.ingest_file SET state='RECHAZADO',technical_result='REJECTED',safe_cause_code='TECHNICAL_READ_FAILURE' WHERE id=%s", (document,))
        elif ineligible == "wrong_object":
            other = uuid4()
            connection.execute("""INSERT INTO app.stored_object(id,storage_kind,locator,sha256,mime_type,byte_size,original_name)
                VALUES (%s,'FILESYSTEM',%s,%s,'application/pdf',10,'other.pdf')""", (other, f"test/{other}/other.pdf", bytes(32)))
            connection.execute("UPDATE app.document_version SET stored_object_id=%s WHERE id=%s", (other, source))
        elif ineligible == "missing_candidate":
            connection.execute("DELETE FROM app.document_candidate_page WHERE ingest_file_id=%s", (document,))
            connection.execute("DELETE FROM app.document_candidate WHERE ingest_file_id=%s", (document,))
        elif ineligible == "invalidated":
            connection.execute("UPDATE app.document SET invalidated_at=CURRENT_TIMESTAMP WHERE id_documento=%s", (document,))
        elif ineligible == "failed_version":
            connection.execute("UPDATE app.document_version SET processing_state='FALLIDA',completed_at=CURRENT_TIMESTAMP WHERE id=%s", (source,))
    if ineligible != "wrong_document":
        item = database["client"].get("/api/documents/ocr", params={"document_id": document}).json()["items"][0]
        assert item["reprocess_eligible"] is False
    target = "not-visible" if ineligible == "wrong_document" else document
    response, _ = _request(database, target, source)
    assert response.status_code == 409
    assert document not in response.text and str(source) not in response.text
    assert database["state"]["runner_calls"] == database["encoder"].calls == 0
    assert _counts(database, document) == (1, 0, 0, None)


def test_kpi_failure_preserves_certified_upstream_then_resumes_without_ocr(database):
    document, source = _seed(database)
    original = database["integration"].analytics.recalculate
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic KPI failure")
    database["integration"].analytics.recalculate = fail
    response, payload = _request(database, document, source)
    assert response.status_code == 503
    assert response.json()["detail"]["cause"] == "UPSTREAM_COMMITTED_KPI_RETRYABLE"
    assert _counts(database, document)[:3] == (2, 1, 1)
    operation = response.json()["detail"]["operation_id"]
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT state FROM app.job_run WHERE operation_id=%s", (operation,)).fetchone()[0] == "FAILED"
    database["integration"].analytics.recalculate = original
    repeated = database["client"].post(f"/api/documents/{document}/ocr/reprocess", json=payload)
    assert repeated.status_code == 200 and repeated.json()["kpi_job_state"] == "COMPLETED"
    assert database["state"]["ocr_calls"] == database["state"]["runner_calls"] == 1
    assert _counts(database, document)[:3] == (2, 1, 1)


def test_concurrent_same_request_has_one_certification_activation_and_job(database):
    document, source = _seed(database)
    payload = ReprocessRequest(source_document_version_id=source, request_id=uuid4())
    def execute():
        return database["service"].reprocess(document, payload, database["principal"])
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(execute) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    assert results[0] == results[1]
    assert _counts(database, document)[:3] == (2, 1, 1)
    assert database["state"]["ocr_calls"] == 1


def test_revocation_during_ocr_prevents_activation_and_success_audit(database):
    document, source = _seed(database)
    def revoke():
        with psycopg.connect(database["owner"]) as connection:
            connection.execute("DELETE FROM app.user_role WHERE user_id=%s", (database["principal"].account_id,))
    database["state"]["ocr_hook"] = revoke
    response, _ = _request(database, document, source)
    assert response.status_code == 403
    assert document not in response.text and str(source) not in response.text
    versions, certificates, audits, active = _counts(database, document)
    assert versions == 2 and certificates == 1
    assert audits == 0 and active is None
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE actor_user_id=%s AND action='AUTHORIZATION_DENIED'",
                                  (database["principal"].account_id,)).fetchone()[0] == 1


def test_failed_candidate_preserves_previously_certified_active_version(database):
    document, source = _seed(database)
    first, _ = _request(database, document, source)
    assert first.status_code == 200
    active = first.json()["document_version_id"]
    database["state"]["confidence"] = 0.4
    rejected, _ = _request(database, document, source)
    assert rejected.status_code == 200 and rejected.json()["processing_state"] == "RECHAZADA"
    assert tuple(str(value) if index == 3 else value
                 for index, value in enumerate(_counts(database, document))) == (3, 1, 1, active)
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM app.document_index_certificate WHERE document_version_id=%s",
                                  (rejected.json()["document_version_id"],)).fetchone()[0] == 0


def test_concurrent_different_requests_keep_distinct_serialized_versions(database):
    document, source = _seed(database)
    def execute():
        response, _ = _request(database, document, source)
        return response
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=30) for future in [executor.submit(execute), executor.submit(execute)]]
    assert any(response.status_code == 200 for response in results)
    assert all(response.status_code in {200, 409, 503} for response in results)
    with psycopg.connect(database["owner"]) as connection:
        versions = connection.execute("SELECT version_number,operation_id FROM app.document_version WHERE id_documento=%s ORDER BY version_number",
                                      (document,)).fetchall()
        assert [row[0] for row in versions] == [1, 2, 3]
        assert len({row[1] for row in versions}) == 3
        active = connection.execute("SELECT active_version_id FROM app.document WHERE id_documento=%s", (document,)).fetchone()[0]
        assert connection.execute("SELECT processing_state FROM app.document_version WHERE id=%s", (active,)).fetchone()[0] == "LISTA"
        assert connection.execute("SELECT count(*) FROM app.document_index_certificate c JOIN app.document_version v ON v.id=c.document_version_id WHERE v.id_documento=%s",
                                  (document,)).fetchone()[0] == 2
