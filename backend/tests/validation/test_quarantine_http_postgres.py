"""Fronteras HTTP de cuarentena sobre PostgreSQL existente y datos sintéticos."""
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

from app.security.api import current_principal
from app.security.models import AuditPersistenceError, AuthenticatedPrincipal
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService
from app.validation.api import router, validation_service_for
from app.validation.repository import ValidationRepository
from app.validation.service import ValidationContext, ValidationService


@pytest.fixture
def database():
    owner = os.getenv("DATA_TEST_DATABASE_URL", "")
    runtime = os.getenv("RUNTIME_TEST_DATABASE_URL", "")
    if not owner or not runtime:
        pytest.fail("La prueba requiere conexiones protegidas owner/runtime a una base de pruebas existente")
    assert "test" in conninfo_to_dict(owner).get("dbname", "").lower()
    assert conninfo_to_dict(runtime)["dbname"] == conninfo_to_dict(owner)["dbname"]
    with psycopg.connect(runtime) as connection:
        assert connection.execute("SELECT session_user,current_user").fetchone() == ("riesgo_legal_app", "riesgo_legal_app")
    account, session = uuid4(), uuid4()
    with psycopg.connect(owner, row_factory=dict_row) as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone()["version_num"] == "0016_rag_operation_lifecycle"
        connection.execute("INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES (%s,%s,'Prueba','hash')", (account, f"quarantine-http-{account.hex}"))
        connection.execute("INSERT INTO app.user_role(user_id,role_id) VALUES (%s,'ANALISTA')", (account,))
        connection.execute("""INSERT INTO app.access_session(id,user_id,refresh_token_sha256,authorization_version,state,expires_at)
            VALUES (%s,%s,%s,1,'ACTIVE',%s)""", (session, account, bytes(32), datetime.now(UTC) + timedelta(hours=1)))
    principal = AuthenticatedPrincipal(account, session, f"quarantine-http-{account.hex}", 1, frozenset({"ANALISTA"}), frozenset())
    security = SecurityService(SecurityRepository(runtime), JwtService(issuer="test", audience="test", keyring={"test": base64.urlsafe_b64encode(bytes(range(32))).decode()}, active_kid="test"))
    service = ValidationService(ValidationRepository(runtime, security.repository), security)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[current_principal] = lambda: principal
    application.dependency_overrides[validation_service_for] = lambda: service
    return owner, service, principal, TestClient(application)


def _seed(owner, *, count=1, stamp=None):
    file_id, object_id = uuid4(), uuid4()
    identities = [uuid4() for _ in range(count)]
    with psycopg.connect(owner) as connection:
        connection.execute("""INSERT INTO app.stored_object(id,storage_kind,locator,sha256,mime_type,byte_size,original_name)
            VALUES (%s,'FILESYSTEM',%s,%s,'text/csv',10,'synthetic.csv')""", (object_id, f"test/{object_id}/synthetic.csv", bytes(32)))
        connection.execute("""INSERT INTO app.ingest_file(id,stored_object_id,source_family,exchange_format,state,operation_id,correlation_id,
            declared_extension,detected_format,format_classification,technical_result,declared_name,source_locator,source_revision,actor_identifier,content_sha256)
            VALUES (%s,%s,'CONTRATOS_DOCUMENTOS','CSV','COMPLETADO',%s,%s,'.csv','CSV','SUPPORTED','ACCEPTED','synthetic.csv',%s,1,'test',%s)""",
            (file_id, object_id, uuid4(), uuid4(), f"test/{file_id}", bytes(32)))
        for identity in identities:
            connection.execute("""INSERT INTO app.quarantine_item(id,ingest_file_id,cause_code,state,original_payload,operation_id,correlation_id,created_at)
                VALUES (%s,%s,'INVALID_TYPE','Pendiente','{}'::jsonb,%s,%s,%s)""", (identity, file_id, uuid4(), uuid4(), stamp or datetime.now(UTC)))
    return file_id, identities


def _events(owner, principal, action):
    with psycopg.connect(owner) as connection:
        return connection.execute("SELECT resource_identifier FROM audit.event WHERE actor_user_id=%s AND action=%s", (principal.account_id, action)).fetchall()


def test_query_filters_nullable_provenance_and_audits_only_delivered_items(database):
    owner, _, principal, client = database
    stamp = datetime(2030, 1, 1, tzinfo=UTC)
    file_id, ids = _seed(owner, count=3, stamp=stamp)
    params = {"ingest_file_id": str(file_id), "source_family": "CONTRATOS_DOCUMENTOS", "state": "Pendiente", "cause_code": "INVALID_TYPE", "rejected_from": stamp.isoformat(), "rejected_to": stamp.isoformat(), "limit": 2}
    first = client.get("/api/validation/quarantine", params=params)
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert all(row["source_record_id"] is None and row["row_number"] is None for row in first.json()["items"])
    assert len(_events(owner, principal, "QUARANTINE_VIEW")) == 2
    second = client.get("/api/validation/quarantine", params={**params, "cursor": first.json()["next_cursor"]})
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["next_cursor"] is None
    returned = [row["id"] for row in first.json()["items"] + second.json()["items"]]
    assert returned == [str(identity) for identity in sorted(ids)]
    assert len(_events(owner, principal, "QUARANTINE_VIEW")) == 3
    assert not {"operation_id", "correlation_id"}.intersection(first.json()["items"][0])


def test_query_each_filter_is_applied_before_returning_payload(database):
    owner, _, principal, client = database
    file_id, _ = _seed(owner, stamp=datetime(2030, 1, 1, tzinfo=UTC))
    for extra in ({"source_family": "AUDITORIA_INTERNA"}, {"cause_code": "INVALID_DATE"}, {"state": "Descartado"}, {"rejected_from": "2031-01-01T00:00:00Z"}, {"rejected_to": "2029-01-01T00:00:00Z"}):
        response = client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id), **extra})
        assert response.status_code == 200
        assert response.json()["items"] == []
    assert _events(owner, principal, "QUARANTINE_VIEW") == []


def test_query_preserves_physical_row_location_and_ti_current_authority(database):
    owner, _, principal, client = database
    file_id, ids = _seed(owner)
    source = uuid4()
    with psycopg.connect(owner) as connection:
        connection.execute("UPDATE app.user_role SET role_id='TI' WHERE user_id=%s", (principal.account_id,))
        connection.execute("""INSERT INTO app.source_record(id,ingest_file_id,row_number,source_sheet,raw_payload,record_sha256,extraction_state)
            VALUES (%s,%s,12,'CSV','{}'::jsonb,%s,'EXTRAIDO')""", (source, file_id, bytes(32)))
        connection.execute("UPDATE app.quarantine_item SET source_record_id=%s WHERE id=%s", (source, ids[0]))
    response = client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id)})
    assert response.status_code == 200
    assert response.json()["items"][0]["row_number"] == 12
    assert response.json()["items"][0]["source_record_id"] == str(source)
    assert client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "motivo TI"}).status_code == 200


@pytest.mark.parametrize("revocation", ["role", "session"])
def test_revoked_authorization_precedes_query_and_denial_is_durable(database, revocation):
    owner, _, principal, client = database
    file_id, _ = _seed(owner)
    with psycopg.connect(owner) as connection:
        if revocation == "role":
            connection.execute("DELETE FROM app.user_role WHERE user_id=%s", (principal.account_id,))
        else:
            connection.execute("UPDATE app.access_session SET state='INVALIDATED',invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", (principal.session_id,))
    response = client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id)})
    assert response.status_code == 403
    assert str(file_id) not in response.text
    assert _events(owner, principal, "QUARANTINE_VIEW") == []
    assert len(_events(owner, principal, "AUTHORIZATION_DENIED")) == 1


def test_juridico_cannot_query_or_discard_and_gets_one_audit_per_denial(database):
    owner, _, principal, client = database
    file_id, ids = _seed(owner)
    with psycopg.connect(owner) as connection:
        connection.execute("UPDATE app.user_role SET role_id='JURIDICO' WHERE user_id=%s", (principal.account_id,))
    assert client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id)}).status_code == 403
    response = client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "motivo"})
    assert response.status_code == 403
    assert str(ids[0]) not in response.text
    assert len(_events(owner, principal, "AUTHORIZATION_DENIED")) == 2
    assert _events(owner, principal, "VALIDATION_DENIED") == []
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT state FROM app.quarantine_item WHERE id=%s", (ids[0],)).fetchone()[0] == "Pendiente"


def test_visualization_audit_failure_rolls_back_page_and_all_partial_audits(database):
    owner, service, principal, client = database
    file_id, _ = _seed(owner, count=2)
    original = service.repository.write_audit_event
    calls = []
    def fail_second(connection, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("synthetic failure")
        return original(connection, **kwargs)
    service.repository.write_audit_event = fail_second
    response = client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id)})
    assert response.status_code == 503
    assert "items" not in response.json()
    assert _events(owner, principal, "QUARANTINE_VIEW") == []


def test_discard_is_atomic_with_transition_audit_and_original_payload(database):
    owner, _, principal, client = database
    _, ids = _seed(owner)
    response = client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": " motivo válido "})
    assert response.status_code == 200
    assert response.json()["discard_justification"] == "motivo válido"
    assert client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "otra"}).status_code == 409
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT state,original_payload,discard_justification FROM app.quarantine_item WHERE id=%s", (ids[0],)).fetchone() == ("Descartado", {}, "motivo válido")
        assert connection.execute("SELECT count(*) FROM app.quarantine_transition WHERE quarantine_item_id=%s", (ids[0],)).fetchone()[0] == 1
    assert len(_events(owner, principal, "QUARANTINE_DISCARD")) == 1


def test_discard_audit_failure_rolls_back_domain_effects(database):
    owner, service, _, client = database
    _, ids = _seed(owner)
    service.repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    response = client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "motivo"})
    assert response.status_code == 503
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT state FROM app.quarantine_item WHERE id=%s", (ids[0],)).fetchone()[0] == "Pendiente"
        assert connection.execute("SELECT count(*) FROM app.quarantine_transition WHERE quarantine_item_id=%s", (ids[0],)).fetchone()[0] == 0


def test_typed_audit_failure_is_unavailable_not_authorization_denial(database):
    owner, service, principal, client = database
    _, ids = _seed(owner)
    service.repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(AuditPersistenceError("synthetic failure"))
    assert client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "motivo"}).status_code == 503
    assert client.get("/api/validation/quarantine").status_code == 503
    assert _events(owner, principal, "AUTHORIZATION_DENIED") == []
    with psycopg.connect(owner) as connection:
        assert connection.execute("SELECT state FROM app.quarantine_item WHERE id=%s", (ids[0],)).fetchone()[0] == "Pendiente"


def test_denial_audit_failure_is_fail_closed_without_payload(database):
    owner, service, principal, client = database
    file_id, ids = _seed(owner)
    with psycopg.connect(owner) as connection:
        connection.execute("UPDATE app.user_role SET role_id='JURIDICO' WHERE user_id=%s", (principal.account_id,))
    service.repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(AuditPersistenceError("synthetic failure"))
    response = client.get("/api/validation/quarantine", params={"ingest_file_id": str(file_id)})
    assert response.status_code == 503
    assert str(file_id) not in response.text
    assert client.post(f"/api/validation/quarantine/{ids[0]}/discard", json={"justification": "motivo"}).status_code == 503
    assert _events(owner, principal, "AUTHORIZATION_DENIED") == []


def test_concurrent_discard_has_one_confirmed_transition(database):
    owner, service, principal, _ = database
    _, ids = _seed(owner)
    def execute():
        try:
            service.discard_for_http(item_id=ids[0], justification="motivo", context=ValidationContext(uuid4(), uuid4(), principal))
            return "confirmed"
        except Exception as exc:
            from app.validation.quarantine import QuarantineError
            assert isinstance(exc, QuarantineError)
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: execute(), range(2)))
    assert sorted(results) == ["confirmed", "rejected"]
    assert len(_events(owner, principal, "QUARANTINE_DISCARD")) == 1
