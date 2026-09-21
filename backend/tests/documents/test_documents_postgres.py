"""Evidencia PostgreSQL del ciclo de vida documental y activación atómica (ADR-005)."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import psycopg
from psycopg.rows import dict_row
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.documents.models import DocumentVersionState, StaleWriteError, VersionNotReadyError
from app.documents.repository import DocumentsRepository
from app.documents.service import DocumentOperationContext, DocumentsService
from app.security.models import AuthenticatedPrincipal
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    if "test" not in (make_url(value).database or "").lower():
        pytest.fail("La evidencia requiere una base desechable")
    return value


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _make_principal(engine: sa.Engine, role: str) -> AuthenticatedPrincipal:
    account_id, session_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :u, 'User', 'hash')"), {"id": account_id, "u": f"{role.lower()}-{account_id.hex[:8]}"})
        connection.execute(sa.text("INSERT INTO app.user_role (user_id, role_id) VALUES (:id, :role)"), {"id": account_id, "role": role})
        connection.execute(
            sa.text("""INSERT INTO app.access_session
                (id, user_id, refresh_token_sha256, authorization_version, state, expires_at)
                VALUES (:session, :user, :hash, 1, 'ACTIVE', :expires)"""),
            {"session": session_id, "user": account_id, "hash": bytes(32), "expires": datetime.now(UTC) + timedelta(hours=1)},
        )
    from app.security.models import ROLE_PERMISSIONS
    return AuthenticatedPrincipal(account_id, session_id, role, 1, frozenset({role}), ROLE_PERMISSIONS[role])


@pytest.fixture
def database(tmp_path: Path):
    url = _url()
    parsed = make_url(url)
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    engine = sa.create_engine(url)
    conninfo = _conninfo(url)
    encoded_secret = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    security = SecurityService(
        SecurityRepository(conninfo),
        JwtService(issuer="issuer", audience="audience", keyring={"key": encoded_secret}, active_kid="key"),
    )
    analyst = _make_principal(engine, "ANALISTA")
    juristic = _make_principal(engine, "JURIDICO")
    repository = DocumentsRepository(conninfo, security.repository)
    service = DocumentsService(repository, security)
    try:
        yield engine, repository, service, analyst, juristic
    finally:
        engine.dispose()


def _stored_object(engine: sa.Engine) -> UUID:
    object_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/pdf', 10, 'doc.pdf')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-doc.pdf", "sha": bytes(32)},
        )
    return object_id


def _context(actor) -> DocumentOperationContext:
    return DocumentOperationContext(operation_id=uuid4(), correlation_id=uuid4(), actor=actor)


def _create_ready_candidate(repository, engine, document_id: str, *, chunks=1) -> UUID:
    object_id = _stored_object(engine)
    with repository.transaction() as connection:
        version = repository.create_candidate_version(
            connection, id_documento=document_id, stored_object_id=object_id,
            content_sha256=bytes(32), operation_id=uuid4(), correlation_id=uuid4(),
        )
        repository.insert_chunks(connection, version.id, [{"content": f"fragmento {i}", "token_count": 5} for i in range(chunks)])
        repository.set_version_state(connection, version.id, DocumentVersionState.LISTA)
    return version.id


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_candidate_does_not_replace_active_until_ready(database) -> None:
    engine, repository, service, analyst, _ = database
    with repository.transaction() as connection:
        document = repository.create_or_get_document(
            connection, id_documento="DOC-1", name="contrato.pdf",
            document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS",
        )
    assert document.active_version_id is None

    object_id = _stored_object(engine)
    with repository.transaction() as connection:
        candidate = repository.create_candidate_version(
            connection, id_documento="DOC-1", stored_object_id=object_id,
            content_sha256=bytes(32), operation_id=uuid4(), correlation_id=uuid4(),
        )
    with pytest.raises(VersionNotReadyError):
        service.activate_candidate(
            id_documento="DOC-1", candidate_version_id=candidate.id,
            expected_active_version_id=None, context=_context(analyst),
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_activation_replaces_active_and_exposes_corpus(database) -> None:
    engine, repository, service, analyst, _ = database
    with repository.transaction() as connection:
        repository.create_or_get_document(connection, id_documento="DOC-2", name="d", document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS")
    candidate_id = _create_ready_candidate(repository, engine, "DOC-2", chunks=2)
    active = service.activate_candidate(
        id_documento="DOC-2", candidate_version_id=candidate_id,
        expected_active_version_id=None, context=_context(analyst),
    )
    assert active == candidate_id
    with repository.transaction() as connection:
        assert repository.active_chunk_count(connection, "DOC-2") == 2


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_two_candidates_same_active_only_first_succeeds(database) -> None:
    engine, repository, service, analyst, _ = database
    with repository.transaction() as connection:
        repository.create_or_get_document(connection, id_documento="DOC-3", name="d", document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS")
    first = _create_ready_candidate(repository, engine, "DOC-3")
    service.activate_candidate(id_documento="DOC-3", candidate_version_id=first, expected_active_version_id=None, context=_context(analyst))
    second = _create_ready_candidate(repository, engine, "DOC-3")
    with pytest.raises(StaleWriteError):
        service.activate_candidate(id_documento="DOC-3", candidate_version_id=second, expected_active_version_id=None, context=_context(analyst))


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_invalidation_removes_active_and_corpus(database) -> None:
    engine, repository, service, analyst, _ = database
    with repository.transaction() as connection:
        repository.create_or_get_document(connection, id_documento="DOC-4", name="d", document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS")
    candidate = _create_ready_candidate(repository, engine, "DOC-4")
    service.activate_candidate(id_documento="DOC-4", candidate_version_id=candidate, expected_active_version_id=None, context=_context(analyst))
    service.invalidate(id_documento="DOC-4", context=_context(analyst))
    with repository.transaction() as connection:
        document = repository.get_document(connection, "DOC-4")
        assert document.active_version_id is None
        assert document.invalidated is True
        assert repository.active_chunk_count(connection, "DOC-4") == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_failure_rolls_back_activation(database) -> None:
    engine, repository, service, analyst, _ = database
    with repository.transaction() as connection:
        repository.create_or_get_document(connection, id_documento="DOC-5", name="d", document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS")
    candidate = _create_ready_candidate(repository, engine, "DOC-5")

    original = repository.write_audit_event
    repository.write_audit_event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit failure"))
    try:
        with pytest.raises(RuntimeError):
            service.activate_candidate(id_documento="DOC-5", candidate_version_id=candidate, expected_active_version_id=None, context=_context(analyst))
    finally:
        repository.write_audit_event = original

    with repository.transaction() as connection:
        document = repository.get_document(connection, "DOC-5")
        assert document.active_version_id is None


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_juridico_cannot_manage_documents(database) -> None:
    engine, repository, service, analyst, juristic = database
    with repository.transaction() as connection:
        repository.create_or_get_document(connection, id_documento="DOC-J", name="d", document_type="CONTRATO", source_family="CONTRATOS_DOCUMENTOS")
    candidate = _create_ready_candidate(repository, engine, "DOC-J")
    with pytest.raises(Exception):
        service.activate_candidate(
            id_documento="DOC-J", candidate_version_id=candidate,
            expected_active_version_id=None, context=_context(juristic),
        )
    with repository.transaction() as connection:
        assert repository.get_document(connection, "DOC-J").active_version_id is None
