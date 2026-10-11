"""SRS_REQUIRED: reproceso HTTP humano sin reclasificación automática RF-087."""
from uuid import uuid4

import psycopg
import pytest

from tests.integration.test_document_http_postgres import _request, _seed, database  # noqa: F401


pytestmark = [pytest.mark.requires_db, pytest.mark.contract]


def test_real_manual_http_reprocess_uses_server_context_and_no_automatic_ocr_event(database):
    document, source = _seed(database, ocr_state="Rechazado por baja confianza", processing_state="RECHAZADA")
    observed = []
    original_runner = database["service"].runner

    def inspect_context(context, file_id):
        observed.append(context)
        assert context.manual_ocr_reprocess is True
        assert context.actor.account_id == database["principal"].account_id
        return original_runner(context, file_id)

    database["service"].runner = inspect_context
    response, payload = _request(database, document, source)
    assert response.status_code == 200 and response.json()["processing_state"] == "LISTA"
    assert len(observed) == 1
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM audit.event WHERE operation_id=%s AND action='AUTOMATIC_OCR'",
            (observed[0].operation_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT actor_type,actor_user_id FROM audit.event WHERE correlation_id=%s AND action='DOCUMENT_VERSION_ACTIVATED'",
            (observed[0].correlation_id,),
        ).fetchone() == ("HUMAN", database["principal"].account_id)
    repeated = database["client"].post(f"/api/documents/{document}/ocr/reprocess", json=payload)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    assert len(observed) == 1 and database["state"]["ocr_calls"] == 1


@pytest.mark.parametrize("field,value", [
    ("manual_ocr_reprocess", False), ("actor_type", "PROCESS"),
    ("process_identifier", "claimed-by-client"), ("automatic_discovery", True),
])
def test_manual_ocr_http_rejects_client_owned_execution_flags(database, field, value):
    document, source = _seed(database)
    response = database["client"].post(f"/api/documents/{document}/ocr/reprocess", json={
        "source_document_version_id": str(source), "request_id": str(uuid4()), field: value,
    })
    assert response.status_code == 422
    assert database["state"]["runner_calls"] == database["state"]["ocr_calls"] == 0
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM app.document_version WHERE id_documento=%s", (document,)).fetchone()[0] == 1
