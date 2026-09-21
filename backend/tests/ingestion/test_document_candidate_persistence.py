"""Round-trip durable del candidato documental sobre PostgreSQL desechable."""
from __future__ import annotations

import uuid

from alembic import command
import psycopg
from psycopg.rows import dict_row
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.ingestion.document_candidate_repository import DocumentCandidateRepository
from app.ingestion.models import DocumentCandidate, DocumentPage
from tests.data.test_postgres_migrations import _config, _test_url


def _psycopg_conninfo() -> str:
    parsed = make_url(_test_url())
    return (
        f"host={parsed.host or 'localhost'} port={parsed.port or 5432} "
        f"user={parsed.username or ''} password={parsed.password or ''} "
        f"dbname={parsed.database or ''}"
    )


@pytest.fixture(scope="module")
def candidate_database() -> sa.Engine:
    url = _test_url()
    config = _config(url)
    engine = sa.create_engine(url)
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


def _insert_ingest_file(engine: sa.Engine) -> uuid.UUID:
    file_id = uuid.uuid4()
    object_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (:id, 'FILESYSTEM', :locator, :sha, 'application/pdf', 10, 'doc.pdf')"
            ),
            {"id": object_id, "locator": f"objects/ab/{object_id}-doc.pdf", "sha": bytes(32)},
        )
        connection.execute(
            sa.text(
                """INSERT INTO app.ingest_file
                   (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
                    declared_extension, detected_format, format_classification, technical_result,
                    declared_name, source_locator, source_revision, actor_identifier, content_sha256)
                   VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'PDF', 'COMPLETADO', :operation, :correlation,
                           '.pdf', 'PDF', 'SUPPORTED', 'ACCEPTED', 'doc.pdf', :locator, 1, 'test', :sha)"""
            ),
            {
                "id": file_id,
                "object": object_id,
                "operation": uuid.uuid4(),
                "correlation": uuid.uuid4(),
                "locator": f"test/{file_id}/doc.pdf",
                "sha": bytes(32),
            },
        )
    return file_id


def _candidate() -> DocumentCandidate:
    return DocumentCandidate(
        pages=(
            DocumentPage(1, "texto nativo de la página uno", False),
            DocumentPage(2, "", True),
            DocumentPage(3, "tercera página", False),
        ),
        native_text="texto nativo de la página uno\n\ntercera página",
        processing_state="PENDING_OCR",
    )


def _persist_and_load(engine: sa.Engine, file_id: uuid.UUID, candidate: DocumentCandidate):
    repository = DocumentCandidateRepository("")
    with psycopg.connect(_psycopg_conninfo(), row_factory=dict_row) as connection:
        with connection.transaction():
            repository.persist(connection, file_id=file_id, candidate=candidate)
    with psycopg.connect(_psycopg_conninfo(), row_factory=dict_row) as connection:
        return repository.load(connection, file_id)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_document_candidate_round_trip_is_page_faithful(candidate_database: sa.Engine) -> None:
    file_id = _insert_ingest_file(candidate_database)
    candidate = _candidate()
    reloaded = _persist_and_load(candidate_database, file_id, candidate)

    assert reloaded is not None
    assert reloaded.processing_state == candidate.processing_state
    assert reloaded.native_text == candidate.native_text
    assert [p.page_number for p in reloaded.pages] == [1, 2, 3]
    assert [p.native_text for p in reloaded.pages] == [p.native_text for p in candidate.pages]
    assert [p.requires_ocr for p in reloaded.pages] == [p.requires_ocr for p in candidate.pages]


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_document_candidate_persist_is_idempotent(candidate_database: sa.Engine) -> None:
    file_id = _insert_ingest_file(candidate_database)
    candidate = _candidate()
    _persist_and_load(candidate_database, file_id, candidate)
    _persist_and_load(candidate_database, file_id, candidate)
    with candidate_database.connect() as connection:
        page_count = connection.execute(
            sa.text("SELECT count(*) FROM app.document_candidate_page WHERE ingest_file_id = :id"),
            {"id": file_id},
        ).scalar_one()
    assert page_count == 3


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_native_only_candidate_round_trip(candidate_database: sa.Engine) -> None:
    file_id = _insert_ingest_file(candidate_database)
    candidate = DocumentCandidate(
        pages=(DocumentPage(1, "primera", False), DocumentPage(2, "segunda", False)),
        native_text="primera\n\nsegunda",
        processing_state="NATIVE_TEXT",
    )
    reloaded = _persist_and_load(candidate_database, file_id, candidate)
    assert reloaded.processing_state == "NATIVE_TEXT"
    assert [p.native_text for p in reloaded.pages] == ["primera", "segunda"]
    assert all(p.requires_ocr is False for p in reloaded.pages)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_scanned_only_candidate_preserves_pending_ocr(candidate_database: sa.Engine) -> None:
    file_id = _insert_ingest_file(candidate_database)
    candidate = DocumentCandidate(
        pages=(DocumentPage(1, "", True), DocumentPage(2, "", True), DocumentPage(3, "", True)),
        native_text="",
        processing_state="PENDING_OCR",
    )
    reloaded = _persist_and_load(candidate_database, file_id, candidate)
    assert reloaded.processing_state == "PENDING_OCR"
    assert [p.page_number for p in reloaded.pages] == [1, 2, 3]
    assert all(p.requires_ocr is True for p in reloaded.pages)
