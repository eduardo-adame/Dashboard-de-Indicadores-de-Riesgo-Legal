"""SRS_REQUIRED: RF-079/084/087; ROBUSTNESS: entrega concurrente y rollback real."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from app.audit.emission import write_process_event
from app.security.models import AuditPersistenceError
from tests.audit.test_audit_postgres import database  # noqa: F401


pytestmark = [pytest.mark.requires_db, pytest.mark.contract]


def _arguments(**changes):
    values = dict(
        process_identifier="documents.ocr_pipeline", action="AUTOMATIC_OCR",
        resource_type="DOCUMENT_VERSION", resource_identifier=str(uuid4()),
        operation_id=uuid4(), correlation_id=uuid4(), result="Exitoso",
    )
    values.update(changes)
    return values


def _event(database, identity):
    with psycopg.connect(database["reader"], row_factory=dict_row) as connection:
        return connection.execute("SELECT * FROM audit.event WHERE id=%s", (identity,)).fetchone()


def test_process_emission_works_with_real_insert_only_runtime(database):
    parameters = _arguments()
    before = datetime.now(UTC)
    with psycopg.connect(database["runtime"]) as connection:
        assert connection.execute(
            "SELECT has_table_privilege(current_user,'audit.event','INSERT'), "
            "has_table_privilege(current_user,'audit.event','SELECT'), "
            "has_table_privilege(current_user,'audit.event','UPDATE'), "
            "has_table_privilege(current_user,'audit.event','DELETE')"
        ).fetchone() == (True, False, False, False)
        identity = write_process_event(connection, **parameters)
    row = _event(database, identity)
    assert row["id"] == identity and before <= row["occurred_at"] <= datetime.now(UTC)
    assert row["actor_type"] == "PROCESS"
    assert row["actor_identifier"] == "documents.ocr_pipeline"
    assert row["actor_user_id"] is None
    for field in ("action", "resource_type", "resource_identifier", "operation_id", "correlation_id", "result"):
        assert row[field] == parameters[field]
    assert row["safe_cause_code"] is None
    assert row["query_sha256"] is None
    assert "consolidated_text" not in row and "content" not in row and "password" not in row
    with psycopg.connect(database["reader"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event_resource WHERE event_id=%s", (identity,)).fetchone()[0] == 0


def test_repeated_event_keeps_original_timestamp_and_entire_row(database):
    parameters = _arguments()
    with psycopg.connect(database["runtime"]) as connection:
        first = write_process_event(connection, **parameters)
    original = _event(database, first)
    with psycopg.connect(database["runtime"]) as connection:
        repeated = write_process_event(connection, **parameters)
    assert repeated == first and _event(database, first) == original
    with psycopg.connect(database["reader"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE operation_id=%s", (parameters["operation_id"],)).fetchone()[0] == 1


def test_different_result_or_attempt_is_another_logical_event(database):
    parameters = _arguments(result="FAILED", safe_cause_code="OCR_PROCESSING_FAILED")
    with psycopg.connect(database["runtime"]) as connection:
        first = write_process_event(connection, **parameters, attempt_number=1)
        second = write_process_event(connection, **parameters, attempt_number=2)
        success = write_process_event(connection, **(parameters | {"result": "Exitoso", "safe_cause_code": None}))
    assert len({first, second, success}) == 3
    with psycopg.connect(database["reader"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE operation_id=%s", (parameters["operation_id"],)).fetchone()[0] == 3


def test_concurrent_delivery_appends_only_one_process_event(database):
    parameters, barrier = _arguments(), Barrier(2)

    def deliver():
        with psycopg.connect(database["runtime"]) as connection:
            barrier.wait(timeout=5)
            return write_process_event(connection, **parameters)

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(lambda _: deliver(), range(2)))
    assert identities[0] == identities[1]
    with psycopg.connect(database["reader"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE id=%s", (identities[0],)).fetchone()[0] == 1


def test_audit_constraint_failure_rolls_back_callers_mutation(database):
    account, parameters = uuid4(), _arguments(result=None)
    with pytest.raises(AuditPersistenceError, match="no pudo confirmarse"):
        with psycopg.connect(database["runtime"]) as connection:
            connection.execute(
                "INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES(%s,%s,'Sintético','hash')",
                (account, f"process-rollback-{account.hex}"),
            )
            write_process_event(connection, **parameters)
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM app.user_account WHERE id=%s", (account,)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM audit.event WHERE operation_id=%s", (parameters["operation_id"],)).fetchone()[0] == 0
