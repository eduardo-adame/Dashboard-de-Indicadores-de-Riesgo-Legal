"""Contrato PostgreSQL para la certificación durable del índice documental."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import threading
import time
import uuid

from alembic import command
from alembic.config import Config
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError


BACKEND_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "bge-m3:d1024:c512:o50:lex-nfkc-casefold-alnum-v1"
VECTOR = "[" + ",".join(["0.1"] * 1024) + "]"


def _config(url: str) -> Config:
    parsed = make_url(url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    return config


@pytest.fixture(scope="module")
def database():
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {
        "riesgo_legal", "riesgo_legal_test"
    }:
        pytest.fail("a dedicated index-certification test database is required")
    # La prueba de durabilidad y protección deja evidencia inmutable a propósito.
    # Este módulo usa su propia base desechable para aislar ejecuciones repetidas.
    database_name = f"rag_index_certificate_test_{uuid.uuid4().hex}"
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
        engine = sa.create_engine(isolated_url)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT current_database()")).scalar_one() == database_name
        config = _config(isolated_url)
        command.upgrade(config, "head")
        yield engine, config
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()


@pytest.fixture()
def connection(database):
    engine, _ = database
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _seed(conn, *, source_text="exact source", state="PROCESANDO", chunk_count=1):
    ids = {key: uuid.uuid4() for key in ("object", "version", "chunk")}
    ids["document"] = f"INDEX-{uuid.uuid4()}"
    conn.execute(sa.text(
        "INSERT INTO app.stored_object "
        "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
        "VALUES (:id, 'FILESYSTEM', :locator, :digest, 'text/plain', 12, 'index.txt')"
    ), {"id": ids["object"], "locator": f"test/index/{ids['object']}", "digest": bytes(32)})
    conn.execute(sa.text(
        "INSERT INTO app.document (id_documento, name, document_type, source_family) "
        "VALUES (:id, 'Index fixture', 'Contrato', 'CONTRATOS_DOCUMENTOS')"
    ), {"id": ids["document"]})
    conn.execute(sa.text(
        "INSERT INTO app.document_version "
        "(id, id_documento, version_number, stored_object_id, processing_state, "
        "consolidated_text, content_sha256, operation_id, correlation_id, completed_at) "
        "VALUES (:id, :document, 1, :object, :state, :source, :digest, :operation, "
        ":correlation, CASE WHEN :state IN ('LISTA', 'FALLIDA', 'RECHAZADA') "
        "THEN CURRENT_TIMESTAMP ELSE NULL END)"
    ), {"id": ids["version"], "document": ids["document"], "object": ids["object"],
        "state": state, "source": source_text, "digest": bytes(32),
        "operation": uuid.uuid4(), "correlation": uuid.uuid4()})
    for ordinal in range(chunk_count):
        chunk_id = ids["chunk"] if ordinal == 0 else uuid.uuid4()
        conn.execute(sa.text(
            "INSERT INTO app.document_chunk "
            "(id, document_version_id, ordinal, content, token_count, embedding) "
            "VALUES (:id, :version, :ordinal, 'chunk', 1, CAST(:embedding AS vector))"
        ), {"id": chunk_id, "version": ids["version"], "ordinal": ordinal,
            "embedding": VECTOR})
        if ordinal == 1:
            ids["chunk2"] = chunk_id
    return ids


def _receipt(conn, fragment_id, *, terms=0, contract=CONTRACT):
    conn.execute(sa.text(
        "INSERT INTO app.document_chunk_index_receipt "
        "(fragment_id, index_contract_version, lexical_term_count, processed_at) "
        "VALUES (:fragment, :contract, :terms, CURRENT_TIMESTAMP)"
    ), {"fragment": fragment_id, "contract": contract, "terms": terms})


def _certificate(conn, ids, *, source_text="exact source", **overrides):
    values = {
        "version": ids["version"], "contract": CONTRACT, "model": "BAAI/bge-m3",
        "dimensions": 1024, "size": 512, "overlap": 50, "expected": 1,
        "embedded": 1, "lexical": 1, "zero": 1,
        "digest": hashlib.sha256(source_text.encode("utf-8")).digest(),
    }
    values.update(overrides)
    conn.execute(sa.text(
        "INSERT INTO app.document_index_certificate "
        "(document_version_id, index_contract_version, embedding_model, embedding_dimensions, "
        "chunk_size_tokens, chunk_overlap_tokens, expected_chunk_count, embedded_chunk_count, "
        "lexical_processed_chunk_count, zero_lexeme_chunk_count, source_text_sha256) "
        "VALUES (:version, :contract, :model, :dimensions, :size, :overlap, :expected, "
        ":embedded, :lexical, :zero, :digest)"
    ), values)


def _ready(conn, ids):
    conn.execute(sa.text(
        "UPDATE app.document_version SET processing_state = 'LISTA', "
        "completed_at = CURRENT_TIMESTAMP WHERE id = :id"
    ), {"id": ids["version"]})


def _assert_deferred_failure(conn, constraint):
    with pytest.raises(DBAPIError) as caught:
        conn.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    assert isinstance(caught.value.orig, psycopg.errors.CheckViolation)
    assert caught.value.orig.diag.constraint_name == constraint


def _concurrent_write(engine, started, pid_holder, action):
    with engine.begin() as conn:
        pid_holder.append(conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one())
        started.set()
        action(conn)


def _assert_waits_for(engine, started, pid_holder, blocker_pid):
    assert started.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as observer:
            blockers = observer.execute(
                sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": pid_holder[0]}
            ).scalar_one()
        if blocker_pid in blockers:
            return
        time.sleep(0.05)
    pytest.fail("the concurrent transaction did not wait for the version lock")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_0013_to_0014_upgrade_and_single_head(database):
    engine, config = database
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0015_runtime_privs"]
    assert script.get_revision("0015_runtime_privs").down_revision == "0014_rag_index_certificate"
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == "0015_runtime_privs"
        for table in ("document_index_certificate", "document_chunk_index_receipt"):
            assert conn.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"app.{table}"}).scalar_one() is not None


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_certificate_references_existing_document_version(connection):
    with pytest.raises(DBAPIError) as caught:
        _certificate(connection, {"version": uuid.uuid4()})
    assert isinstance(caught.value.orig, psycopg.errors.ForeignKeyViolation)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_duplicate_certificate_and_new_contract_are_rejected(connection):
    ids = _seed(connection)
    _receipt(connection, ids["chunk"])
    _certificate(connection, ids)
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    with pytest.raises(DBAPIError):
        _certificate(connection, ids)


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize("overrides,constraint", [
    ({"expected": 0, "embedded": 0, "lexical": 0, "zero": 0}, "ck_document_index_certificate_counts"),
    ({"embedded": 0}, "ck_document_index_certificate_counts"),
    ({"lexical": 0}, "ck_document_index_certificate_counts"),
    ({"dimensions": 512}, "ck_document_index_certificate_configuration"),
    ({"digest": b"short"}, "ck_document_index_certificate_source_hash"),
    ({"contract": "different"}, "ck_document_index_certificate_contract"),
    ({"digest": bytes(32)}, "ck_document_index_certificate_source"),
    ({"expected": 2, "embedded": 2, "lexical": 2, "zero": 2}, "ck_document_index_certificate_chunks"),
])
def test_certificate_rejects_invalid_contract_and_incomplete_index(connection, overrides, constraint):
    ids = _seed(connection)
    _receipt(connection, ids["chunk"])
    _ready(connection, ids)
    if constraint.startswith("ck_document_index_certificate_") and constraint not in {
        "ck_document_index_certificate_chunks", "ck_document_index_certificate_source"
    }:
        with pytest.raises(DBAPIError) as caught:
            _certificate(connection, ids, **overrides)
        assert caught.value.orig.diag.constraint_name == constraint
    else:
        _certificate(connection, ids, **overrides)
        _assert_deferred_failure(connection, constraint)


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize("missing,constraint", [
    ("embedding", "ck_document_index_certificate_chunks"),
    ("receipt", "ck_document_index_certificate_lexical"),
    ("terms", "ck_document_index_certificate_lexical"),
    ("receipt_contract", "ck_document_chunk_index_receipt_contract"),
    ("ordinal", "ck_document_index_certificate_chunks"),
])
def test_incomplete_chunk_or_lexical_provenance_is_rejected(connection, missing, constraint):
    ids = _seed(connection)
    if missing == "embedding":
        connection.execute(sa.text("UPDATE app.document_chunk SET embedding = NULL WHERE id = :id"), {"id": ids["chunk"]})
    if missing == "ordinal":
        connection.execute(sa.text("UPDATE app.document_chunk SET ordinal = 1 WHERE id = :id"), {"id": ids["chunk"]})
    if missing == "terms":
        connection.execute(sa.text(
            "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
            "VALUES (:id, 'word', 1)"
        ), {"id": ids["chunk"]})
    if missing == "receipt_contract":
        with pytest.raises(DBAPIError) as caught:
            _receipt(connection, ids["chunk"], contract="different")
        assert caught.value.orig.diag.constraint_name == constraint
        return
    if missing != "receipt":
        _receipt(connection, ids["chunk"])
    _certificate(connection, ids)
    _ready(connection, ids)
    _assert_deferred_failure(connection, constraint)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_zero_lexeme_receipt_and_activation_in_same_transaction(connection):
    ids = _seed(connection)
    _receipt(connection, ids["chunk"], terms=0)
    _certificate(connection, ids)
    _ready(connection, ids)
    connection.execute(sa.text(
        "UPDATE app.document SET active_version_id = :version WHERE id_documento = :document"
    ), {"version": ids["version"], "document": ids["document"]})
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    assert connection.execute(sa.text(
        "SELECT active_version_id FROM app.document WHERE id_documento = :document"
    ), {"document": ids["document"]}).scalar_one() == ids["version"]


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize("mutation", [
    "certificate_update", "certificate_delete", "receipt_update", "receipt_delete",
    "chunk_update", "chunk_delete", "chunk_insert", "embedding", "term_insert",
    "term_update", "term_delete", "source_text", "source_identity",
    "source_document", "source_object", "source_record", "source_native_text", "source_hash",
])
def test_certified_index_is_immutable(connection, mutation):
    ids = _seed(connection)
    has_term = mutation in {"term_update", "term_delete"}
    if has_term:
        connection.execute(sa.text(
            "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
            "VALUES (:id, 'word', 1)"
        ), {"id": ids["chunk"]})
    _receipt(connection, ids["chunk"], terms=int(has_term))
    _certificate(connection, ids, zero=int(not has_term))
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    statements = {
        "certificate_update": ("UPDATE app.document_index_certificate SET certified_at = CURRENT_TIMESTAMP WHERE document_version_id = :id", ids["version"]),
        "certificate_delete": ("DELETE FROM app.document_index_certificate WHERE document_version_id = :id", ids["version"]),
        "receipt_update": ("UPDATE app.document_chunk_index_receipt SET processed_at = CURRENT_TIMESTAMP WHERE fragment_id = :id", ids["chunk"]),
        "receipt_delete": ("DELETE FROM app.document_chunk_index_receipt WHERE fragment_id = :id", ids["chunk"]),
        "chunk_update": ("UPDATE app.document_chunk SET content = 'changed' WHERE id = :id", ids["chunk"]),
        "chunk_delete": ("DELETE FROM app.document_chunk WHERE id = :id", ids["chunk"]),
        "embedding": ("UPDATE app.document_chunk SET embedding = NULL WHERE id = :id", ids["chunk"]),
        "term_insert": ("INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) VALUES (:id, 'new', 1)", ids["chunk"]),
        "term_update": ("UPDATE app.chunk_term SET term_frequency = 2 WHERE fragment_id = :id", ids["chunk"]),
        "term_delete": ("DELETE FROM app.chunk_term WHERE fragment_id = :id", ids["chunk"]),
        "source_text": ("UPDATE app.document_version SET consolidated_text = 'changed' WHERE id = :id", ids["version"]),
        "source_identity": ("UPDATE app.document_version SET version_number = 2 WHERE id = :id", ids["version"]),
        "source_document": ("UPDATE app.document_version SET id_documento = 'other' WHERE id = :id", ids["version"]),
        "source_object": ("UPDATE app.document_version SET stored_object_id = gen_random_uuid() WHERE id = :id", ids["version"]),
        "source_record": ("UPDATE app.document_version SET source_record_id = gen_random_uuid() WHERE id = :id", ids["version"]),
        "source_native_text": ("UPDATE app.document_version SET native_text = 'changed' WHERE id = :id", ids["version"]),
        "source_hash": ("UPDATE app.document_version SET content_sha256 = decode(repeat('aa', 32), 'hex') WHERE id = :id", ids["version"]),
    }
    if mutation == "chunk_insert":
        statement = sa.text(
            "INSERT INTO app.document_chunk (id, document_version_id, ordinal, content, token_count) "
            "VALUES (:id, :version, 1, 'new', 1)"
        )
        params = {"id": uuid.uuid4(), "version": ids["version"]}
    else:
        sql, target = statements[mutation]
        statement, params = sa.text(sql), {"id": target}
    with pytest.raises(DBAPIError) as caught:
        connection.execute(statement, params)
    assert isinstance(caught.value.orig, psycopg.errors.CheckViolation)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_legacy_version_gets_no_automatic_certificate(database):
    engine, config = database
    command.downgrade(config, "0013_proactive_analysis")
    with engine.begin() as conn:
        ids = _seed(conn, state="LISTA")
    command.upgrade(config, "0014_rag_index_certificate")
    with engine.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM app.document_index_certificate WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_runtime_group_has_exact_certificate_and_receipt_privileges(connection):
    for table in ("document_index_certificate", "document_chunk_index_receipt"):
        for privilege, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False)):
            assert connection.execute(sa.text(
                "SELECT has_table_privilege('riesgo_legal_runtime', :table, :privilege)"
            ), {"table": f"app.{table}", "privilege": privilege}).scalar_one() is expected


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_runtime_group_can_insert_receipt_and_certificate(connection):
    ids = _seed(connection)
    connection.execute(sa.text("SET LOCAL ROLE riesgo_legal_runtime"))
    _receipt(connection, ids["chunk"])
    _certificate(connection, ids)
    connection.execute(sa.text("RESET ROLE"))
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_receipt_term_count_matches_real_terms(connection):
    ids = _seed(connection)
    connection.execute(sa.text(
        "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
        "VALUES (:id, 'word', 2)"
    ), {"id": ids["chunk"]})
    _receipt(connection, ids["chunk"], terms=1)
    _certificate(connection, ids, zero=0)
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_duplicate_ordinal_rejected_by_existing_unique_constraint(connection):
    ids = _seed(connection)
    with pytest.raises(DBAPIError) as caught:
        connection.execute(sa.text(
            "INSERT INTO app.document_chunk "
            "(id, document_version_id, ordinal, content, token_count) "
            "VALUES (:id, :version, 0, 'duplicate', 1)"
        ), {"id": uuid.uuid4(), "version": ids["version"]})
    assert caught.value.orig.diag.constraint_name == "uq_document_chunk_ordinal"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_receipt_term_count_mismatch_is_rejected(connection):
    ids = _seed(connection)
    _receipt(connection, ids["chunk"], terms=1)
    _certificate(connection, ids, zero=0)
    _ready(connection, ids)
    _assert_deferred_failure(connection, "ck_document_index_certificate_lexical")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_receipt_cannot_be_added_after_certification(connection):
    ids = _seed(connection, chunk_count=2)
    _receipt(connection, ids["chunk"])
    _receipt(connection, ids["chunk2"])
    _certificate(connection, ids, expected=2, embedded=2, lexical=2, zero=2)
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    with pytest.raises(DBAPIError) as caught:
        _receipt(connection, ids["chunk2"])
    assert isinstance(caught.value.orig, psycopg.errors.CheckViolation)
    assert caught.value.orig.diag.constraint_name == "ck_document_chunk_index_receipt_certified"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_source_hash_is_exact_utf8_without_normalization(connection):
    source = "café\r\n"
    ids = _seed(connection, source_text=source)
    _receipt(connection, ids["chunk"])
    _certificate(connection, ids, source_text=source)
    _ready(connection, ids)
    connection.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
    assert connection.execute(sa.text(
        "SELECT source_text_sha256 FROM app.document_index_certificate "
        "WHERE document_version_id = :id"
    ), {"id": ids["version"]}).scalar_one() == hashlib.sha256(source.encode("utf-8")).digest()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_0014_empty_isolated_downgrade_and_reupgrade(database):
    engine, config = database
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM app.document_index_certificate")).scalar_one() == 0
        assert conn.execute(sa.text("SELECT count(*) FROM app.document_chunk_index_receipt")).scalar_one() == 0
    command.downgrade(config, "0013_proactive_analysis")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == "0013_proactive_analysis"
    command.upgrade(config, "0014_rag_index_certificate")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == "0014_rag_index_certificate"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_certificate_durable_and_evidence_downgrade_guard(database):
    engine, config = database
    with engine.begin() as conn:
        ids = _seed(conn)
        _receipt(conn, ids["chunk"])
        _certificate(conn, ids)
        _ready(conn, ids)
    with engine.connect() as fresh:
        assert fresh.execute(sa.text(
            "SELECT index_contract_version FROM app.document_index_certificate "
            "WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == CONTRACT
    with pytest.raises(RuntimeError, match="index evidence exists"):
        command.downgrade(config, "0013_proactive_analysis")
    with engine.connect() as fresh:
        assert fresh.execute(sa.text(
            "SELECT version_num FROM public.alembic_version"
        )).scalar_one() == "0014_rag_index_certificate"
        assert fresh.execute(sa.text(
            "SELECT count(*) FROM app.document_index_certificate WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_certificate_wins_and_rejects_late_term(database):
    engine, _ = database
    with engine.begin() as setup:
        ids = _seed(setup)
        _receipt(setup, ids["chunk"])
    started = threading.Event()
    pid_holder = []
    with engine.connect() as cert_conn:
        transaction = cert_conn.begin()
        try:
            assert cert_conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "read committed"
            _certificate(cert_conn, ids)
            _ready(cert_conn, ids)
            cert_conn.execute(sa.text("SET CONSTRAINTS trg_document_index_certificate_complete IMMEDIATE"))
            cert_pid = cert_conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
            with ThreadPoolExecutor(max_workers=1) as pool:
                late_term = pool.submit(
                    _concurrent_write, engine, started, pid_holder,
                    lambda conn: conn.execute(sa.text(
                        "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
                        "VALUES (:id, 'late', 1)"
                    ), {"id": ids["chunk"]}),
                )
                _assert_waits_for(engine, started, pid_holder, cert_pid)
                transaction.commit()
                with pytest.raises(DBAPIError) as caught:
                    late_term.result(timeout=10)
                assert caught.value.orig.diag.constraint_name == "ck_certified_chunk_term_immutable"
        finally:
            if transaction.is_active:
                transaction.rollback()
    with engine.connect() as verify:
        assert verify.execute(sa.text(
            "SELECT count(*) FROM app.document_index_certificate WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == 1
        assert verify.execute(sa.text(
            "SELECT count(*) FROM app.chunk_term WHERE fragment_id = :id"
        ), {"id": ids["chunk"]}).scalar_one() == 0
        assert verify.execute(sa.text(
            "SELECT zero_lexeme_chunk_count FROM app.document_index_certificate "
            "WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_term_wins_and_certificate_sees_committed_change(database):
    engine, _ = database
    with engine.begin() as setup:
        ids = _seed(setup)
        _receipt(setup, ids["chunk"])
    started = threading.Event()
    pid_holder = []
    with engine.connect() as term_conn:
        transaction = term_conn.begin()
        try:
            term_conn.execute(sa.text(
                "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
                "VALUES (:id, 'early', 1)"
            ), {"id": ids["chunk"]})
            term_pid = term_conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()

            def certify(conn):
                _certificate(conn, ids)
                _ready(conn, ids)

            with ThreadPoolExecutor(max_workers=1) as pool:
                cert = pool.submit(_concurrent_write, engine, started, pid_holder, certify)
                _assert_waits_for(engine, started, pid_holder, term_pid)
                transaction.commit()
                with pytest.raises(DBAPIError) as caught:
                    cert.result(timeout=10)
                assert caught.value.orig.diag.constraint_name == "ck_document_index_certificate_lexical"
        finally:
            if transaction.is_active:
                transaction.rollback()
    with engine.connect() as verify:
        assert verify.execute(sa.text(
            "SELECT count(*) FROM app.document_index_certificate WHERE document_version_id = :id"
        ), {"id": ids["version"]}).scalar_one() == 0
        assert verify.execute(sa.text(
            "SELECT count(*) FROM app.chunk_term WHERE fragment_id = :id"
        ), {"id": ids["chunk"]}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_different_versions_do_not_share_lock(database):
    engine, _ = database
    with engine.begin() as setup:
        first = _seed(setup)
        second = _seed(setup)
        _receipt(setup, first["chunk"])
        _receipt(setup, second["chunk"])
    with engine.connect() as cert_conn:
        transaction = cert_conn.begin()
        try:
            _certificate(cert_conn, first)
            _ready(cert_conn, first)
            with engine.begin() as other:
                other.execute(sa.text("SET LOCAL lock_timeout = '1000ms'"))
                other.execute(sa.text(
                    "INSERT INTO app.chunk_term (fragment_id, normalized_lexeme, term_frequency) "
                    "VALUES (:id, 'independent', 1)"
                ), {"id": second["chunk"]})
            transaction.commit()
        finally:
            if transaction.is_active:
                transaction.rollback()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_certificate_rejects_late_source_mutation(database):
    engine, _ = database
    with engine.begin() as setup:
        ids = _seed(setup)
        _receipt(setup, ids["chunk"])
    started = threading.Event()
    pid_holder = []
    with engine.connect() as cert_conn:
        transaction = cert_conn.begin()
        try:
            _certificate(cert_conn, ids)
            _ready(cert_conn, ids)
            cert_pid = cert_conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
            with ThreadPoolExecutor(max_workers=1) as pool:
                late_source = pool.submit(
                    _concurrent_write, engine, started, pid_holder,
                    lambda conn: conn.execute(sa.text(
                        "UPDATE app.document_version SET consolidated_text = 'late' WHERE id = :id"
                    ), {"id": ids["version"]}),
                )
                _assert_waits_for(engine, started, pid_holder, cert_pid)
                transaction.commit()
                with pytest.raises(DBAPIError) as caught:
                    late_source.result(timeout=10)
                assert caught.value.orig.diag.constraint_name == "ck_certified_document_source_immutable"
        finally:
            if transaction.is_active:
                transaction.rollback()
    with engine.connect() as verify:
        assert verify.execute(sa.text(
            "SELECT consolidated_text FROM app.document_version WHERE id = :id"
        ), {"id": ids["version"]}).scalar_one() == "exact source"
