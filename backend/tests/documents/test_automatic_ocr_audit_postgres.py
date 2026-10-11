"""SRS_REQUIRED: RF-022/027/087; ROBUSTNESS: retries, fallos y atomicidad SQL.

El motor OCR y el encoder se aíslan: la propiedad examinada es auditoría,
persistencia y activación reales, no precisión OCR ni calidad semántica del modelo.
"""
from dataclasses import replace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.coordination.service import CoordinationService, DispatchContext
from app.corpus.tokenizer import FastTokenizer
from app.documents.processing import process_candidate
from app.ingestion.models import DocumentCandidate, DocumentPage
from app.security.models import AuditPersistenceError, AuthorizationError
from tests.coordination.test_coordination_postgres import (
    _conninfo, _ingest_pdf_with_candidate, _url, database,  # noqa: F401
)


pytestmark = [pytest.mark.requires_db, pytest.mark.contract]
_TEXT = "Documento sintético de verificación operativa sin información jurídica privada."


class _Encoder:
    def __init__(self):
        self.calls = 0

    def encode_documents(self, texts, normalize_embeddings=False):
        self.calls += 1
        return [[1.0] * 1024 for _ in texts]


def _prepare(database, *, native=False, actor=None):
    engine, _, security, analytic, _ = database
    file_id = _ingest_pdf_with_candidate(
        engine, state="NATIVE_TEXT" if native else "PENDING_OCR",
        page_texts=[_TEXT if native else ""],
    )
    candidate = DocumentCandidate(
        pages=(DocumentPage(1, _TEXT if native else "", not native),),
        native_text=_TEXT if native else "", processing_state="NATIVE_TEXT" if native else "PENDING_OCR",
    )
    context = DispatchContext(uuid4(), uuid4(), actor or analytic)
    return file_id, candidate, context, security


def _run(file_id, candidate, context, security, *, confidence=0.96, pipeline=None, encoder=None):
    return process_candidate(
        conninfo=_conninfo(_url()), security=security, file_id=file_id,
        candidate=candidate, context=context, tokenizer_factory=FastTokenizer,
        ocr_pipeline=pipeline or (lambda *_: {1: (_TEXT, confidence)}),
        embedding_service=encoder or _Encoder(),
    )


def _events(engine, context):
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT * FROM audit.event WHERE operation_id=:id AND action='AUTOMATIC_OCR' ORDER BY id"),
            {"id": context.operation_id},
        ).mappings().all()


def _document_snapshot(engine, file_id, context):
    result = {}
    with engine.connect() as connection:
        result["document"] = connection.execute(sa.text("SELECT to_jsonb(d) FROM app.document d WHERE id_documento=:id"), {"id": str(file_id)}).scalars().all()
        result["version"] = connection.execute(sa.text("SELECT to_jsonb(v) FROM app.document_version v WHERE operation_id=:id ORDER BY id"), {"id": context.operation_id}).scalars().all()
        for table in ("ocr_run", "document_chunk"):
            result[table] = connection.execute(
                sa.text(f"SELECT to_jsonb(r) FROM app.{table} r JOIN app.document_version v ON v.id=r.document_version_id WHERE v.operation_id=:id ORDER BY r.id"),
                {"id": context.operation_id},
            ).scalars().all()
    result["audit"] = _events(engine, context)
    return result


@pytest.mark.parametrize("confidence,expected_state,expected_result", [
    (0.96, "LISTA", "Exitoso"),
    (0.80, "LISTA", "Exitoso"),
    (0.40, "RECHAZADA", "Rechazado por baja confianza"),
    (0.00, "RECHAZADA", "Rechazado por baja confianza"),
    (None, "RECHAZADA", "Pendiente"),
])
def test_automatic_ocr_result_is_minimal_correlated_and_idempotent(database, confidence, expected_state, expected_result):
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)
    encoder = _Encoder()
    outcome = _run(file_id, candidate, context, security, confidence=confidence, encoder=encoder)
    first = _document_snapshot(engine, file_id, context)
    automatic, = first["audit"]
    version, = first["version"]
    ocr, = first["ocr_run"]
    assert version["processing_state"] == expected_state
    assert ocr["estado_ocr"] == expected_result
    assert ocr["confianza_agregada"] == confidence
    assert automatic["actor_type"] == "PROCESS" and automatic["actor_user_id"] is None
    assert automatic["actor_identifier"] == "documents.ocr_pipeline"
    assert automatic["resource_type"] == "DOCUMENT_VERSION"
    assert automatic["resource_identifier"] == version["id"]
    assert automatic["operation_id"] == context.operation_id
    assert automatic["correlation_id"] == context.correlation_id
    assert automatic["result"] == expected_result
    assert automatic["safe_cause_code"] == ("OCR_LOW_CONFIDENCE" if confidence is not None and confidence < 0.80 else None)
    assert automatic["query_sha256"] is None
    assert _TEXT not in str(automatic)
    assert automatic["occurred_at"] is not None
    if expected_state == "LISTA":
        assert str(outcome) == version["id"]
        assert first["document"][0]["active_version_id"] == version["id"]
        assert first["document_chunk"] and encoder.calls == 1
    else:
        assert outcome is None and first["document"][0]["active_version_id"] is None
        assert first["document_chunk"] == [] and encoder.calls == 0
    _run(file_id, candidate, context, security, confidence=confidence, encoder=encoder)
    assert _document_snapshot(engine, file_id, context) == first


def test_native_document_does_not_emit_ocr_process_or_call_pipeline(database):
    engine = database[0]
    file_id, candidate, context, security = _prepare(database, native=True)

    def forbidden(*_args):
        pytest.fail("Un documento nativo no debe invocar OCR")

    assert _run(file_id, candidate, context, security, pipeline=forbidden) is not None
    snapshot = _document_snapshot(engine, file_id, context)
    assert snapshot["audit"] == [] and snapshot["ocr_run"] == []
    assert snapshot["version"][0]["processing_state"] == "LISTA"


def test_server_owned_manual_reprocess_context_excludes_automatic_audit(database):
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)
    context = replace(context, manual_ocr_reprocess=True)
    _run(file_id, candidate, context, security, confidence=0.40)
    snapshot = _document_snapshot(engine, file_id, context)
    assert len(snapshot["ocr_run"]) == 1 and snapshot["audit"] == []


@pytest.mark.parametrize("confidence", [0.96, 0.40])
def test_retry_of_existing_ocr_result_does_not_backfill_process_audit(database, confidence):
    """Un resultado previamente persistido no se convierte en una ejecución nueva."""
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)
    manual_context = replace(context, manual_ocr_reprocess=True)
    first_outcome = _run(file_id, candidate, manual_context, security, confidence=confidence)
    original = _document_snapshot(engine, file_id, context)
    assert len(original["version"]) == len(original["ocr_run"]) == 1
    assert original["audit"] == []
    with engine.connect() as connection:
        original_audit = connection.execute(
            sa.text("SELECT id,md5(row_to_json(e)::text) FROM audit.event e WHERE correlation_id=:id ORDER BY id"),
            {"id": context.correlation_id},
        ).all()
    repeated = _run(file_id, candidate, context, security, confidence=confidence)
    assert context.manual_ocr_reprocess is False
    assert repeated == first_outcome
    assert _document_snapshot(engine, file_id, context) == original
    assert _events(engine, context) == []
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT id,md5(row_to_json(e)::text) FROM audit.event e WHERE correlation_id=:id ORDER BY id"),
            {"id": context.correlation_id},
        ).all() == original_audit


@pytest.mark.robustness
def test_pipeline_failure_records_attempt_identity_without_text_or_result_version(database):
    engine, coordination_repository, security, actor, _ = database
    file_id, candidate, _, _ = _prepare(database)
    seen_contexts = []

    def failure(*_args):
        raise RuntimeError("Contenido sintético que no debe copiarse a la bitácora")

    def runner(context, actual_file):
        seen_contexts.append(context)
        return _run(actual_file, candidate, context, security, pipeline=failure)

    service = CoordinationService(coordination_repository, security, document_runner=runner)
    correlation = uuid4()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            service.dispatch(file_id=file_id, correlation_id=correlation, actor=actor)
    assert [context.attempt_number for context in seen_contexts] == [1, 2]
    assert seen_contexts[0].operation_id == seen_contexts[1].operation_id
    automatic = _events(engine, seen_contexts[0])
    assert len(automatic) == 2 and automatic[0]["id"] != automatic[1]["id"]
    assert all(event["result"] == "FAILED" and event["safe_cause_code"] == "OCR_PROCESSING_FAILED" for event in automatic)
    assert all(event["resource_type"] == "INGEST_FILE" and event["resource_identifier"] == str(file_id) for event in automatic)
    assert all(event["actor_type"] == "PROCESS" and event["correlation_id"] == correlation for event in automatic)
    assert "Contenido sintético" not in str(automatic)
    snapshot = _document_snapshot(engine, file_id, seen_contexts[0])
    assert snapshot["version"] == [] and snapshot["ocr_run"] == []
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT state,attempt_count FROM app.coordination_dispatch WHERE file_id=:id"), {"id": file_id}).one() == ("FAILED", 2)


@pytest.mark.parametrize("confidence", [0.96, 0.40])
def test_mandatory_audit_failure_rolls_back_ocr_version_and_document(database, monkeypatch, confidence):
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)
    encoder = _Encoder()

    def fail(*_args, **_kwargs):
        raise AuditPersistenceError("Fallo sintético de persistencia central")

    monkeypatch.setattr("app.documents.processing.write_process_event", fail)
    with pytest.raises(AuditPersistenceError):
        _run(file_id, candidate, context, security, confidence=confidence, encoder=encoder)
    snapshot = _document_snapshot(engine, file_id, context)
    assert all(value == [] for value in snapshot.values())
    assert encoder.calls == 0


def test_failed_pipeline_cannot_confirm_failure_when_mandatory_audit_unavailable(database, monkeypatch):
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)

    def pipeline(*_args):
        raise RuntimeError("Fallo OCR sintético")

    def audit(*_args, **_kwargs):
        raise AuditPersistenceError("Fallo central sintético")

    monkeypatch.setattr("app.documents.processing.write_process_event", audit)
    with pytest.raises(AuditPersistenceError):
        _run(file_id, candidate, context, security, pipeline=pipeline)
    assert all(value == [] for value in _document_snapshot(engine, file_id, context).values())


def test_current_permission_denies_before_pipeline_and_process_emission(database):
    engine, _, _, _, juridico = database
    file_id, candidate, context, security = _prepare(database, actor=juridico)

    def forbidden(*_args):
        pytest.fail("La autorización debe preceder al contenido y OCR")

    with pytest.raises(AuthorizationError):
        _run(file_id, candidate, context, security, pipeline=forbidden)
    assert all(value == [] for value in _document_snapshot(engine, file_id, context).values())


def test_role_revoked_during_ocr_denies_result_confirmation_without_new_state(database):
    """La autorización posterior al motor debe usar la membresía vigente del actor."""
    engine = database[0]
    file_id, candidate, context, security = _prepare(database)
    before = _document_snapshot(engine, file_id, context)
    calls = []

    def revoke_and_return(*_args):
        calls.append(file_id)
        with engine.begin() as connection:
            connection.execute(
                sa.text("""UPDATE app.user_role SET active=false,revoked_at=CURRENT_TIMESTAMP
                    WHERE user_id=:id AND role_id='ANALISTA'"""),
                {"id": context.actor.account_id},
            )
        return {1: (_TEXT, 0.96)}

    with pytest.raises(AuthorizationError):
        _run(file_id, candidate, context, security, pipeline=revoke_and_return)
    assert calls == [file_id]
    assert _document_snapshot(engine, file_id, context) == before
    assert all(value == [] for value in before.values())
    assert _events(engine, context) == []
