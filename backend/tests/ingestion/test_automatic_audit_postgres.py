"""SRS_REQUIRED: ingesta automática RF-087; ROBUSTNESS: replay, carreras y fallo cerrado."""
from concurrent.futures import ThreadPoolExecutor
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

from app.ingestion.models import IngestionLimits
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestionService
from app.security.models import AuditPersistenceError, AuthorizationError
from app.security.models import AuthenticatedPrincipal, ROLE_PERMISSIONS
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


pytestmark = [pytest.mark.requires_db, pytest.mark.contract]


@pytest.fixture
def database(tmp_path):
    """Conserva evidencia previa y aísla cada caso por cuenta, sesión y procedencia."""
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.fail("Falta la conexión protegida de PostgreSQL de pruebas", pytrace=False)
    parsed = make_url(url)
    assert "test" in (parsed.database or "").lower()
    assert parsed.database not in {"riesgo_legal", "riesgo_legal_test"}
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    backend_root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(backend_root / "alembic.ini")), "head")
    engine = sa.create_engine(url)
    account_id, session_id = uuid4(), uuid4()
    username = f"automatic-ingestion-{account_id.hex}"
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES(:id,:username,'Sintético','hash')"),
            {"id": account_id, "username": username},
        )
        connection.execute(sa.text("INSERT INTO app.user_role(user_id,role_id) VALUES(:id,'ANALISTA')"), {"id": account_id})
        connection.execute(
            sa.text("""INSERT INTO app.access_session
                (id,user_id,refresh_token_sha256,authorization_version,state,expires_at)
                VALUES(:session,:account,:hash,1,'ACTIVE',:expires)"""),
            {"session": session_id, "account": account_id, "hash": bytes(32), "expires": datetime.now(UTC) + timedelta(hours=1)},
        )
    actor = AuthenticatedPrincipal(account_id, session_id, username, 1, frozenset({"ANALISTA"}), ROLE_PERMISSIONS["ANALISTA"])
    conninfo = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    security = SecurityService(SecurityRepository(conninfo))
    limits = IngestionLimits(52_428_800, 2_000, 268_435_456, 67_108_864, 100, 65_536, 1_048_576, 250_000, 256, 5_000_000)
    service = IngestionService(IngestionRepository(conninfo), security, tmp_path / "objects", limits)
    try:
        yield engine, service, actor
    finally:
        # Sólo libera conexiones: no revierte migraciones, roles ni evidencia persistente.
        engine.dispose()


def _controlled_file(tmp_path, name="sample.csv", content=b"id,amount\nL-1,20\n"):
    root = tmp_path / "controlled"
    location = root / "litigation"
    location.mkdir(parents=True, exist_ok=True)
    (location / f"{uuid4().hex}-{name}").write_bytes(content)
    return root


def _events(engine, correlation):
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT * FROM audit.event WHERE correlation_id=:id ORDER BY id"),
            {"id": correlation},
        ).mappings().all()


def _ingestion_rows(engine):
    """Compara identidad y hash de filas sin copiar el contenido a la evidencia."""
    with engine.connect() as connection:
        return {
            table: connection.execute(sa.text(f"SELECT id,md5(row_to_json(t)::text) FROM app.{table} t ORDER BY id")).all()
            for table in ("ingest_file", "source_record", "stored_object")
        }


@pytest.mark.parametrize("name,content,result", [
    ("sample.csv", b"id,amount\nL-1,20\n", "ACCEPTED"),
    ("sample.txt", b"contenido sintetico", "REJECTED"),
    ("sample.pdf", b"%PDF-1.7\ninvalid", "FAILED"),
    ("empty.csv", b"", "REJECTED"),
])
def test_controlled_discovery_adds_process_without_rewriting_human_receipt(database, tmp_path, name, content, result):
    engine, service, actor = database
    correlation = uuid4()
    root = _controlled_file(tmp_path, name, content)
    outcome, = service.run_location(root, "litigation", actor, correlation_id=correlation)
    events = _events(engine, correlation)
    automatic, = [event for event in events if event["action"] == "AUTOMATIC_INGESTION"]
    human, = [event for event in events if event["action"] == "INGESTION_COMPLETED"]
    assert automatic["actor_type"] == "PROCESS"
    assert automatic["actor_identifier"] == "ingestion.controlled_discovery"
    assert automatic["actor_user_id"] is None
    assert automatic["operation_id"] == outcome.operation_id
    assert automatic["correlation_id"] == outcome.correlation_id == correlation
    assert automatic["resource_type"] == "INGEST_FILE"
    assert automatic["resource_identifier"] == str(outcome.file_id)
    assert automatic["result"] == result
    assert automatic["safe_cause_code"] == outcome.safe_cause_code
    assert automatic["occurred_at"] is not None
    assert human["actor_type"] == "HUMAN" and human["actor_user_id"] == actor.account_id
    assert human["result"] == result
    assert automatic["query_sha256"] is None


def test_automatic_resource_limit_rejection_has_minimal_process_event(database, tmp_path):
    engine, service, actor = database
    from dataclasses import replace
    service.limits = replace(service.limits, max_file_bytes=5)
    correlation = uuid4()
    outcome, = service.run_location(_controlled_file(tmp_path), "litigation", actor, correlation_id=correlation)
    automatic, = [event for event in _events(engine, correlation) if event["action"] == "AUTOMATIC_INGESTION"]
    assert outcome.state == "RECHAZADO"
    assert automatic["result"] == "REJECTED"
    assert automatic["safe_cause_code"] == "RESOURCE_LIMIT_EXCEEDED"
    assert automatic["operation_id"] == outcome.operation_id
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT stored_object_id FROM app.ingest_file WHERE id=:id"), {"id": outcome.file_id}).scalar_one() is None


def test_repeated_discovery_preserves_event_and_durable_receipt(database, tmp_path):
    engine, service, actor = database
    root, correlation = _controlled_file(tmp_path), uuid4()
    first, = service.run_location(root, "litigation", actor, correlation_id=correlation)
    original = _events(engine, correlation)
    repeated, = service.run_location(root, "litigation", actor, correlation_id=uuid4())
    assert repeated.idempotent and repeated.file_id == first.file_id
    assert repeated.operation_id == first.operation_id
    assert repeated.correlation_id == correlation
    assert _events(engine, correlation) == original
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM audit.event WHERE action='AUTOMATIC_INGESTION' AND resource_identifier=:id"), {"id": str(first.file_id)}).scalar_one() == 1


@pytest.mark.robustness
def test_concurrent_discovery_has_one_process_event(database, tmp_path):
    engine, service, actor = database
    root, correlation = _controlled_file(tmp_path), uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.run_location(root, "litigation", actor, correlation_id=correlation)[0], range(2)))
    assert results[0].file_id == results[1].file_id
    assert sum(item.idempotent for item in results) == 1
    assert len([event for event in _events(engine, correlation) if event["action"] == "AUTOMATIC_INGESTION"]) == 1


@pytest.mark.parametrize("limited", [False, True])
def test_process_writer_failure_rolls_back_human_event_file_rows_and_object(database, tmp_path, monkeypatch, limited):
    engine, service, actor = database
    if limited:
        from dataclasses import replace
        service.limits = replace(service.limits, max_file_bytes=5)
    correlation = uuid4()
    before = _ingestion_rows(engine)

    def fail(*args, **kwargs):
        raise AuditPersistenceError("Fallo sintético de auditoría de proceso")

    monkeypatch.setattr("app.ingestion.service.write_process_event", fail)
    with pytest.raises(AuditPersistenceError):
        service.run_location(_controlled_file(tmp_path), "litigation", actor, correlation_id=correlation)
    assert _events(engine, correlation) == []
    assert _ingestion_rows(engine) == before
    assert list(service.storage_root.glob("objects/**/*sample.csv")) == []


def test_manual_upload_is_human_even_with_execute_capability_and_controlled_locator(database):
    engine, service, actor = database
    result = service.ingest_stream(
        BytesIO(b"id,amount\nL-1,20\n"), original_name="sample.csv", controlled_location="litigation",
        principal=actor, capability="ingest.execute", source_locator=f"controlled/litigation/{uuid4().hex}-manual.csv",
        idempotency_key=None,
    )
    events = _events(engine, result.correlation_id)
    assert [event["action"] for event in events] == ["INGESTION_COMPLETED"]
    assert events[0]["actor_type"] == "HUMAN"


def test_revoked_execute_permission_denies_before_any_process_event(database, tmp_path):
    engine, service, actor = database
    correlation = uuid4()
    before = _ingestion_rows(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE app.user_role SET active=false,revoked_at=CURRENT_TIMESTAMP WHERE user_id=:id AND role_id='ANALISTA'"),
            {"id": actor.account_id},
        )
    with pytest.raises(AuthorizationError):
        service.run_location(_controlled_file(tmp_path), "litigation", actor, correlation_id=correlation)
    events = _events(engine, correlation)
    assert [event["action"] for event in events] == ["INGESTION_DENIED"]
    assert events[0]["actor_type"] == "HUMAN" and events[0]["result"] == "DENIED"
    assert _ingestion_rows(engine) == before
