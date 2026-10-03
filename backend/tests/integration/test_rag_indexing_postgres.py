"""Evidencia PostgreSQL de indexación, certificación y activación RAG."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import hashlib
from io import BytesIO
import os
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import uuid

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.corpus.tokenizer import FastTokenizer
from app.coordination.repository import CoordinationRepository
from app.coordination.runners import make_validation_runner
from app.coordination.service import CoordinationService
from app.documents.models import DocumentProcessingError, StaleWriteError
from app.documents.repository import DocumentsRepository
from app.documents.service import DocumentOperationContext, DocumentsService
from app.ingestion.models import (
    DocumentCandidate,
    DocumentPage,
    IngestionLimits,
    RoutingTarget,
)
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestionService
from app.documents.processing import process_candidate
from app.rag.index_repository import IndexRepository
from app.rag.indexing import (
    INDEX_CONTRACT_VERSION,
    IndexChunk,
    IndexingError,
    IndexingInvariantError,
    IndexingService,
    source_text_sha256,
)
from app.rag.lexical import LexicalTermFrequency
from app.security.models import (
    AuthenticationError,
    AuthorizationError,
    AuthenticatedPrincipal,
    ROLE_PERMISSIONS,
)
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _Encoder:
    def encode_documents(self, texts, normalize_embeddings=False):
        return [[0.01 * ((index % 7) + 1)] * 1024 for index, _ in enumerate(texts)]


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _configure_alembic(url: str) -> Config:
    parsed = make_url(url)
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    return Config(str(BACKEND_ROOT / "alembic.ini"))


def _principal(engine: sa.Engine, role: str) -> AuthenticatedPrincipal:
    account_id, session_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.user_account "
                "(id, username, display_name, password_hash) "
                "VALUES (:id, :username, 'RAG test', 'hash')"
            ),
            {"id": account_id, "username": f"rag-{role.lower()}-{account_id.hex[:8]}"},
        )
        connection.execute(
            sa.text("INSERT INTO app.user_role (user_id, role_id) VALUES (:id, :role)"),
            {"id": account_id, "role": role},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.access_session "
                "(id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
                "VALUES (:session, :user, :digest, 1, 'ACTIVE', :expires)"
            ),
            {
                "session": session_id,
                "user": account_id,
                "digest": bytes(32),
                "expires": datetime.now(UTC) + timedelta(hours=1),
            },
        )
    return AuthenticatedPrincipal(
        account_id,
        session_id,
        role,
        1,
        frozenset({role}),
        ROLE_PERMISSIONS[role],
    )


@pytest.fixture(scope="module")
def database():
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.fail("DATA_TEST_DATABASE_URL es obligatorio para INDEXING")
    parsed = make_url(url)
    maintenance_database = (parsed.database or "").lower()
    if "test" not in maintenance_database and maintenance_database != "postgres":
        pytest.fail(
            "INDEXING requiere una base de pruebas o la base de mantenimiento postgres"
        )

    database_name = f"rag_indexing_test_{uuid.uuid4().hex}"
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
        command.upgrade(_configure_alembic(isolated_url), "head")
        engine = sa.create_engine(isolated_url)
        conninfo = _conninfo(isolated_url)
        key = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        security = SecurityService(
            SecurityRepository(conninfo),
            JwtService(
                issuer="issuer",
                audience="audience",
                keyring={"key": key},
                active_kid="key",
            ),
        )
        yield {
            "engine": engine,
            "conninfo": conninfo,
            "security": security,
            "analyst": _principal(engine, "ANALISTA"),
            "juristic": _principal(engine, "JURIDICO"),
        }
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()


def _seed_version(database, *, text="texto indexable", document_id=None):
    engine = database["engine"]
    document_id = document_id or f"RAG-{uuid.uuid4()}"
    stored_id, version_id = uuid.uuid4(), uuid.uuid4()
    operation_id, correlation_id = uuid.uuid4(), uuid.uuid4()
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (:id, 'FILESYSTEM', :locator, :digest, 'text/plain', 10, 'rag.txt')"
            ),
            {"id": stored_id, "locator": f"rag/{stored_id}", "digest": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.document "
                "(id_documento, name, document_type, source_family) "
                "VALUES (:id, 'RAG', 'CONTRATO', 'CONTRATOS_DOCUMENTOS')"
            ),
            {"id": document_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.document_version "
                "(id, id_documento, version_number, stored_object_id, processing_state, "
                "consolidated_text, content_sha256, operation_id, correlation_id) "
                "VALUES (:id, :document, 1, :stored, 'PROCESANDO', :text, :digest, "
                ":operation, :correlation)"
            ),
            {
                "id": version_id,
                "document": document_id,
                "stored": stored_id,
                "text": text,
                "digest": digest,
                "operation": operation_id,
                "correlation": correlation_id,
            },
        )
    return {
        "document": document_id,
        "version": version_id,
        "stored": stored_id,
        "operation": operation_id,
        "correlation": correlation_id,
        "text": text,
    }


def _index(database, ids):
    repository = IndexRepository(database["conninfo"])
    return IndexingService(repository, _Encoder()).index_version(
        document_version_id=ids["version"],
        consolidated_text=ids["text"],
        tokenizer=FastTokenizer(),
    )


def _activate(database, ids, actor, expected=None):
    repository = DocumentsRepository(
        database["conninfo"], database["security"].repository
    )
    return DocumentsService(repository, database["security"]).activate_candidate(
        id_documento=ids["document"],
        candidate_version_id=ids["version"],
        expected_active_version_id=expected,
        context=DocumentOperationContext(
            ids["operation"], ids["correlation"], actor
        ),
    )


def _seed_ingest_candidate(database, *, declared_name="doc.pdf"):
    file_id, stored_id = uuid.uuid4(), uuid.uuid4()
    correlation_id = uuid.uuid4()
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (:id, 'FILESYSTEM', :locator, :digest, 'application/pdf', 10, :name)"
            ),
            {
                "id": stored_id,
                "locator": f"rag/{stored_id}",
                "digest": bytes(32),
                "name": declared_name,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.ingest_file "
                "(id, stored_object_id, source_family, exchange_format, state, "
                "operation_id, correlation_id, declared_extension, detected_format, "
                "format_classification, technical_result, declared_name, source_locator, "
                "source_revision, actor_identifier, content_sha256) "
                "VALUES (:id, :stored, 'CONTRATOS_DOCUMENTOS', 'PDF', 'COMPLETADO', "
                ":operation, :correlation, '.pdf', 'PDF', 'SUPPORTED', 'ACCEPTED', "
                ":name, :locator, 1, 'test', :digest)"
            ),
            {
                "id": file_id,
                "stored": stored_id,
                "operation": uuid.uuid4(),
                "correlation": correlation_id,
                "locator": f"rag/{file_id}/{declared_name}",
                "digest": bytes(32),
                "name": declared_name,
            },
        )
    candidate = DocumentCandidate(
        pages=(DocumentPage(1, "cláusula contractual indexable", False),),
        native_text="cláusula contractual indexable",
        processing_state="NATIVE_TEXT",
    )
    return file_id, correlation_id, candidate


def _process(
    database,
    *,
    file_id,
    correlation_id,
    candidate,
    operation_id,
    actor=None,
    embedding_service=None,
    tokenizer_factory=None,
):
    return process_candidate(
        conninfo=database["conninfo"],
        security=database["security"],
        file_id=file_id,
        candidate=candidate,
        context=SimpleNamespace(
            actor=actor or database["analyst"],
            operation_id=operation_id,
            correlation_id=correlation_id,
        ),
        tokenizer_factory=tokenizer_factory or FastTokenizer,
        embedding_service=embedding_service or _Encoder(),
    )


def _persisted_index_chunk(database, version_id) -> IndexChunk:
    with database["engine"].connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT id, ordinal, content, token_count, page_start, page_end, "
                "section, clause, embedding::text AS embedding "
                "FROM app.document_chunk WHERE document_version_id=:version"
            ),
            {"version": version_id},
        ).mappings().one()
        terms = connection.execute(
            sa.text(
                "SELECT normalized_lexeme, term_frequency FROM app.chunk_term "
                "WHERE fragment_id=:fragment ORDER BY normalized_lexeme"
            ),
            {"fragment": row["id"]},
        ).mappings().all()
    vector = tuple(float(value) for value in row["embedding"].strip("[]").split(","))
    return IndexChunk(
        fragment_id=row["id"],
        ordinal=row["ordinal"],
        content=row["content"],
        token_count=row["token_count"],
        page_start=row["page_start"],
        page_end=row["page_end"],
        section=row["section"],
        clause=row["clause"],
        embedding=vector,
        terms=tuple(
            LexicalTermFrequency(term["normalized_lexeme"], term["term_frequency"])
            for term in terms
        ),
    )


@pytest.mark.requires_db
def test_chunks_embeddings_terms_receipts_and_certificate_persist(database) -> None:
    ids = _seed_version(database, text="Contrato ÚNICO número 42")
    result = _index(database, ids)
    with database["engine"].connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT vector_dims(c.embedding) AS dims, r.lexical_term_count, "
                "cert.expected_chunk_count, v.processing_state "
                "FROM app.document_chunk c "
                "JOIN app.document_chunk_index_receipt r ON r.fragment_id = c.id "
                "JOIN app.document_index_certificate cert ON cert.document_version_id = c.document_version_id "
                "JOIN app.document_version v ON v.id = c.document_version_id "
                "WHERE c.id = :id"
            ),
            {"id": result.fragment_ids[0]},
        ).mappings().one()
    assert row == {
        "dims": 1024,
        "lexical_term_count": 4,
        "expected_chunk_count": 1,
        "processing_state": "LISTA",
    }


@pytest.mark.requires_db
def test_zero_lexeme_receipt_is_valid(database) -> None:
    ids = _seed_version(database, text="!!! — ___")
    result = _index(database, ids)
    with database["engine"].connect() as connection:
        receipt = connection.execute(
            sa.text(
                "SELECT lexical_term_count FROM app.document_chunk_index_receipt "
                "WHERE fragment_id = :id"
            ),
            {"id": result.fragment_ids[0]},
        ).scalar_one()
        terms = connection.execute(
            sa.text("SELECT count(*) FROM app.chunk_term WHERE fragment_id = :id"),
            {"id": result.fragment_ids[0]},
        ).scalar_one()
    assert receipt == 0
    assert terms == 0


@pytest.mark.requires_db
def test_certified_nonactive_and_legacy_versions_are_invisible(database) -> None:
    certified = _seed_version(database)
    _index(database, certified)
    repository = IndexRepository(database["conninfo"])
    assert repository.active_certified_fragment_count(certified["version"]) == 0

    legacy = _seed_version(database, text="legacy")
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE app.document_version SET processing_state='LISTA', "
                "completed_at=CURRENT_TIMESTAMP WHERE id=:id"
            ),
            {"id": legacy["version"]},
        )
        connection.execute(
            sa.text("UPDATE app.document SET active_version_id=:version WHERE id_documento=:document"),
            {"version": legacy["version"], "document": legacy["document"]},
        )
    assert repository.active_certified_fragment_count(legacy["version"]) == 0


@pytest.mark.requires_db
def test_superseded_ocr_version_is_excluded_from_active_corpus(database) -> None:
    document_id = f"OCR-{uuid.uuid4()}"
    first = _seed_version(database, text="texto OCR anterior", document_id=document_id)
    _index(database, first)
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.ocr_run "
                "(id, document_version_id, attempt_number, estado_ocr, resultado_ocr, "
                "confianza_agregada, total_page_count, ocr_processed_page_count, "
                "granularity, is_final, operation_id, correlation_id) "
                "VALUES (:id, :version, 1, 'Exitoso', 'texto OCR anterior', 0.95, "
                "1, 1, 'WORD', true, :operation, :correlation)"
            ),
            {
                "id": uuid.uuid4(),
                "version": first["version"],
                "operation": uuid.uuid4(),
                "correlation": first["correlation"],
            },
        )
    _activate(database, first, database["analyst"])

    replacement = _seed_next_version(database, document_id, "texto OCR reemplazado")
    _index(database, replacement)
    _activate(database, replacement, database["analyst"], first["version"])

    repository = IndexRepository(database["conninfo"])
    assert repository.active_certified_fragment_count(first["version"]) == 0
    assert repository.active_certified_fragment_count(replacement["version"]) == 1


@pytest.mark.requires_db
@pytest.mark.parametrize("terminal_state", ("RECHAZADA", "FALLIDA"))
def test_rejected_and_failed_versions_are_excluded_from_active_corpus(
    database, terminal_state
) -> None:
    ids = _seed_version(database, text=f"estado terminal {terminal_state}")
    _index(database, ids)
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE app.document SET active_version_id=:version "
                "WHERE id_documento=:document"
            ),
            {"version": ids["version"], "document": ids["document"]},
        )
        connection.execute(
            sa.text(
                "UPDATE app.document_version SET processing_state=:state "
                "WHERE id=:version"
            ),
            {"state": terminal_state, "version": ids["version"]},
        )
    assert (
        IndexRepository(database["conninfo"]).active_certified_fragment_count(
            ids["version"]
        )
        == 0
    )


@pytest.mark.requires_db
def test_physically_quarantined_ingest_file_never_enters_document_corpus(
    database, tmp_path
) -> None:
    source_locator = f"manual/analyst/quarantine-{uuid.uuid4()}"
    ingestion = IngestionService(
        IngestionRepository(database["conninfo"]),
        database["security"],
        tmp_path / "quarantine-objects",
        IngestionLimits(
            52_428_800,
            2_000,
            268_435_456,
            67_108_864,
            100,
            65_536,
            1_048_576,
            250_000,
            256,
            5_000_000,
        ),
    )
    result = ingestion.ingest_stream(
        BytesIO(b"%PDF-1.7\ninvalid"),
        original_name="quarantined-contract.pdf",
        controlled_location="contracts-documents",
        principal=database["analyst"],
        capability="ingest.upload",
        source_locator=source_locator,
        idempotency_key=source_locator,
    )

    assert result.state == "CUARENTENA"
    assert result.routing_target is RoutingTarget.VALIDATION
    assert result.safe_cause_code == "CORRUPT_PDF"
    assert result.document is None

    document_runner_calls: list[uuid.UUID] = []

    def forbidden_document_runner(context, file_id):
        document_runner_calls.append(file_id)
        raise AssertionError("un archivo en cuarentena no debe llegar a Documents")

    coordination = CoordinationService(
        CoordinationRepository(
            database["conninfo"], database["security"].repository
        ),
        database["security"],
        validation_runner=make_validation_runner(
            conninfo=database["conninfo"], security=database["security"]
        ),
        document_runner=forbidden_document_runner,
    )
    outcome = coordination.dispatch(
        file_id=result.file_id,
        correlation_id=result.correlation_id,
        actor=database["analyst"],
    )

    assert outcome.downstream_target == "VALIDATION"
    assert outcome.state == "COMPLETED"
    assert outcome.downstream_result_id is None
    assert document_runner_calls == []

    document_id = str(result.file_id)
    with database["engine"].connect() as connection:
        ingest = connection.execute(
            sa.text(
                "SELECT state, operation_id, correlation_id, stored_object_id "
                "FROM app.ingest_file WHERE id=:file"
            ),
            {"file": result.file_id},
        ).one()
        quarantine = connection.execute(
            sa.text(
                "SELECT state, cause_code, original_object_id, operation_id, "
                "correlation_id FROM app.quarantine_item WHERE ingest_file_id=:file"
            ),
            {"file": result.file_id},
        ).one()
        dispatch = connection.execute(
            sa.text(
                "SELECT downstream_target, state FROM app.coordination_dispatch "
                "WHERE file_id=:file"
            ),
            {"file": result.file_id},
        ).one()
        effects = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM app.document WHERE id_documento=:document) AS documents, "
                "(SELECT count(*) FROM app.document_version WHERE id_documento=:document) AS versions, "
                "(SELECT count(*) FROM app.document_chunk c JOIN app.document_version v "
                "ON v.id=c.document_version_id WHERE v.id_documento=:document) AS chunks, "
                "(SELECT count(*) FROM app.document_chunk c JOIN app.document_version v "
                "ON v.id=c.document_version_id WHERE v.id_documento=:document "
                "AND c.embedding IS NOT NULL) AS embeddings, "
                "(SELECT count(*) FROM app.document_chunk_index_receipt r "
                "JOIN app.document_chunk c ON c.id=r.fragment_id "
                "JOIN app.document_version v ON v.id=c.document_version_id "
                "WHERE v.id_documento=:document) AS receipts, "
                "(SELECT count(*) FROM app.document_index_certificate cert "
                "JOIN app.document_version v ON v.id=cert.document_version_id "
                "WHERE v.id_documento=:document) AS certificates, "
                "(SELECT count(*) FROM app.document WHERE id_documento=:document "
                "AND active_version_id IS NOT NULL) AS activations, "
                "(SELECT count(*) FROM app.active_document_chunk c "
                "JOIN app.document_version v ON v.id=c.document_version_id "
                "JOIN app.document_index_certificate cert "
                "ON cert.document_version_id=v.id "
                "WHERE v.id_documento=:document) AS corpus_fragments"
            ),
            {"document": document_id},
        ).one()

    assert ingest.state == "CUARENTENA"
    assert ingest.operation_id == result.operation_id
    assert ingest.correlation_id == result.correlation_id
    assert quarantine.state == "Pendiente"
    assert quarantine.cause_code == "CORRUPT_PDF"
    assert quarantine.original_object_id == ingest.stored_object_id
    assert isinstance(quarantine.operation_id, uuid.UUID)
    assert quarantine.operation_id != result.operation_id
    assert quarantine.correlation_id == result.correlation_id
    assert dispatch == ("VALIDATION", "COMPLETED")
    assert tuple(effects) == (0, 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.requires_db
def test_authorized_activation_is_atomic_and_audited_once(database) -> None:
    ids = _seed_version(database)
    _index(database, ids)
    assert _activate(database, ids, database["analyst"]) == ids["version"]
    assert IndexRepository(database["conninfo"]).active_certified_fragment_count(
        ids["version"]
    ) == 1
    with database["engine"].connect() as connection:
        total = connection.execute(
            sa.text(
                "SELECT count(*) FROM audit.event "
                "WHERE action='DOCUMENT_VERSION_ACTIVATED' AND resource_identifier=:id"
            ),
            {"id": ids["document"]},
        ).scalar_one()
    assert total == 1


@pytest.mark.requires_db
def test_unauthorized_activation_preserves_active_version(database) -> None:
    document_id = f"AUTH-{uuid.uuid4()}"
    first = _seed_version(database, text="primera", document_id=document_id)
    _index(database, first)
    _activate(database, first, database["analyst"])

    second = _seed_next_version(database, document_id, "segunda")
    _index(database, second)
    with pytest.raises(AuthorizationError):
        _activate(database, second, database["juristic"], first["version"])
    with database["engine"].connect() as connection:
        active = connection.execute(
            sa.text("SELECT active_version_id FROM app.document WHERE id_documento=:id"),
            {"id": document_id},
        ).scalar_one()
    assert active == first["version"]


@pytest.mark.requires_db
def test_embedding_failure_preserves_active_version_without_partial_index(database) -> None:
    document_id = f"FAIL-{uuid.uuid4()}"
    active = _seed_version(database, text="versión vigente", document_id=document_id)
    _index(database, active)
    _activate(database, active, database["analyst"])
    candidate = _seed_next_version(database, document_id, "versión candidata")

    class _InvalidEncoder:
        def encode_documents(self, texts, normalize_embeddings=False):
            return [[1.0, 2.0, 3.0] for _ in texts]

    with pytest.raises(IndexingError):
        IndexingService(
            IndexRepository(database["conninfo"]), _InvalidEncoder()
        ).index_version(
            document_version_id=candidate["version"],
            consolidated_text=candidate["text"],
            tokenizer=FastTokenizer(),
        )

    with database["engine"].connect() as connection:
        current, chunks, certificates = connection.execute(
            sa.text(
                "SELECT d.active_version_id, "
                "(SELECT count(*) FROM app.document_chunk WHERE document_version_id=:candidate), "
                "(SELECT count(*) FROM app.document_index_certificate WHERE document_version_id=:candidate) "
                "FROM app.document d WHERE d.id_documento=:document"
            ),
            {
                "candidate": candidate["version"],
                "document": document_id,
            },
        ).one()
    assert current == active["version"]
    assert chunks == 0
    assert certificates == 0


@pytest.mark.requires_db
def test_partial_persistence_failure_rolls_back_written_chunks(database) -> None:
    ids = _seed_version(database, text="persistencia parcial controlada")
    valid = IndexChunk(
        fragment_id=uuid.uuid4(),
        ordinal=0,
        content="persistencia",
        token_count=1,
        page_start=1,
        page_end=1,
        section=None,
        clause=None,
        embedding=tuple([0.1] * 1024),
        terms=(LexicalTermFrequency("persistencia", 1),),
    )
    invalid = replace(
        valid,
        fragment_id=uuid.uuid4(),
        ordinal=1,
        content="parcial",
        token_count=0,
        terms=(LexicalTermFrequency("parcial", 1),),
    )
    with pytest.raises(Exception):
        IndexRepository(database["conninfo"]).persist_certified_index(
            document_version_id=ids["version"],
            consolidated_text=ids["text"],
            source_text_sha256=source_text_sha256(ids["text"]),
            chunks=(valid, invalid),
        )
    with database["engine"].connect() as connection:
        counts = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM app.document_chunk WHERE document_version_id=:id), "
                "(SELECT count(*) FROM app.document_index_certificate WHERE document_version_id=:id)"
            ),
            {"id": ids["version"]},
        ).one()
    assert counts == (0, 0)


def _seed_next_version(database, document_id: str, text: str):
    engine = database["engine"]
    stored_id, version_id = uuid.uuid4(), uuid.uuid4()
    operation_id, correlation_id = uuid.uuid4(), uuid.uuid4()
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    with engine.begin() as connection:
        version_number = connection.execute(
            sa.text(
                "SELECT max(version_number)+1 FROM app.document_version "
                "WHERE id_documento=:id"
            ),
            {"id": document_id},
        ).scalar_one()
        connection.execute(
            sa.text(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (:id, 'FILESYSTEM', :locator, :digest, 'text/plain', 10, 'rag.txt')"
            ),
            {"id": stored_id, "locator": f"rag/{stored_id}", "digest": digest},
        )
        connection.execute(
            sa.text(
                "INSERT INTO app.document_version "
                "(id, id_documento, version_number, stored_object_id, processing_state, "
                "consolidated_text, content_sha256, operation_id, correlation_id) "
                "VALUES (:id, :document, :number, :stored, 'PROCESANDO', :text, "
                ":digest, :operation, :correlation)"
            ),
            {
                "id": version_id,
                "document": document_id,
                "number": version_number,
                "stored": stored_id,
                "text": text,
                "digest": digest,
                "operation": operation_id,
                "correlation": correlation_id,
            },
        )
    return {
        "document": document_id,
        "version": version_id,
        "stored": stored_id,
        "operation": operation_id,
        "correlation": correlation_id,
        "text": text,
    }


@pytest.mark.requires_db
def test_retry_is_idempotent_and_does_not_duplicate_index(database) -> None:
    ids = _seed_version(database)
    first = _index(database, ids)
    second = _index(database, ids)
    assert first.fragment_ids == second.fragment_ids
    assert second.reused_certificate is True
    with database["engine"].connect() as connection:
        counts = connection.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM app.document_chunk WHERE document_version_id=:id), "
                "(SELECT count(*) FROM app.document_index_certificate WHERE document_version_id=:id)"
            ),
            {"id": ids["version"]},
        ).one()
    assert counts == (1, 1)


@pytest.mark.requires_db
@pytest.mark.parametrize(
    "mutation",
    (
        {"content": "contenido incompatible"},
        {"token_count": 99},
        {"page_start": 2},
        {"page_end": 3},
        {"section": "Sección incompatible"},
        {"clause": "Cláusula incompatible"},
        {"embedding": tuple([0.2] * 1024)},
        {"terms": (LexicalTermFrequency("otro", 2),)},
    ),
    ids=(
        "content",
        "token-boundary",
        "page-start",
        "page-end",
        "section",
        "clause",
        "embedding",
        "lexical-representation",
    ),
)
def test_certified_retry_rejects_each_incompatible_component(database, mutation) -> None:
    ids = _seed_version(database, text="Cláusula Única\ntexto certificado")
    _index(database, ids)
    persisted = _persisted_index_chunk(database, ids["version"])
    with pytest.raises(IndexingInvariantError):
        IndexRepository(database["conninfo"]).persist_certified_index(
            document_version_id=ids["version"],
            consolidated_text=ids["text"],
            source_text_sha256=source_text_sha256(ids["text"]),
            chunks=(replace(persisted, **mutation),),
        )
    assert _persisted_index_chunk(database, ids["version"]) == persisted


@pytest.mark.requires_db
def test_conflicting_retry_fails_without_overwrite(database) -> None:
    ids = _seed_version(database)
    _index(database, ids)
    persisted = _persisted_index_chunk(database, ids["version"])
    with pytest.raises(IndexingInvariantError):
        IndexRepository(database["conninfo"]).persist_certified_index(
            document_version_id=ids["version"],
            consolidated_text=ids["text"],
            source_text_sha256=bytes(32),
            chunks=(persisted,),
        )
    assert _persisted_index_chunk(database, ids["version"]) == persisted


@pytest.mark.requires_db
def test_certified_retry_rejects_incompatible_receipt(database) -> None:
    ids = _seed_version(database, text="receipt incompatible")
    chunk = IndexChunk(
        fragment_id=uuid.uuid4(),
        ordinal=0,
        content=ids["text"],
        token_count=2,
        page_start=1,
        page_end=1,
        section=None,
        clause=None,
        embedding=tuple([0.1] * 1024),
        terms=(
            LexicalTermFrequency("incompatible", 1),
            LexicalTermFrequency("receipt", 1),
        ),
    )
    vector = "[" + ",".join("0.1" for _ in range(1024)) + "]"
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.document_chunk "
                "(id, document_version_id, ordinal, content, token_count, page_start, "
                "page_end, section, clause, embedding) VALUES "
                "(:id, :version, 0, :content, 2, 1, 1, NULL, NULL, CAST(:vector AS vector))"
            ),
            {
                "id": chunk.fragment_id,
                "version": ids["version"],
                "content": chunk.content,
                "vector": vector,
            },
        )
        for term in chunk.terms:
            connection.execute(
                sa.text(
                    "INSERT INTO app.chunk_term "
                    "(fragment_id, normalized_lexeme, term_frequency) "
                    "VALUES (:fragment, :term, :frequency)"
                ),
                {
                    "fragment": chunk.fragment_id,
                    "term": term.normalized_lexeme,
                    "frequency": term.term_frequency,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO app.document_chunk_index_receipt "
                "(fragment_id, index_contract_version, lexical_term_count, processed_at) "
                "VALUES (:fragment, :contract, 3, CURRENT_TIMESTAMP)"
            ),
            {"fragment": chunk.fragment_id, "contract": INDEX_CONTRACT_VERSION},
        )

    with pytest.raises(IndexingInvariantError, match="receipt léxico incompatible"):
        IndexRepository(database["conninfo"]).persist_certified_index(
            document_version_id=ids["version"],
            consolidated_text=ids["text"],
            source_text_sha256=source_text_sha256(ids["text"]),
            chunks=(chunk,),
        )
    with database["engine"].connect() as connection:
        receipt_count, certificate_count = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT lexical_term_count FROM app.document_chunk_index_receipt "
                " WHERE fragment_id=:fragment), "
                "(SELECT count(*) FROM app.document_index_certificate "
                " WHERE document_version_id=:version)"
            ),
            {"fragment": chunk.fragment_id, "version": ids["version"]},
        ).one()
    assert receipt_count == 3
    assert certificate_count == 0


@pytest.mark.requires_db
def test_certified_retry_rejects_incompatible_certificate(database) -> None:
    ids = _seed_version(database, text="certificado incompatible")
    _index(database, ids)
    persisted = _persisted_index_chunk(database, ids["version"])
    unexpected = replace(
        persisted,
        fragment_id=uuid.uuid4(),
        ordinal=1,
        content="fragmento inesperado",
        token_count=2,
        terms=(LexicalTermFrequency("fragmento", 1),),
    )
    with pytest.raises(
        IndexingInvariantError, match="certificado concurrente incompatible"
    ):
        IndexRepository(database["conninfo"]).persist_certified_index(
            document_version_id=ids["version"],
            consolidated_text=ids["text"],
            source_text_sha256=source_text_sha256(ids["text"]),
            chunks=(persisted, unexpected),
        )
    with database["engine"].connect() as connection:
        counts = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM app.document_chunk WHERE document_version_id=:id), "
                "(SELECT expected_chunk_count FROM app.document_index_certificate "
                " WHERE document_version_id=:id)"
            ),
            {"id": ids["version"]},
        ).one()
    assert counts == (1, 1)


@pytest.mark.requires_db
def test_concurrent_same_input_creates_one_certificate(database) -> None:
    ids = _seed_version(database, text="concurrencia documental")

    def execute():
        return _index(database, ids)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=20) for future in (pool.submit(execute), pool.submit(execute))]
    assert results[0].fragment_ids == results[1].fragment_ids
    with database["engine"].connect() as connection:
        total = connection.execute(
            sa.text(
                "SELECT count(*) FROM app.document_index_certificate "
                "WHERE document_version_id=:id"
            ),
            {"id": ids["version"]},
        ).scalar_one()
    assert total == 1


@pytest.mark.requires_db
def test_full_process_concurrent_same_operation_is_idempotent(database) -> None:
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()

    def execute():
        return _process(
            database,
            file_id=file_id,
            correlation_id=correlation_id,
            candidate=candidate,
            operation_id=operation_id,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=30)
            for future in (pool.submit(execute), pool.submit(execute))
        ]
    assert results[0] == results[1]
    with database["engine"].connect() as connection:
        versions, certificates, audits = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM app.document_version WHERE operation_id=:operation), "
                "(SELECT count(*) FROM app.document_index_certificate cert "
                " JOIN app.document_version v ON v.id=cert.document_version_id "
                " WHERE v.operation_id=:operation), "
                "(SELECT count(*) FROM audit.event WHERE action='DOCUMENT_VERSION_ACTIVATED' "
                " AND resource_identifier=:document)"
            ),
            {"operation": operation_id, "document": str(file_id)},
        ).one()
    assert (versions, certificates, audits) == (1, 1, 1)


@pytest.mark.requires_db
def test_full_process_concurrent_distinct_operations_serialize_new_document(database) -> None:
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operations = (uuid.uuid4(), uuid.uuid4())

    def execute(operation_id):
        try:
            return (
                "success",
                _process(
                    database,
                    file_id=file_id,
                    correlation_id=correlation_id,
                    candidate=candidate,
                    operation_id=operation_id,
                ),
            )
        except StaleWriteError:
            return "stale", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=30)
            for future in (
                pool.submit(execute, operations[0]),
                pool.submit(execute, operations[1]),
            )
        ]
    assert {outcome[0] for outcome in outcomes} <= {"success", "stale"}
    assert any(outcome[0] == "success" for outcome in outcomes)
    with database["engine"].connect() as connection:
        documents = connection.execute(
            sa.text("SELECT count(*) FROM app.document WHERE id_documento=:id"),
            {"id": str(file_id)},
        ).scalar_one()
        versions = connection.execute(
            sa.text(
                "SELECT version_number FROM app.document_version "
                "WHERE id_documento=:id ORDER BY version_number"
            ),
            {"id": str(file_id)},
        ).scalars().all()
    assert documents == 1
    assert versions == [1, 2]


@pytest.mark.requires_db
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("name", "otro.pdf"),
        ("document_type", "ANEXO"),
        ("source_family", "LITIGIOS"),
        ("document_date", date(2030, 2, 1)),
    ),
)
def test_document_identity_conflicts_are_rejected_under_lock(
    database, field, replacement
) -> None:
    repository = DocumentsRepository(database["conninfo"], None)
    document_id = f"IDENTITY-{uuid.uuid4()}"
    expected = {
        "name": "contrato.pdf",
        "document_type": "CONTRATO",
        "source_family": "CONTRATOS_DOCUMENTOS",
        "document_date": date(2030, 1, 1),
    }
    with repository.transaction() as connection:
        created = repository.create_or_get_document(
            connection, id_documento=document_id, **expected
        )
    with repository.transaction() as connection:
        reused = repository.create_or_get_document(
            connection, id_documento=document_id, **expected
        )
    assert reused == created

    incompatible = dict(expected)
    incompatible[field] = replacement
    with pytest.raises(DocumentProcessingError, match="identidad documental incompatible"):
        with repository.transaction() as connection:
            repository.create_or_get_document(
                connection, id_documento=document_id, **incompatible
            )
    with database["engine"].connect() as connection:
        persisted = connection.execute(
            sa.text(
                "SELECT name, document_type, source_family, document_date "
                "FROM app.document WHERE id_documento=:id"
            ),
            {"id": document_id},
        ).mappings().one()
    assert dict(persisted) == expected


@pytest.mark.requires_db
def test_concurrent_incompatible_document_identity_creates_one_stable_row(database) -> None:
    repository = DocumentsRepository(database["conninfo"], None)
    document_id = f"IDENTITY-CONCURRENT-{uuid.uuid4()}"
    barrier = Barrier(2)
    candidates = (
        {
            "name": "primero.pdf",
            "document_type": "CONTRATO",
            "source_family": "CONTRATOS_DOCUMENTOS",
            "document_date": date(2030, 1, 1),
        },
        {
            "name": "segundo.pdf",
            "document_type": "ANEXO",
            "source_family": "LITIGIOS",
            "document_date": date(2030, 2, 1),
        },
    )

    def execute(metadata):
        try:
            with repository.transaction() as connection:
                barrier.wait(timeout=20)
                repository.create_or_get_document(
                    connection, id_documento=document_id, **metadata
                )
            return "created"
        except DocumentProcessingError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=30)
            for future in (
                pool.submit(execute, candidates[0]),
                pool.submit(execute, candidates[1]),
            )
        ]
    assert sorted(outcomes) == ["created", "rejected"]
    with database["engine"].connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT name, document_type, source_family, document_date "
                "FROM app.document WHERE id_documento=:id"
            ),
            {"id": document_id},
        ).mappings().one()
        version_count = connection.execute(
            sa.text(
                "SELECT count(*) FROM app.document_version WHERE id_documento=:id"
            ),
            {"id": document_id},
        ).scalar_one()
    assert dict(row) in candidates
    assert version_count == 0


@pytest.mark.requires_db
def test_concurrent_failure_is_retryable_without_partial_artifacts(database) -> None:
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()
    barrier = Barrier(2)

    class _SynchronizedEncoder(_Encoder):
        def encode_documents(self, texts, normalize_embeddings=False):
            barrier.wait(timeout=20)
            return super().encode_documents(texts, normalize_embeddings)

    class _SynchronizedInvalidEncoder:
        def encode_documents(self, texts, normalize_embeddings=False):
            barrier.wait(timeout=20)
            return [[1.0, 2.0, 3.0] for _ in texts]

    def execute(embedding_service):
        try:
            return (
                "success",
                _process(
                    database,
                    file_id=file_id,
                    correlation_id=correlation_id,
                    candidate=candidate,
                    operation_id=operation_id,
                    embedding_service=embedding_service,
                ),
            )
        except IndexingError:
            return "controlled_failure", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=30)
            for future in (
                pool.submit(execute, _SynchronizedEncoder()),
                pool.submit(execute, _SynchronizedInvalidEncoder()),
            )
        ]
    assert {outcome[0] for outcome in outcomes} == {
        "success",
        "controlled_failure",
    }
    successful_version = next(
        outcome[1] for outcome in outcomes if outcome[0] == "success"
    )
    with database["engine"].connect() as connection:
        document_count, version_count, certificate_count, active, audits = (
            connection.execute(
                sa.text(
                    "SELECT "
                    "(SELECT count(*) FROM app.document WHERE id_documento=:document), "
                    "(SELECT count(*) FROM app.document_version WHERE operation_id=:operation), "
                    "(SELECT count(*) FROM app.document_index_certificate cert "
                    " JOIN app.document_version v ON v.id=cert.document_version_id "
                    " WHERE v.operation_id=:operation), "
                    "(SELECT active_version_id FROM app.document WHERE id_documento=:document), "
                    "(SELECT count(*) FROM audit.event "
                    " WHERE action='DOCUMENT_VERSION_ACTIVATED' AND resource_identifier=:document)"
                ),
                {"document": str(file_id), "operation": operation_id},
            ).one()
        )
    assert (document_count, version_count, certificate_count) == (1, 1, 1)
    assert active == successful_version
    assert audits == 1


@pytest.mark.requires_db
def test_stale_candidate_cannot_replace_newer_active_version(database) -> None:
    document_id = f"STALE-{uuid.uuid4()}"
    stale = _seed_version(database, text="candidata antigua", document_id=document_id)
    _index(database, stale)
    newer = _seed_next_version(database, document_id, "candidata nueva")
    _index(database, newer)
    _activate(database, newer, database["analyst"], None)
    with pytest.raises(StaleWriteError):
        _activate(database, stale, database["analyst"], None)


@pytest.mark.requires_db
def test_process_candidate_end_to_end_and_response_retry(database) -> None:
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()
    first = _process(
        database,
        file_id=file_id,
        correlation_id=correlation_id,
        candidate=candidate,
        operation_id=operation_id,
    )
    second = _process(
        database,
        file_id=file_id,
        correlation_id=correlation_id,
        candidate=candidate,
        operation_id=operation_id,
    )
    assert first == second
    with database["engine"].connect() as connection:
        versions, certificates, audits = connection.execute(
            sa.text(
                "SELECT "
                "(SELECT count(*) FROM app.document_version WHERE operation_id=:operation), "
                "(SELECT count(*) FROM app.document_index_certificate cert "
                " JOIN app.document_version v ON v.id=cert.document_version_id "
                " WHERE v.operation_id=:operation), "
                "(SELECT count(*) FROM audit.event WHERE action='DOCUMENT_VERSION_ACTIVATED' "
                " AND resource_identifier=:document)"
            ),
            {"operation": operation_id, "document": str(file_id)},
        ).one()
    assert (versions, certificates, audits) == (1, 1, 1)


@pytest.mark.requires_db
def test_active_retry_revalidates_revoked_permission_without_duplicate_effect(database) -> None:
    actor = _principal(database["engine"], "ANALISTA")
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()
    version_id = _process(
        database,
        file_id=file_id,
        correlation_id=correlation_id,
        candidate=candidate,
        operation_id=operation_id,
        actor=actor,
    )
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text("DELETE FROM app.user_role WHERE user_id=:id"),
            {"id": actor.account_id},
        )
    with pytest.raises(AuthorizationError):
        _process(
            database,
            file_id=file_id,
            correlation_id=correlation_id,
            candidate=candidate,
            operation_id=operation_id,
            actor=actor,
        )
    with database["engine"].connect() as connection:
        active, success_audits = connection.execute(
            sa.text(
                "SELECT d.active_version_id, "
                "(SELECT count(*) FROM audit.event "
                " WHERE action='DOCUMENT_VERSION_ACTIVATED' AND resource_identifier=:document) "
                "FROM app.document d WHERE d.id_documento=:document"
            ),
            {"document": str(file_id)},
        ).one()
    assert active == version_id
    assert success_audits == 1


@pytest.mark.requires_db
def test_revoked_active_retry_with_incompatible_identity_is_denied_before_inference(
    database,
) -> None:
    actor = _principal(database["engine"], "ANALISTA")
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()
    version_id = _process(
        database,
        file_id=file_id,
        correlation_id=correlation_id,
        candidate=candidate,
        operation_id=operation_id,
        actor=actor,
    )
    incompatible_candidate = DocumentCandidate(
        pages=(DocumentPage(1, "contenido certificado incompatible", False),),
        native_text="contenido certificado incompatible",
        processing_state="NATIVE_TEXT",
    )

    class _InferenceMustNotRun:
        def __init__(self):
            self.calls = 0

        def encode_documents(self, texts, normalize_embeddings=False):
            self.calls += 1
            raise AssertionError("BGE no debe ejecutarse antes de denegar")

    encoder = _InferenceMustNotRun()
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text("DELETE FROM app.user_role WHERE user_id=:id"),
            {"id": actor.account_id},
        )
    with pytest.raises(AuthorizationError) as denied:
        _process(
            database,
            file_id=file_id,
            correlation_id=correlation_id,
            candidate=incompatible_candidate,
            operation_id=operation_id,
            actor=actor,
            embedding_service=encoder,
        )
    assert encoder.calls == 0
    assert str(file_id) not in str(denied.value)
    assert str(version_id) not in str(denied.value)
    with database["engine"].connect() as connection:
        active, certificate_count, success_audits = connection.execute(
            sa.text(
                "SELECT d.active_version_id, "
                "(SELECT count(*) FROM app.document_index_certificate "
                " WHERE document_version_id=:version), "
                "(SELECT count(*) FROM audit.event "
                " WHERE action='DOCUMENT_VERSION_ACTIVATED' "
                " AND resource_identifier=:document) "
                "FROM app.document d WHERE d.id_documento=:document"
            ),
            {"document": str(file_id), "version": version_id},
        ).one()
    assert active == version_id
    assert certificate_count == 1
    assert success_audits == 1


@pytest.mark.requires_db
def test_active_retry_revalidates_inactive_session(database) -> None:
    actor = _principal(database["engine"], "ANALISTA")
    file_id, correlation_id, candidate = _seed_ingest_candidate(database)
    operation_id = uuid.uuid4()
    _process(
        database,
        file_id=file_id,
        correlation_id=correlation_id,
        candidate=candidate,
        operation_id=operation_id,
        actor=actor,
    )
    with database["engine"].begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE app.access_session SET state='LOGGED_OUT', "
                "invalidated_at=CURRENT_TIMESTAMP WHERE id=:id"
            ),
            {"id": actor.session_id},
        )
    with pytest.raises(AuthenticationError):
        _process(
            database,
            file_id=file_id,
            correlation_id=correlation_id,
            candidate=candidate,
            operation_id=operation_id,
            actor=actor,
        )


@pytest.mark.requires_db
def test_mandatory_activation_audit_failure_rolls_back_pointer(database, monkeypatch) -> None:
    ids = _seed_version(database, text="auditoría obligatoria")
    _index(database, ids)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("fallo de auditoría simulado")

    monkeypatch.setattr(
        database["security"].repository,
        "write_audit_event",
        fail_audit,
    )
    with pytest.raises(RuntimeError, match="fallo de auditoría"):
        _activate(database, ids, database["analyst"])
    with database["engine"].connect() as connection:
        active = connection.execute(
            sa.text("SELECT active_version_id FROM app.document WHERE id_documento=:id"),
            {"id": ids["document"]},
        ).scalar_one()
    assert active is None


@pytest.mark.requires_db
def test_runtime_group_permissions_are_minimal_for_index_evidence(database) -> None:
    with database["engine"].connect() as connection:
        for table in ("app.document_index_certificate", "app.document_chunk_index_receipt"):
            privileges = {
                privilege: connection.execute(
                    sa.text("SELECT has_table_privilege('riesgo_legal_runtime', :table, :privilege)"),
                    {"table": table, "privilege": privilege},
                ).scalar_one()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            assert privileges == {
                "SELECT": True,
                "INSERT": True,
                "UPDATE": False,
                "DELETE": False,
            }
