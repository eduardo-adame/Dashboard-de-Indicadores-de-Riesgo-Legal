"""Evidencia PostgreSQL real de la frontera append-only de auditoría RAG."""
from __future__ import annotations

import os
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
import pytest
from sqlalchemy.engine import make_url

from app.security.models import AuditPersistenceError, AuthenticatedPrincipal
from app.security.repository import SecurityRepository


def _connection() -> psycopg.Connection:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated Security/runtime test database is required")
    return psycopg.connect(
        host=parsed.host, port=parsed.port or 5432, dbname=parsed.database,
        user=parsed.username, password=parsed.password, row_factory=dict_row,
    )


@pytest.fixture()
def connection():
    with _connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            assert cursor.fetchone()["version_num"] == "0015_runtime_privs"
        try:
            yield conn
        finally:
            conn.rollback()


def _seed(
    connection,
    *,
    state="EVIDENCIA_SUFICIENTE",
    final_fragment=False,
    additional_final_fragments=0,
    query_plaintext: str | None = None,
    fragment_content: str = "text",
):
    account_id, session_id, rag_id, operation_id, correlation_id = (uuid4() for _ in range(5))
    query_hash = sha256(query_plaintext.encode("utf-8")).digest() if query_plaintext else bytes(range(32))
    username = f"audit_{account_id.hex}"
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO app.user_account (id, username, display_name, password_hash) "
            "VALUES (%s, %s, 'Audit fixture', 'test-only-hash')",
            (account_id, username),
        )
        cursor.execute(
            "INSERT INTO app.access_session "
            "(id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
            "VALUES (%s, %s, %s, 1, 'ACTIVE', CURRENT_TIMESTAMP + interval '1 hour')",
            (session_id, account_id, bytes(32)),
        )
        cursor.execute(
            "INSERT INTO app.rag_operation "
            "(id, user_id, session_id, query_sha256, state, operation_id, correlation_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (rag_id, account_id, session_id, query_hash, state, operation_id, correlation_id),
        )
    resources: list[tuple[str, object]] = []
    fragment_count = int(final_fragment) + additional_final_fragments
    for index in range(fragment_count):
        stored_id, version_id, fragment_id = (uuid4() for _ in range(3))
        document_id = f"AUDIT-{uuid4()}"
        document_name = f"Audit document {index + 1}"
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app.stored_object "
                "(id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
                "VALUES (%s, 'FILESYSTEM', %s, %s, 'text/plain', 4, 'audit.txt')",
                (stored_id, f"test/audit/{stored_id}", bytes(32)),
            )
            cursor.execute(
                "INSERT INTO app.document (id_documento, name, document_type, source_family) "
                "VALUES (%s, %s, 'Contrato', 'CONTRATOS_DOCUMENTOS')",
                (document_id, document_name),
            )
            cursor.execute(
                "INSERT INTO app.document_version "
                "(id, id_documento, version_number, stored_object_id, processing_state, "
                "consolidated_text, content_sha256, operation_id, correlation_id) "
                "VALUES (%s, %s, 1, %s, 'PROCESANDO', %s, %s, %s, %s)",
                (version_id, document_id, stored_id, fragment_content, bytes(32), uuid4(), uuid4()),
            )
            cursor.execute(
                "INSERT INTO app.document_chunk "
                "(id, document_version_id, ordinal, content, token_count) "
                "VALUES (%s, %s, 0, %s, 1)",
                (fragment_id, version_id, fragment_content),
            )
            cursor.execute(
                "INSERT INTO app.rag_final_fragment "
                "(rag_operation_id, fragment_id, usage, citation_document_name, citation_document_type) "
                "VALUES (%s, %s, 'EVIDENCE', %s, 'Contrato')",
                (rag_id, fragment_id, document_name),
            )
        resources.append((document_id, fragment_id))
    principal = AuthenticatedPrincipal(account_id, session_id, username, 1, frozenset(), frozenset())
    return dict(actor=principal, rag_operation_id=rag_id, operation_id=operation_id,
                correlation_id=correlation_id, query_sha256=query_hash, resources=tuple(resources))


def _write(connection, values, *, action="RAG_QUERY", result="SUCCESS", **overrides):
    return SecurityRepository.write_rag_audit_event(
        connection, **(values | overrides), action=action, result=result, safe_cause_code=None,
    )


@pytest.mark.requires_db
@pytest.mark.parametrize("action,result,state", [
    ("RAG_QUERY", "SUCCESS", "EVIDENCIA_SUFICIENTE"),
    ("AUTHORIZATION_DENIED", "DENIED", "SIN_AUTORIZACION"),
])
def test_rag_audit_actions_persist_exactly(connection, action, result, state):
    values = _seed(connection, state=state)
    event_id = _write(connection, values, action=action, result=result)
    with connection.cursor() as cursor:
        cursor.execute("SELECT action, result, operation_id, query_sha256 FROM audit.event WHERE id = %s", (event_id,))
        row = cursor.fetchone()
        assert row == dict(action=action, result=result, operation_id=values["operation_id"], query_sha256=values["query_sha256"])
        cursor.execute("SELECT count(*) AS n FROM audit.event_resource WHERE event_id = %s", (event_id,))
        assert cursor.fetchone()["n"] == 0


@pytest.mark.requires_db
def test_unknown_action_and_inferred_denial_rejected(connection):
    values = _seed(connection, state="SIN_AUTORIZACION")
    with pytest.raises(AuditPersistenceError):
        _write(connection, values, action="UNLISTED", result="DENIED")
    with pytest.raises(AuditPersistenceError):
        _write(connection, values, action="RAG_QUERY", result="DENIED")


@pytest.mark.requires_db
def test_event_id_stable_and_conflict_fails_closed(connection):
    values = _seed(connection)
    event_id = _write(connection, values)
    assert event_id == uuid5(NAMESPACE_URL, f"riesgo-legal:RAG_QUERY:{str(values['operation_id']).lower()}")
    with pytest.raises(AuditPersistenceError):
        _write(connection, values)


@pytest.mark.requires_db
def test_final_resource_provenance_and_atomic_insert(connection):
    values = _seed(connection, final_fragment=True)
    event_id = _write(connection, values)
    with connection.cursor() as cursor:
        cursor.execute("SELECT resource_type, resource_identifier, id_documento, fragment_id "
                       "FROM audit.event_resource WHERE event_id = %s", (event_id,))
        row = cursor.fetchone()
    assert row == dict(resource_type="DOCUMENT_FRAGMENT",
                       resource_identifier=str(values["resources"][0][1]),
                       id_documento=values["resources"][0][0], fragment_id=values["resources"][0][1])


@pytest.mark.requires_db
def test_mismatched_document_fragment_and_missing_final_set_rejected(connection):
    values = _seed(connection, final_fragment=True)
    with pytest.raises(AuditPersistenceError):
        _write(connection, values, resources=(("WRONG-DOCUMENT", values["resources"][0][1]),))
    with pytest.raises(AuditPersistenceError):
        _write(connection, values, resources=())


@pytest.mark.requires_db
def test_operation_identity_and_hash_mismatch_rejected(connection):
    values = _seed(connection)
    for changed in ({"operation_id": uuid4()}, {"correlation_id": uuid4()}, {"query_sha256": bytes(32)}):
        with pytest.raises(AuditPersistenceError):
            _write(connection, values, **changed)


@pytest.mark.requires_db
def test_writer_neither_commits_nor_rolls_back(connection):
    values = _seed(connection)
    event_id = _write(connection, values)
    with _connection() as other:
        with other.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM audit.event WHERE id = %s", (event_id,))
            assert cursor.fetchone()["n"] == 0
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS n FROM audit.event WHERE id = %s", (event_id,))
        assert cursor.fetchone()["n"] == 1


@pytest.mark.requires_db
def test_rag_audit_multiple_resources_are_atomic(connection):
    values = _seed(connection, final_fragment=True, additional_final_fragments=1)
    event_id = _write(connection, values)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) AS n FROM audit.event WHERE id = %s", (event_id,))
        assert cursor.fetchone()["n"] == 1
        cursor.execute(
            "SELECT id_documento, fragment_id FROM audit.event_resource "
            "WHERE event_id = %s ORDER BY id_documento, fragment_id",
            (event_id,),
        )
        persisted = {(row["id_documento"], row["fragment_id"]) for row in cursor.fetchall()}
    assert persisted == set(values["resources"])
    assert len(persisted) == 2


@pytest.mark.requires_db
def test_rag_audit_event_failure_rolls_back_caller_transaction(connection):
    values = _seed(connection)
    event_id = uuid5(NAMESPACE_URL, f"riesgo-legal:RAG_QUERY:{str(values['operation_id']).lower()}")
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO audit.event "
            "(id, actor_type, actor_identifier, actor_user_id, action, resource_type, "
            "resource_identifier, result, operation_id, correlation_id, query_sha256) "
            "VALUES (%s, 'HUMAN', %s, %s, 'RAG_QUERY', 'RAG_OPERATION', %s, "
            "'SUCCESS', %s, %s, %s)",
            (
                event_id,
                values["actor"].username,
                values["actor"].account_id,
                str(values["rag_operation_id"]),
                uuid4(),
                values["correlation_id"],
                values["query_sha256"],
            ),
        )
    with pytest.raises(AuditPersistenceError):
        _write(connection, values)
    assert connection.info.transaction_status == TransactionStatus.INERROR

    connection.rollback()
    with _connection() as other:
        with other.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM audit.event WHERE id = %s", (event_id,))
            assert cursor.fetchone()["n"] == 0
            cursor.execute("SELECT count(*) AS n FROM audit.event_resource WHERE event_id = %s", (event_id,))
            assert cursor.fetchone()["n"] == 0
            cursor.execute("SELECT count(*) AS n FROM app.rag_operation WHERE id = %s", (values["rag_operation_id"],))
            assert cursor.fetchone()["n"] == 0


@pytest.mark.requires_db
def test_rag_audit_resource_failure_rolls_back_event_and_caller_transaction(connection):
    values = _seed(connection, final_fragment=True, additional_final_fragments=1)
    event_id = uuid5(NAMESPACE_URL, f"riesgo-legal:RAG_QUERY:{str(values['operation_id']).lower()}")
    rejected_fragment = values["resources"][1][1]
    function_name = f"reject_resource_{uuid4().hex}"
    trigger_name = f"reject_resource_{uuid4().hex}"
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "CREATE FUNCTION pg_temp.{}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.fragment_id = {}::uuid THEN "
                "RAISE EXCEPTION 'controlled audit resource failure'; END IF; RETURN NEW; END $$"
            ).format(sql.Identifier(function_name), sql.Literal(str(rejected_fragment)))
        )
        cursor.execute(
            sql.SQL(
                "CREATE TRIGGER {} BEFORE INSERT ON audit.event_resource "
                "FOR EACH ROW EXECUTE FUNCTION pg_temp.{}()"
            ).format(sql.Identifier(trigger_name), sql.Identifier(function_name))
        )

    with pytest.raises(AuditPersistenceError):
        _write(connection, values)
    assert connection.info.transaction_status == TransactionStatus.INERROR

    connection.rollback()
    with _connection() as other:
        with other.cursor() as cursor:
            cursor.execute("SELECT count(*) AS n FROM audit.event WHERE id = %s", (event_id,))
            assert cursor.fetchone()["n"] == 0
            cursor.execute("SELECT count(*) AS n FROM audit.event_resource WHERE event_id = %s", (event_id,))
            assert cursor.fetchone()["n"] == 0
            cursor.execute("SELECT count(*) AS n FROM app.rag_operation WHERE id = %s", (values["rag_operation_id"],))
            assert cursor.fetchone()["n"] == 0


@pytest.mark.requires_db
def test_rag_audit_excludes_sensitive_data(connection):
    sentinels = (
        "SENSITIVE_QUERY_SENTINEL",
        "SENSITIVE_FRAGMENT_SENTINEL",
        "SENSITIVE_DOCUMENT_SENTINEL",
        "SENSITIVE_PROMPT_SENTINEL",
        "SENSITIVE_RESPONSE_SENTINEL",
        "SENSITIVE_JWT_SENTINEL",
        "SENSITIVE_RUNTIME_PASSWORD_SENTINEL",
        "SENSITIVE_MIGRATION_PASSWORD_SENTINEL",
        "SENSITIVE_API_KEY_SENTINEL",
        "SENSITIVE_SECRET_TOKEN_SENTINEL",
    )
    values = _seed(
        connection,
        final_fragment=True,
        query_plaintext=sentinels[0],
        fragment_content=" ".join(sentinels[1:]),
    )
    event_id = _write(connection, values)
    with connection.cursor() as cursor:
        cursor.execute("SELECT row_to_json(e)::text AS payload FROM audit.event e WHERE id = %s", (event_id,))
        event_payload = cursor.fetchone()["payload"]
        cursor.execute(
            "SELECT COALESCE(json_agg(row_to_json(r))::text, '[]') AS payload "
            "FROM audit.event_resource r WHERE event_id = %s",
            (event_id,),
        )
        resource_payload = cursor.fetchone()["payload"]
    persisted_audit_payload = event_payload + resource_payload
    assert values["query_sha256"] == sha256(sentinels[0].encode("utf-8")).digest()
    for sentinel in sentinels:
        assert sentinel not in persisted_audit_payload
