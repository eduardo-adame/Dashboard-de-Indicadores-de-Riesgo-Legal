"""Integración RAG con PostgreSQL desechable y evidencia de auditoría real."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import hashlib
import os
from pathlib import Path
import re
from threading import Event, Lock
from time import monotonic, sleep
from urllib.error import HTTPError
from uuid import uuid4

from alembic import command
from alembic.config import Config
import psycopg
import pytest
from fastapi.testclient import TestClient

from app.corpus.tokenizer import FastTokenizer
from app.main import app
from app.rag.index_repository import IndexRepository
from app.rag.indexing import IndexingService
from app.rag.models import EvidenceState
from app.rag.provider import GROQ_CHAT_URL, GroqProvider
from app.rag.retrieval import RetrievalService
from app.rag.retrieval_repository import RetrievalRepository
from app.rag.service import RagApplicationError, RagApplicationService
from app.security.models import AuditPersistenceError, AuthenticatedPrincipal, ROLE_PERMISSIONS
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _Database(dict):
    def __repr__(self) -> str:
        return f"_Database(name={self['name']!r}, credentials='<redacted>')"


class _Encoder:
    def encode_documents(self, texts, normalize_embeddings=False):
        return [[0.8, 0.2, *([0.0] * 1022)] for _ in texts]

    def encode_query(self, _query):
        return [1.0, 0.0, *([0.0] * 1022)]


class _Provider:
    def __init__(self):
        self.calls = 0
        self._lock = Lock()

    def generate(self, messages, *, before_send):
        before_send()
        assert len(messages) == 2
        with self._lock:
            self.calls += 1
        return "El plazo es de treinta días [E1]."


@pytest.fixture(scope="module")
def database():
    host = os.environ.get("POSTGRES_HOST", "")
    owner = os.environ.get("POSTGRES_USER", "")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    runtime = os.environ.get("RUNTIME_POSTGRES_USER", "")
    runtime_password = os.environ.get("RUNTIME_POSTGRES_PASSWORD", "")
    if not all((host, owner, password, runtime, runtime_password)):
        pytest.fail("Falta configuración protegida para PostgreSQL de pruebas")
    port = os.environ.get("POSTGRES_PORT", "5432")
    name = f"rag_application_test_{uuid4().hex}"
    maintenance = f"host={host} port={port} dbname=postgres user={owner} password={password}"
    owner_dsn = f"host={host} port={port} dbname={name} user={owner} password={password}"
    runtime_dsn = f"host={host} port={port} dbname={name} user={runtime} password={runtime_password}"
    created = False
    try:
        with psycopg.connect(maintenance, autocommit=True) as admin:
            assert admin.execute("SELECT current_database(), current_user").fetchone() == ("postgres", owner)
            assert name.startswith("rag_application_test_")
            admin.execute(f'CREATE DATABASE "{name}"')
            created = True
        previous = os.environ.get("POSTGRES_DB")
        try:
            os.environ["POSTGRES_DB"] = name
            command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "0016_rag_operation_lifecycle")
        finally:
            if previous is None:
                os.environ.pop("POSTGRES_DB", None)
            else:
                os.environ["POSTGRES_DB"] = previous
        with psycopg.connect(owner_dsn, autocommit=True) as connection:
            assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == "0016_rag_operation_lifecycle"
        yield _Database(name=name, owner=owner_dsn, runtime=runtime_dsn)
    finally:
        if created:
            with psycopg.connect(maintenance, autocommit=True) as admin:
                assert name.startswith("rag_application_test_")
                assert admin.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (name,)).fetchone()[0] == 0
                admin.execute(f'DROP DATABASE "{name}"')


def _principal(database) -> AuthenticatedPrincipal:
    identity, session = uuid4(), uuid4()
    username = f"rag-application-{identity.hex}"
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.user_account (id, username, display_name, password_hash) "
            "VALUES (%s, %s, 'Synthetic', 'hash')", (identity, username),
        )
        connection.execute("INSERT INTO app.user_role (user_id, role_id) VALUES (%s, 'JURIDICO')", (identity,))
        connection.execute(
            "INSERT INTO app.access_session (id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
            "VALUES (%s, %s, %s, 1, 'ACTIVE', %s)",
            (session, identity, bytes(32), datetime.now(UTC) + timedelta(hours=2)),
        )
    return AuthenticatedPrincipal(identity, session, username, 1, frozenset({"JURIDICO"}), ROLE_PERMISSIONS["JURIDICO"])


def _grant(database, active=True):
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        SecurityRepository.set_scope_grant(connection, "JURIDICO", "CONTRATOS_DOCUMENTOS", active)


def _document(database):
    text = "El contrato sintético establece un plazo de treinta días."
    file_id, stored_id, version_id = uuid4(), uuid4(), uuid4()
    digest = hashlib.sha256(text.encode()).digest()
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
            "VALUES (%s, 'FILESYSTEM', %s, %s, 'application/pdf', 10, 'synthetic.pdf')",
            (stored_id, f"synthetic/{stored_id}", digest),
        )
        connection.execute(
            "INSERT INTO app.ingest_file (id, stored_object_id, source_family, exchange_format, state, "
            "operation_id, correlation_id, declared_extension, detected_format, format_classification, "
            "technical_result, declared_name, source_locator, source_revision, actor_identifier, content_sha256) "
            "VALUES (%s, %s, 'CONTRATOS_DOCUMENTOS', 'PDF', 'COMPLETADO', %s, %s, '.pdf', 'PDF', "
            "'SUPPORTED', 'ACCEPTED', 'synthetic.pdf', %s, 1, 'test', %s)",
            (file_id, stored_id, uuid4(), uuid4(), f"synthetic/{file_id}", digest),
        )
        connection.execute(
            "INSERT INTO app.document (id_documento, name, document_type, source_family, document_date) "
            "VALUES (%s, 'Synthetic document', 'CONTRATO', 'CONTRATOS_DOCUMENTOS', %s)",
            (str(file_id), date(2026, 1, 1)),
        )
        connection.execute(
            "INSERT INTO app.document_version (id, id_documento, version_number, stored_object_id, "
            "processing_state, consolidated_text, content_sha256, operation_id, correlation_id) "
            "VALUES (%s, %s, 1, %s, 'PROCESANDO', %s, %s, %s, %s)",
            (version_id, str(file_id), stored_id, text, digest, uuid4(), uuid4()),
        )
    indexed = IndexingService(IndexRepository(database["owner"]), _Encoder()).index_version(
        document_version_id=version_id, consolidated_text=text, tokenizer=FastTokenizer()
    )
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute("UPDATE app.document SET active_version_id=%s WHERE id_documento=%s", (version_id, str(file_id)))
    return indexed.fragment_ids[0]


def _service(database, provider=None):
    security = SecurityService(SecurityRepository(database["runtime"]))
    return RagApplicationService(
        conninfo=database["runtime"], security=security,
        retrieval=RetrievalService(security, encoder=_Encoder()), provider=provider or _Provider(),
    )


def _synthetic_deadline(response: str) -> str | None:
    match = re.search(r"\b(?:treinta|30)\s+d[íi]as\b", response, flags=re.IGNORECASE)
    return match.group(0) if match else None


@pytest.mark.parametrize("response", (
    "El plazo es de treinta días [E1].",
    "El plazo es de 30   DÍAS [E1].",
))
def test_rf055_synthetic_deadline_accepts_equivalent_facts(response: str) -> None:
    assert _synthetic_deadline(response) is not None


@pytest.mark.parametrize("response", (
    "No puedo determinar el plazo. [E1]",
    "El plazo es de cuarenta días [E1].",
))
def test_rf055_synthetic_deadline_rejects_abstention_and_wrong_fact(response: str) -> None:
    assert _synthetic_deadline(response) is None


def test_state_three_persists_operation_and_atomic_audit(database) -> None:
    principal = _principal(database)
    service = _service(database)
    result = service.execute(principal=principal, query="Consulta sintética", idempotency_key=uuid4())
    assert result.status_code == 200
    assert result.payload["state"] == "SIN_EVIDENCIA"
    assert result.payload["fragments"] == []
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        row = connection.execute(
            "SELECT r.query_sha256, e.query_sha256, e.action FROM app.rag_operation r "
            "JOIN audit.event e ON e.operation_id=r.operation_id WHERE r.operation_id=%s",
            (result.payload["operation_id"],),
        ).fetchone()
        assert bytes(row[0]) == bytes(row[1])
        assert row[2] == "RAG_QUERY"


def test_state_two_preserves_reference_fragments_without_generation(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    provider = _Provider()
    service = _service(database, provider)
    original = service.retrieval.retrieve

    def insufficient(connection, context):
        return replace(original(connection, context), state=EvidenceState.EVIDENCIA_INSUFICIENTE)

    service.retrieval.retrieve = insufficient
    result = service.execute(principal=principal, query="¿Cuál es el plazo?")
    assert result.status_code == 200
    assert result.payload["state"] == "EVIDENCIA_INSUFICIENTE"
    assert result.payload["generation_status"] == "NOT_REQUESTED"
    assert result.payload["generated_response"] is None
    assert result.payload["citations"] == []
    assert result.payload["fragments"]
    assert provider.calls == 0
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM app.rag_final_fragment f JOIN app.rag_operation r ON r.id=f.rag_operation_id "
            "WHERE r.operation_id=%s AND f.usage='REFERENCE'",
            (result.payload["operation_id"],),
        ).fetchone()[0] == len(result.payload["fragments"])


def test_rf048_reference_is_persisted_only_when_valid(database) -> None:
    principal = _principal(database)
    run_id, finding_id, reference_id = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.analytic_run (id, run_type, state, result, operation_id, correlation_id, completed_at) "
            "VALUES (%s, 'PROACTIVE_ANALYSIS', 'COMPLETED', 'SUCCESS', %s, %s, CURRENT_TIMESTAMP)",
            (run_id, uuid4(), uuid4()),
        )
        connection.execute(
            "INSERT INTO app.finding (id, analytic_run_id, finding_type, description, triggered_rule, state) "
            "VALUES (%s, %s, 'SIGNAL', 'Hallazgo sintético', 'RULE_SYNTHETIC', 'OPEN')",
            (finding_id, run_id),
        )
        connection.execute(
            "INSERT INTO app.context_reference (id, finding_id, operation_id, correlation_id) "
            "VALUES (%s, %s, %s, %s)",
            (reference_id, finding_id, uuid4(), uuid4()),
        )
    _grant(database, active=False)
    service = _service(database)
    key = uuid4()
    valid = service.execute(
        principal=principal, query="Consulta sintética", idempotency_key=key,
        context_reference_id=reference_id,
    )
    invalid = service.execute(principal=principal, query="Consulta sintética", context_reference_id=uuid4())
    assert valid.payload["context_reference_id"] == reference_id
    assert invalid.payload["context_reference_id"] is None
    assert valid.payload["state"] == invalid.payload["state"] == "SIN_EVIDENCIA"
    assert valid.payload["fragments"] == invalid.payload["fragments"] == []
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT context_reference_id FROM app.rag_operation WHERE operation_id=%s",
            (valid.payload["operation_id"],),
        ).fetchone()[0] == reference_id
        assert connection.execute(
            "SELECT context_reference_id FROM app.rag_operation WHERE operation_id=%s",
            (invalid.payload["operation_id"],),
        ).fetchone()[0] is None
    with pytest.raises(RagApplicationError) as error:
        service.execute(
            principal=principal, query="Consulta sintética", idempotency_key=key,
            context_reference_id=uuid4(),
        )
    assert error.value.status_code == 409


def test_idempotency_conflicts_on_query_and_session_without_new_audit(database) -> None:
    _grant(database, active=False)
    principal = _principal(database)
    key = uuid4()
    service = _service(database)
    first = service.execute(principal=principal, query="Consulta A", idempotency_key=key)
    with pytest.raises(RagApplicationError) as query_error:
        service.execute(principal=principal, query="Consulta B", idempotency_key=key)
    assert query_error.value.status_code == 409
    other_session = uuid4()
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.access_session (id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
            "VALUES (%s, %s, %s, 1, 'ACTIVE', %s)",
            (other_session, principal.account_id, bytes([1]) * 32, datetime.now(UTC) + timedelta(hours=2)),
        )
    with pytest.raises(RagApplicationError) as session_error:
        service.execute(
            principal=replace(principal, session_id=other_session),
            query="Consulta A", idempotency_key=key,
        )
    assert session_error.value.status_code == 409
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM app.rag_operation WHERE operation_id=%s",
            (first.payload["operation_id"],),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s",
            (first.payload["operation_id"],),
        ).fetchone()[0] == 1


def test_state_one_cites_persisted_authorized_fragment_and_retry_is_idempotent(database) -> None:
    _grant(database)
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute("UPDATE app.document SET active_version_id=NULL WHERE active_version_id IS NOT NULL")
    fragment = _document(database)
    principal = _principal(database)
    provider = _Provider()
    key = uuid4()
    service = _service(database, provider)
    first = service.execute(principal=principal, query="¿Cuál es el plazo del contrato?", idempotency_key=key)
    second = _service(database, provider).execute(
        principal=principal, query="¿Cuál es el plazo del contrato?", idempotency_key=key
    )
    assert first.status_code == second.status_code == 200
    assert first.payload["state"] == "EVIDENCIA_SUFICIENTE"
    assert first.payload["operation_id"] == second.payload["operation_id"]
    assert any(item["fragment_id"] == fragment for item in first.payload["fragments"])
    assert first.payload["citations"] == second.payload["citations"]
    assert first.payload["citations"][0]["handle"] == "E1"
    assert first.payload["citations"][0]["fragment_id"] == fragment
    assert provider.calls == 1
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        operation_id = first.payload["operation_id"]
        assert connection.execute("SELECT count(*) FROM app.rag_operation WHERE operation_id=%s", (operation_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit.event WHERE operation_id=%s", (operation_id,)).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM app.rag_final_fragment f JOIN app.rag_operation r ON r.id=f.rag_operation_id "
            "WHERE r.operation_id=%s AND f.fragment_id=%s", (operation_id, fragment),
        ).fetchone()[0] == 1


def test_http_jwt_to_retrieval_and_audit_revalidates_revoked_retry(database, monkeypatch) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    secret = base64.urlsafe_b64encode(b"clave-sintetica-de-prueba-32-bytes!!").decode().rstrip("=")
    tokens = JwtService(issuer="rag-test", audience="rag-test", keyring={"test": secret}, active_kid="test")
    security = SecurityService(SecurityRepository(database["runtime"]), tokens)
    provider = _Provider()
    service = RagApplicationService(
        conninfo=database["runtime"], security=security,
        retrieval=RetrievalService(security, encoder=_Encoder()), provider=provider,
    )
    monkeypatch.setattr(app.state, "security_service", security, raising=False)
    monkeypatch.setattr(app.state, "rag_application_service", service, raising=False)
    access = tokens.issue(
        account_id=principal.account_id, session_id=principal.session_id,
        authorization_version=principal.authorization_version,
    )
    key = uuid4()
    client = TestClient(app)
    assert client.post(
        "/api/rag/query", json={"query": "¿Cuál es el plazo del contrato?"},
        headers={"Authorization": "Bearer not.a.valid.jwt"},
    ).status_code == 401
    first = client.post(
        "/api/rag/query", json={"query": "¿Cuál es el plazo del contrato?"},
        headers={"Authorization": f"Bearer {access}", "Idempotency-Key": str(key)},
    )
    assert first.status_code == 200
    assert first.json()["state"] == "EVIDENCIA_SUFICIENTE"
    assert first.json()["citations"][0]["fragment_id"] in {
        item["fragment_id"] for item in first.json()["fragments"]
    }
    assert provider.calls == 1
    _grant(database, active=False)
    denied = client.post(
        "/api/rag/query", json={"query": "¿Cuál es el plazo del contrato?"},
        headers={"Authorization": f"Bearer {access}", "Idempotency-Key": str(key)},
    )
    assert denied.status_code == 403
    assert "fragment" not in denied.text.lower()
    assert provider.calls == 1
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s",
            (first.json()["operation_id"],),
        ).fetchone()[0] == 1


def test_explicit_denial_is_state_four_with_one_audit_and_no_fragments(database) -> None:
    principal = _principal(database)
    key = uuid4()
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute("DELETE FROM app.user_role WHERE user_id=%s", (principal.account_id,))
    service = _service(database)
    result = service.execute(principal=principal, query="Consulta sintética", idempotency_key=key)
    assert result.status_code == 403
    assert result.payload["state"] == "SIN_AUTORIZACION"
    assert result.payload["fragments"] == []
    assert result.payload["citations"] == []
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s AND action='AUTHORIZATION_DENIED'",
            (result.payload["operation_id"],),
        ).fetchone()[0] == 1
    retry = service.execute(principal=principal, query="Consulta sintética", idempotency_key=key)
    assert retry.status_code == 403
    assert retry.payload["operation_id"] == result.payload["operation_id"]
    assert retry.payload["fragments"] == []
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE actor_user_id=%s AND action='AUTHORIZATION_DENIED'",
            (principal.account_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM app.rag_operation WHERE operation_id=%s",
            (result.payload["operation_id"],),
        ).fetchone()[0] == 1


def test_audit_failure_rolls_back_operation_and_fragments(database, monkeypatch) -> None:
    principal = _principal(database)
    key = uuid4()
    service = _service(database)
    operation_id = service._identity(principal, key)

    def fail_audit(*_args, **_kwargs):
        raise AuditPersistenceError("fallo sintético")

    monkeypatch.setattr(SecurityRepository, "write_rag_audit_event", fail_audit)
    with pytest.raises(RagApplicationError):
        service.execute(principal=principal, query="Consulta sintética", idempotency_key=key)
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM app.rag_operation WHERE operation_id=%s", (operation_id,)).fetchone()[0] == 0


def test_revoked_retry_does_not_replay_persisted_evidence(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    key = uuid4()
    service = _service(database)
    first = service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert first.payload["state"] == "EVIDENCIA_SUFICIENTE"
    _grant(database, active=False)
    with pytest.raises(RagApplicationError) as error:
        service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert error.value.status_code == 403
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM app.rag_operation WHERE operation_id=%s", (first.payload["operation_id"],)
        ).fetchone()[0] == 1


def test_revoked_retry_audit_failure_is_safe_and_does_not_replay(database, monkeypatch) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    key = uuid4()
    service = _service(database)
    first = service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert first.status_code == 200
    _grant(database, active=False)

    def fail_denial_audit(*_args, **_kwargs):
        raise AuditPersistenceError("fallo sintético")

    monkeypatch.setattr(SecurityRepository, "write_audit_event", fail_denial_audit)
    with pytest.raises(RagApplicationError) as error:
        service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert error.value.status_code == 503
    assert str(error.value) == "La operación no pudo completarse"


def test_revocation_waits_until_authorized_replay_is_materialized(database, monkeypatch) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    key = uuid4()
    service = _service(database)
    first = service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert first.status_code == 200
    revocation_started = Event()
    revocation_pid = {}
    original = RetrievalRepository.final_fragments_still_authorized

    def revoke() -> None:
        with psycopg.connect(database["owner"], autocommit=True) as connection:
            with connection.transaction():
                revocation_pid["value"] = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                revocation_started.set()
                connection.execute(
                    "UPDATE app.access_session SET state='INVALIDATED', invalidated_at=CURRENT_TIMESTAMP WHERE id=%s",
                    (principal.session_id,),
                )
                SecurityRepository.set_scope_grant(connection, "JURIDICO", "CONTRATOS_DOCUMENTOS", False)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def check(repository, connection, scope, fragment_ids):
            nonlocal future
            future = pool.submit(revoke)
            assert revocation_started.wait(5)
            # La sesión queda bloqueada hasta terminar el chequeo y la lectura.
            deadline = monotonic() + 3
            while monotonic() < deadline:
                with psycopg.connect(database["owner"], autocommit=True) as observer:
                    wait_type = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (revocation_pid["value"],),
                    ).fetchone()[0]
                if wait_type == "Lock":
                    break
                sleep(0.02)
            assert wait_type == "Lock"
            assert not future.done()
            return original(repository, connection, scope, fragment_ids)

        monkeypatch.setattr(RetrievalRepository, "final_fragments_still_authorized", check)
        replay = service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
        assert replay.status_code == 200
        assert replay.payload["fragments"] == first.payload["fragments"]
        assert future is not None
        future.result(timeout=10)
    with pytest.raises(RagApplicationError) as error:
        service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key)
    assert error.value.status_code == 401


def test_revocation_after_commit_before_presentation_is_audited(database, monkeypatch) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    service = _service(database)
    original = service._existing_result

    def revoke_before_presentation(*args, **kwargs):
        _grant(database, active=False)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_existing_result", revoke_before_presentation)
    with pytest.raises(RagApplicationError) as error:
        service.execute(principal=principal, query="¿Cuál es el plazo?")
    assert error.value.status_code == 403
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM app.rag_operation WHERE user_id=%s AND state='EVIDENCIA_SUFICIENTE'",
            (principal.account_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE actor_user_id=%s AND action='AUTHORIZATION_DENIED'",
            (principal.account_id,),
        ).fetchone()[0] == 1


def test_transport_failure_persists_failed_operation_and_single_audit(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)

    class FailedProvider:
        def generate(self, _messages, *, before_send):
            before_send()
            from app.rag.provider import GroqFailure
            raise GroqFailure("PROVIDER_UNAVAILABLE")

    key = uuid4()
    result = _service(database, FailedProvider()).execute(
        principal=principal, query="¿Cuál es el plazo?", idempotency_key=key,
    )
    assert result.status_code == 503
    assert result.payload["operation_status"] == "FAILED"
    assert result.payload["generation_status"] == "FAILED"
    assert result.payload["generated_response"] is None
    assert result.payload["fragments"]
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        operation = connection.execute(
            "SELECT safe_cause_code FROM app.rag_operation WHERE operation_id=%s",
            (result.payload["operation_id"],),
        ).fetchone()
        assert operation[0] == "PROVIDER_UNAVAILABLE"
        audit = connection.execute(
            "SELECT result, count(*) FROM audit.event WHERE operation_id=%s GROUP BY result",
            (result.payload["operation_id"],),
        ).fetchone()
        assert audit == ("FAILURE", 1)


def test_exhausted_429_keeps_only_authorized_fragments_and_one_failure_audit(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)

    class RateLimitedOpener:
        calls = 0

        def open(self, _request, timeout):
            self.calls += 1
            raise HTTPError(GROQ_CHAT_URL, 429, "rate", {}, None)

    opener = RateLimitedOpener()
    provider = GroqProvider(
        api_key="clave-sintetica", timeout_seconds=1, opener=opener, sleep=lambda _: None,
    )
    result = _service(database, provider).execute(principal=principal, query="¿Cuál es el plazo?")
    assert opener.calls == 3
    assert result.status_code == 503
    assert result.payload["operation_status"] == "FAILED"
    assert result.payload["generation_status"] == "FAILED"
    assert result.payload["generated_response"] is None
    assert result.payload["fragments"]
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT safe_cause_code FROM app.rag_operation WHERE operation_id=%s",
            (result.payload["operation_id"],),
        ).fetchone()[0] == "PROVIDER_RATE_LIMITED"
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s AND result='FAILURE'",
            (result.payload["operation_id"],),
        ).fetchone()[0] == 1


def test_429_revocation_during_backoff_prevents_second_post(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)

    class RateLimitedOpener:
        calls = 0

        def open(self, _request, timeout):
            self.calls += 1
            raise HTTPError(GROQ_CHAT_URL, 429, "rate", {}, None)

    opener = RateLimitedOpener()
    provider = GroqProvider(
        api_key="clave-sintetica", timeout_seconds=1, opener=opener,
        sleep=lambda _delay: _grant(database, active=False),
    )
    result = _service(database, provider).execute(principal=principal, query="¿Cuál es el plazo?")
    assert opener.calls == 1
    assert result.status_code == 403
    assert result.payload["state"] == "SIN_AUTORIZACION"
    assert result.payload["fragments"] == []
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s AND action='AUTHORIZATION_DENIED'",
            (result.payload["operation_id"],),
        ).fetchone()[0] == 1


def test_concurrent_same_key_has_one_durable_operation_and_audit(database) -> None:
    _grant(database)
    _document(database)
    principal = _principal(database)
    key = uuid4()
    provider = _Provider()
    service = _service(database, provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: service.execute(principal=principal, query="¿Cuál es el plazo?", idempotency_key=key),
            range(2),
        ))
    assert len({result.payload["operation_id"] for result in results}) == 1
    operation_id = results[0].payload["operation_id"]
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        assert connection.execute("SELECT count(*) FROM app.rag_operation WHERE operation_id=%s", (operation_id,)).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM audit.event WHERE operation_id=%s", (operation_id,)).fetchone()[0] == 1


def test_real_groq_smoke_with_synthetic_authorized_document(database) -> None:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        pytest.fail("GROQ_API_KEY ausente; el smoke real no puede declararse PASS")
    _grant(database)
    fragment_id = _document(database)
    principal = _principal(database)
    service = _service(database, GroqProvider(api_key=key, timeout_seconds=45))
    result = service.execute(
        principal=principal,
        query="Según el documento sintético, ¿cuál es el plazo? Responde con cita [E1].",
    )
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        operation = connection.execute(
            "SELECT safe_cause_code, generated_response FROM app.rag_operation WHERE operation_id=%s",
            (result.payload["operation_id"],),
        ).fetchone()
        cause, persisted_response = operation if operation else (None, None)
        persisted = connection.execute(
            "SELECT count(*) FROM app.rag_final_fragment f JOIN app.rag_operation r ON r.id=f.rag_operation_id "
            "WHERE r.operation_id=%s AND f.fragment_id=%s AND f.usage='EVIDENCE'",
            (result.payload["operation_id"], fragment_id),
        ).fetchone()[0]
        source_text = connection.execute(
            "SELECT content FROM app.document_chunk WHERE id=%s", (fragment_id,),
        ).fetchone()[0]
        audited = connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s AND action='RAG_QUERY' AND result='SUCCESS'",
            (result.payload["operation_id"],),
        ).fetchone()[0]
    assert result.status_code == 200, f"causa_segura={cause}"
    assert result.payload["state"] == "EVIDENCIA_SUFICIENTE"
    response = result.payload["generated_response"]
    assert response and response.strip()
    assert "[E1]" in response
    observed_fact = _synthetic_deadline(response)
    assert observed_fact is not None, "La respuesta no fundamentó el plazo sintético"
    assert _synthetic_deadline(source_text) is not None
    assert len(result.payload["fragments"]) == 1
    assert result.payload["fragments"][0]["fragment_id"] == fragment_id
    assert len(result.payload["citations"]) == 1
    assert result.payload["citations"][0]["handle"] == "E1"
    assert result.payload["citations"][0]["fragment_id"] == fragment_id
    assert persisted_response == response
    assert _synthetic_deadline(persisted_response) is not None
    assert persisted == 1
    assert audited == 1
    assert key not in str(result.payload)
    print(f"RF055_OBSERVED_SYNTHETIC_FACT={observed_fact}")
    print("RF055_CITATION_HANDLES=E1")
    print("RF055_PERSISTED_RESPONSE_FACT=PASS")
