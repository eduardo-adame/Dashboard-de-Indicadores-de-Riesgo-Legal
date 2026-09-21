"""Persistencia durable del candidato documental a través del servicio de ingesta."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from docx import Document
from pypdf import PdfWriter

from app.ingestion.document_candidate_repository import DocumentCandidateRepository
from app.ingestion.models import IngestionLimits
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestionService
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
    account_id, session_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :u, 'Analyst', 'hash')"), {"id": account_id, "u": f"analyst-{account_id.hex[:6]}"})
        connection.execute(sa.text("INSERT INTO app.user_role (user_id, role_id) VALUES (:id, 'ANALISTA')"), {"id": account_id})
        connection.execute(
            sa.text("""INSERT INTO app.access_session
                (id, user_id, refresh_token_sha256, authorization_version, state, expires_at)
                VALUES (:session, :user, :hash, 1, 'ACTIVE', :expires)"""),
            {"session": session_id, "user": account_id, "hash": bytes(32), "expires": datetime.now(UTC) + timedelta(hours=1)},
        )
    principal = AuthenticatedPrincipal(account_id, session_id, "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.upload", "ingest.execute"}))
    conninfo = _conninfo(url)
    encoded_secret = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    security = SecurityService(
        SecurityRepository(conninfo),
        JwtService(issuer="issuer", audience="audience", keyring={"key": encoded_secret}, active_kid="key"),
    )
    limits = IngestionLimits(52_428_800, 2_000, 268_435_456, 67_108_864, 100, 65_536, 1_048_576, 250_000, 256, 5_000_000)
    service = IngestionService(IngestionRepository(conninfo), security, tmp_path / "objects", limits)
    try:
        yield engine, service, principal
    finally:
        engine.dispose()


def _docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _ingest(service, principal, content: bytes, name: str, location: str = "contracts-documents"):
    return service.ingest_stream(
        BytesIO(content), original_name=name, controlled_location=location,
        principal=principal, capability="ingest.upload",
        source_locator=f"manual/{principal.account_id}/{name}", idempotency_key=name,
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_docx_candidate_is_persisted_and_page_faithful(database) -> None:
    engine, service, principal = database
    result = _ingest(service, principal, _docx_bytes("cláusula primera"), "contrato.docx")
    assert result.state == "COMPLETADO"

    repository = DocumentCandidateRepository("")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_conninfo(_url()), row_factory=dict_row) as connection:
        candidate = repository.load(connection, result.file_id)

    assert candidate is not None
    assert candidate.processing_state == "NATIVE_TEXT"
    assert "cláusula primera" in candidate.native_text
    assert len(candidate.pages) == 1
    assert candidate.pages[0].requires_ocr is False


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_scanned_pdf_candidate_preserves_pending_ocr(database) -> None:
    engine, service, principal = database
    result = _ingest(service, principal, _blank_pdf_bytes(), "escaneado.pdf")
    assert result.state == "COMPLETADO"

    repository = DocumentCandidateRepository("")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(_conninfo(_url()), row_factory=dict_row) as connection:
        candidate = repository.load(connection, result.file_id)

    assert candidate is not None
    assert candidate.processing_state == "PENDING_OCR"
    assert candidate.pages[0].requires_ocr is True
