"""Evidencia PostgreSQL de la coordinación de despacho downstream."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.coordination.models import DispatchConflictError, DispatchState
from app.coordination.repository import CoordinationRepository
from app.coordination.service import CoordinationService
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
    analytic = _make_principal(engine, "ANALISTA")
    juristic = _make_principal(engine, "JURIDICO")
    repository = CoordinationRepository(conninfo, security.repository)
    try:
        yield engine, repository, security, analytic, juristic
    finally:
        engine.dispose()


def _ingest_file(engine: sa.Engine, *, exchange_format: str, state: str) -> UUID:
    file_id = uuid4()
    object_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/octet-stream', 10, 'f')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-f", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("""INSERT INTO app.ingest_file
                (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                 declared_extension, detected_format, format_classification, technical_result,
                 declared_name, source_locator, source_revision, actor_identifier, content_sha256)
                VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', :fmt, :state, :operation, :correlation,
                        '.bin', :fmt, 'SUPPORTED', 'ACCEPTED', 'f', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "fmt": exchange_format, "state": state, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}/f", "sha": bytes(32)},
        )
    return file_id


class CountingRunner:
    def __init__(self, result_id: UUID | None = None, fail_times: int = 0) -> None:
        self.calls = 0
        self._result = result_id
        self._fail_times = fail_times

    def __call__(self, context, file_id):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("downstream failure")
        return self._result


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_routing_target_reconstructed(database) -> None:
    engine, repository, security, analytic, _ = database
    csv_file = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    pdf_file = _ingest_file(engine, exchange_format="PDF", state="COMPLETADO")
    quarantined = _ingest_file(engine, exchange_format="PDF", state="CUARENTENA")
    with repository.transaction() as connection:
        assert repository.reconstruct_routing_target(connection, csv_file).value == "VALIDATION"
        assert repository.reconstruct_routing_target(connection, pdf_file).value == "DOCUMENT"
        assert repository.reconstruct_routing_target(connection, quarantined).value == "VALIDATION"
        assert repository.reconstruct_routing_target(connection, uuid4()) is None


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_duplicate_dispatch_runs_once(database) -> None:
    engine, repository, security, analytic, _ = database
    file_id = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    runner = CountingRunner()
    service = CoordinationService(repository, security, validation_runner=runner)
    first = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    second = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert first.state == "COMPLETED"
    assert second.state == "COMPLETED"
    assert second.downstream_result_id == first.downstream_result_id
    assert runner.calls == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_duplicate_dispatch_runs_once(database) -> None:
    engine, repository, security, analytic, _ = database
    file_id = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    runner = CountingRunner()
    service = CoordinationService(repository, security, validation_runner=runner)

    def attempt():
        try:
            return service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic).state
        except DispatchConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [f.result() for f in [executor.submit(attempt), executor.submit(attempt)]]
    assert outcomes.count("COMPLETED") >= 1
    assert runner.calls == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_failed_dispatch_can_be_retried(database) -> None:
    engine, repository, security, analytic, _ = database
    file_id = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    runner = CountingRunner(fail_times=1)
    service = CoordinationService(repository, security, validation_runner=runner)
    with pytest.raises(RuntimeError):
        service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    outcome = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert outcome.state == "COMPLETED"
    assert runner.calls == 2


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_stale_in_progress_is_recovered(database) -> None:
    engine, repository, security, analytic, _ = database
    file_id = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    runner = CountingRunner()
    service = CoordinationService(repository, security, validation_runner=runner)
    with repository.transaction() as connection:
        dispatch = repository.get_or_create_dispatch(connection, file_id=file_id, downstream_target="VALIDATION", correlation_id=uuid4())
        repository.claim(connection, dispatch.id)
        connection.execute("UPDATE app.coordination_dispatch SET heartbeat_at = CURRENT_TIMESTAMP - INTERVAL '2 hours' WHERE id = %s", (dispatch.id,))
    recovered = service.recover_stale()
    assert dispatch.id in recovered
    outcome = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert outcome.state == "COMPLETED"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_juridico_without_capability_is_denied(database) -> None:
    engine, repository, security, analytic, juristic = database
    file_id = _ingest_file(engine, exchange_format="CSV", state="COMPLETADO")
    runner = CountingRunner()
    service = CoordinationService(repository, security, validation_runner=runner)
    with pytest.raises(Exception):
        service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=juristic)
    assert runner.calls == 0


def _ingest_csv_with_rows(engine: sa.Engine, rows: list[dict[str, object]]) -> UUID:
    import json

    file_id = uuid4()
    object_id = uuid4()
    headers = ["ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision"]
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 10, 'data.csv')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-data.csv", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("""INSERT INTO app.ingest_file
                (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                 declared_extension, detected_format, format_classification, technical_result,
                 declared_name, source_locator, source_revision, actor_identifier, content_sha256, extraction_metadata)
                VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'CSV', 'COMPLETADO', :operation, :correlation,
                        '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'data.csv', :locator, 1, 'test', :sha, :meta)"""),
            {"id": file_id, "object": object_id, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}/data.csv", "sha": bytes(32), "meta": json.dumps({"headers": headers})},
        )
        for index, row in enumerate(rows):
            connection.execute(
                sa.text("""INSERT INTO app.source_record
                    (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state)
                    VALUES (gen_random_uuid(), :file, :row, 'CSV', CAST(:payload AS jsonb), :sha, 'EXTRAIDO')"""),
                {"file": file_id, "row": index + 2, "payload": json.dumps(row), "sha": bytes(32)},
            )
    return file_id


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_validation_runner_end_to_end(database) -> None:
    from app.coordination.runners import make_validation_runner

    engine, repository, security, analytic, _ = database
    rows = [
        {"ID_Contrato": "C-1", "Fecha_Solicitud": "2026-01-10", "Fecha_Firma": "2026-01-15", "Fecha_Vencimiento": "2026-06-30", "Estado_Revision": "Pendiente"},
        {"ID_Contrato": "C-2", "Fecha_Solicitud": "2026-01-10", "Fecha_Firma": "2026-01-15", "Fecha_Vencimiento": "2026-06-30", "Estado_Revision": "No iniciado"},
    ]
    file_id = _ingest_csv_with_rows(engine, rows)
    validation_runner = make_validation_runner(conninfo=_conninfo(_url()), security=security)
    service = CoordinationService(repository, security, validation_runner=validation_runner)
    outcome = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert outcome.state == "COMPLETED"
    with engine.connect() as connection:
        total = connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()
    assert total == 1
    # Retry: sin duplicar cuarentena.
    service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    with engine.connect() as connection:
        total_after = connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE ingest_file_id = :id"), {"id": file_id}).scalar_one()
    assert total_after == 1


def _ingest_pdf_with_candidate(engine: sa.Engine, *, state: str, page_texts: list[str], exchange_format: str = "PDF") -> UUID:
    file_id = uuid4()
    object_id = uuid4()
    native = "\n\n".join(t for t in page_texts if t)
    extension = "." + exchange_format.lower()
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/octet-stream', 10, 'doc')"),
            {"id": object_id, "locator": f"objects/ab/{object_id}-doc", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("""INSERT INTO app.ingest_file
                (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                 declared_extension, detected_format, format_classification, technical_result,
                 declared_name, source_locator, source_revision, actor_identifier, content_sha256)
                VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', :fmt, 'COMPLETADO', :operation, :correlation,
                        :ext, :fmt, 'SUPPORTED', 'ACCEPTED', 'doc', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "fmt": exchange_format, "ext": extension, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}/doc", "sha": bytes(32)},
        )
        connection.execute(
            sa.text("INSERT INTO app.document_candidate (ingest_file_id, processing_state, native_text) VALUES (:id, :state, :native)"),
            {"id": file_id, "state": state, "native": native},
        )
        for number, text in enumerate(page_texts, start=1):
            connection.execute(
                sa.text("INSERT INTO app.document_candidate_page (id, ingest_file_id, page_number, native_text, requires_ocr) VALUES (gen_random_uuid(), :id, :n, :text, :ocr)"),
                {"id": file_id, "n": number, "text": text, "ocr": text == ""},
            )
    return file_id


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_document_runner_native_pdf(database) -> None:
    from app.coordination.runners import make_document_runner
    from app.corpus.tokenizer import FastTokenizer

    engine, repository, security, analytic, _ = database
    file_id = _ingest_pdf_with_candidate(engine, state="NATIVE_TEXT", page_texts=["cláusula primera del contrato", "cláusula segunda"])
    runner = make_document_runner(conninfo=_conninfo(_url()), security=security, tokenizer_factory=lambda: FastTokenizer())
    service = CoordinationService(repository, security, document_runner=runner)
    outcome = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert outcome.state == "COMPLETED"
    with engine.connect() as connection:
        active = connection.execute(sa.text("SELECT active_version_id FROM app.document WHERE id_documento = :id"), {"id": str(file_id)}).scalar_one()
        chunks = connection.execute(sa.text("SELECT count(*) FROM app.active_document_chunk")).scalar_one()
    assert active is not None
    assert chunks >= 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_document_runner_docx_native(database) -> None:
    from app.coordination.runners import make_document_runner
    from app.corpus.tokenizer import FastTokenizer

    engine, repository, security, analytic, _ = database
    file_id = _ingest_pdf_with_candidate(engine, state="NATIVE_TEXT", page_texts=["contenido docx"], exchange_format="DOCX")
    runner = make_document_runner(conninfo=_conninfo(_url()), security=security, tokenizer_factory=lambda: FastTokenizer())
    service = CoordinationService(repository, security, document_runner=runner)
    outcome = service.dispatch(file_id=file_id, correlation_id=uuid4(), actor=analytic)
    assert outcome.state == "COMPLETED"
    with engine.connect() as connection:
        active = connection.execute(sa.text("SELECT active_version_id FROM app.document WHERE id_documento = :id"), {"id": str(file_id)}).scalar_one()
    assert active is not None
