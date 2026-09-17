"""Pruebas sin base de datos para la cadena de revisiones y su SQL."""
from __future__ import annotations

from importlib.metadata import version
from pathlib import Path
import subprocess
import sys

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


@pytest.mark.data_schema
def test_runtime_is_python_312_and_pins_are_resolved() -> None:
    assert sys.version_info[:2] == (3, 12)
    assert version("SQLAlchemy") == "2.0.54"
    assert version("alembic") == "1.20.0"
    assert version("pgvector") == "0.5.0"
    assert version("psycopg") == "3.2.3"


@pytest.mark.data_schema
def test_revision_chain_is_linear_and_complete() -> None:
    config = Config(str(ALEMBIC_INI))
    script = ScriptDirectory.from_config(config)
    revisions = list(script.walk_revisions(base="base", head="heads"))
    assert [item.revision for item in reversed(revisions)] == [
        "0001_foundation",
        "0002_ingestion_domain",
        "0003_documents_corpus",
        "0004_analytics_rag_jobs",
        "0005_security_audit_grants",
    ]
    assert len(script.get_heads()) == 1


@pytest.mark.data_schema
def test_offline_upgrade_and_downgrade_compile() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head", "--sql"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    assert "create extension if not exists vector" in sql
    assert "create table app.document_chunk" in sql
    assert "vector(1024)" in sql
    assert "using hnsw" in sql
    assert "create table audit.event" in sql

    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "downgrade", "head:base", "--sql"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert downgrade.returncode == 0, downgrade.stderr


@pytest.mark.data_schema
def test_rag_and_bm25_persistence_excludes_ephemeral_statistics() -> None:
    revisions = (BACKEND_ROOT / "migrations" / "versions").glob("*.py")
    source = "\n".join(path.read_text(encoding="utf-8") for path in revisions)

    assert '"normalized_lexeme"' in source
    assert '"term_frequency"' in source
    assert '"token_count"' in source
    assert '"rag_final_fragment"' in source
    assert '"total_page_count"' in source
    assert '"ocr_processed_page_count"' in source
    assert "unit_type IN ('PAGE', 'BLOCK', 'LINE')" not in source

    forbidden_columns = (
        'sa.Column("document_frequency"',
        'sa.Column("average_document_length"',
        'sa.Column("vector_rank"',
        'sa.Column("bm25_rank"',
        'sa.Column("rrf_score"',
        'sa.Column("final_rank"',
    )
    for forbidden in forbidden_columns:
        assert forbidden not in source
