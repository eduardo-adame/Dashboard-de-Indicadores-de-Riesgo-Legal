"""Rutas representativas de repositorio mediante un LOGIN limitado real de base de datos."""
from __future__ import annotations

from datetime import date
import os
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from sqlalchemy.engine import make_url

from app.analytics.repository import AnalyticsRepository
from app.coordination.repository import CoordinationRepository
from app.documents.repository import DocumentsRepository
from app.ingestion.repository import IngestionRepository
from app.projection.repository import ProjectionRepository
from app.security.repository import SecurityRepository
from app.validation.repository import ValidationRepository
from scripts.provision_runtime_role import provision


@pytest.mark.requires_db
def test_representative_paths_use_genuine_limited_login() -> None:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated Security/runtime test database is required")
    owner = dict(host=parsed.host, port=parsed.port or 5432, dbname=parsed.database,
                 user=parsed.username, password=parsed.password)
    login = f"rl_test_{uuid4().hex}"
    password = secrets.token_urlsafe(32)
    with psycopg.connect(**owner, autocommit=True) as admin:
        try:
            provision(admin, login=login, password=password)
            with psycopg.connect(host=parsed.host, port=parsed.port or 5432,
                                 dbname=parsed.database, user=login, password=password,
                                 row_factory=dict_row) as connection:
                assert connection.execute("SELECT current_user AS name").fetchone()["name"] == login
                # Estas llamadas ejecutan el SQL real del repositorio, no verificaciones
                # de privilegios del owner. Todas las escrituras permanecen en este rollback.
                assert IngestionRepository.next_revision(connection, "runtime-smoke-nonexistent") == 1
                assert ValidationRepository.find_quarantine_by_operation(connection, uuid4()) is None
                assert ProjectionRepository.source_record_provenance(connection, uuid4()) is None
                assert CoordinationRepository.get(connection, uuid4()) is None
                assert SecurityRepository.account_by_id(connection, uuid4()) is None
                assert AnalyticsRepository.contract_rows(connection, date(2035, 1, 1), date(2035, 1, 31)) == []
                assert AnalyticsRepository.ocr_final_rows(connection) == []
                document_id = f"RUNTIME-SMOKE-{uuid4()}"
                created = DocumentsRepository.create_or_get_document(
                    connection, id_documento=document_id, name="Runtime smoke",
                    document_type="Contrato", source_family="CONTRATOS_DOCUMENTOS",
                )
                assert created.id_documento == document_id
                assert DocumentsRepository.next_version_number(connection, document_id) == 1
                connection.rollback()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
