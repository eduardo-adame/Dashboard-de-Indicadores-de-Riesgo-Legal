"""Permisos reales, autorización vigente y consulta sobre una base de pruebas ya migrada."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import os
import secrets
from threading import Event
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit.models import AuditQuery, AuditUnavailableError
from app.audit.repository import AuditRepository
from app.audit.service import AuditQueryService
from app.security.models import AuthenticatedPrincipal, ROLE_PERMISSIONS, SecurityError
from app.security.repository import SecurityRepository
from app.security.service import SecurityService
from app.security.tokens import JwtService
from scripts.provision_audit_reader import ProvisioningError, validate_reader_privileges


class Database(dict):
    def __repr__(self):
        return "Database(credentials='<protected>')"


@pytest.fixture(scope="module")
def database():
    values = Database(owner=os.getenv("AUDIT_TEST_OWNER_CONNINFO"), runtime=os.getenv("AUDIT_TEST_RUNTIME_CONNINFO"),
                      reader=os.getenv("AUDIT_TEST_READER_CONNINFO"))
    if not all(values.values()):
        pytest.fail("Falta configuración protegida de la base de pruebas de Auditoría", pytrace=False)
    try:
        for connection_string in values.values():
            with psycopg.connect(connection_string) as connection:
                name, current = connection.execute("SELECT current_database(), current_user").fetchone()
                assert "test" in name.lower() and name not in {"riesgo_legal", "riesgo_legal_test"}
                if connection_string == values["reader"]:
                    assert current == "riesgo_legal_audit_app"
        with psycopg.connect(values["owner"]) as connection:
            assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == "0016_rag_operation_lifecycle"
    except psycopg.Error:
        pytest.fail("No se confirmó el acceso PostgreSQL de pruebas", pytrace=False)
    return values


def principal(database, role="ANALISTA"):
    identity, session = uuid4(), uuid4()
    username = f"audit-synthetic-{identity.hex}"
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES(%s,%s,'Sintético','hash')", (identity, username))
        connection.execute("INSERT INTO app.user_role(user_id,role_id) VALUES(%s,%s)", (identity, role))
        connection.execute("INSERT INTO app.access_session(id,user_id,refresh_token_sha256,authorization_version,state,expires_at) VALUES(%s,%s,%s,1,'ACTIVE',%s)", (session, identity, bytes(32), datetime.now(UTC) + timedelta(hours=1)))
    return AuthenticatedPrincipal(identity, session, username, 1, frozenset({role}), ROLE_PERMISSIONS[role])


def event(database, actor, action="QUARANTINE_VIEW", *, occurred_at=None):
    identity, correlation = uuid4(), uuid4()
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("INSERT INTO audit.event(id,occurred_at,actor_type,actor_identifier,actor_user_id,action,resource_type,result,operation_id,correlation_id) VALUES(%s,%s,'HUMAN',%s,%s,%s,'SYNTHETIC','SUCCESS',%s,%s)",
                           (identity, occurred_at or datetime.now(UTC), actor.username, actor.account_id, action, uuid4(), correlation))
        connection.execute("INSERT INTO audit.event_resource(event_id,resource_type,resource_identifier) VALUES(%s,'SYNTHETIC',%s)", (identity, str(identity)))
    return identity


def service(database, repository=None):
    return AuditQueryService(repository or AuditRepository(database["reader"]), SecurityService(SecurityRepository(database["runtime"])))


def test_real_tcp_identities_and_reader_privileges(database):
    with psycopg.connect(database["reader"]) as connection:
        assert connection.execute("SELECT session_user,current_user").fetchone() == ("riesgo_legal_audit_app", "riesgo_legal_audit_app")
        validate_reader_privileges(connection, "riesgo_legal_audit_app")
        assert connection.execute("SELECT pg_has_role(current_user,'riesgo_legal_audit_reader','USAGE')").fetchone()[0]
        assert connection.execute("SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user").fetchone() == (True, True, False, False, False, False)
        assert connection.execute("""SELECT r.rolname,m.admin_option FROM pg_catalog.pg_auth_members m
             JOIN pg_catalog.pg_roles r ON r.oid=m.roleid JOIN pg_catalog.pg_roles u ON u.oid=m.member
             WHERE u.rolname=current_user""").fetchall() == [("riesgo_legal_audit_reader", False)]
        connection.execute("SELECT id FROM audit.event LIMIT 1").fetchall()


@pytest.mark.parametrize("statement", ["INSERT INTO audit.event DEFAULT VALUES", "UPDATE audit.event SET result='FAILURE' WHERE false",
                                       "DELETE FROM audit.event WHERE false", "TRUNCATE audit.event", "CREATE TABLE app.audit_forbidden(id integer)"])
def test_reader_writes_and_persistent_ddl_denied(database, statement):
    with psycopg.connect(database["reader"]) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(statement)


@pytest.mark.parametrize("statement", ["SELECT id FROM audit.event LIMIT 1", "UPDATE audit.event SET result='FAILURE' WHERE false", "DELETE FROM audit.event WHERE false"])
def test_normal_runtime_cannot_read_update_or_delete_audit(database, statement):
    with psycopg.connect(database["runtime"]) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(statement)


def test_runtime_can_append_using_existing_writer(database):
    actor = principal(database)
    with psycopg.connect(database["runtime"]) as connection:
        SecurityRepository.write_audit_event(connection, actor=actor, action="QUARANTINE_VIEW", resource_type="SYNTHETIC", resource_identifier=None, result="SUCCESS", correlation_id=uuid4())


def test_reader_application_transaction_is_read_only(database):
    with AuditRepository(database["reader"]).read_transaction() as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        assert connection.execute("SHOW search_path").fetchone()["search_path"] == "pg_catalog"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with connection.transaction():
                connection.execute("CREATE TEMP TABLE audit_not_part_of_api(id integer)")


def test_analyst_only_own_allowed_events_resources_and_cursor(database):
    actor, other = principal(database), principal(database)
    first, second = event(database, actor), event(database, actor)
    hidden = {event(database, other), event(database, actor, "RAG_QUERY"), event(database, actor, "AUTHORIZATION_DENIED")}
    query = AuditQuery(limit=1)
    page = service(database).list_events(actor, query)
    assert page.items[0].id in {first, second} and page.items[0].id not in hidden
    assert {resource.resource_identifier for resource in page.items[0].resources} == {str(page.items[0].id)}
    assert page.next_cursor
    following = service(database).list_events(actor, query.model_copy(update={"cursor": page.next_cursor}))
    assert {page.items[0].id, following.items[0].id} == {first, second}
    assert following.next_cursor is None


def test_ti_sees_all_categories_without_read_audit_recursion(database):
    actor = principal(database, "TI")
    identity = event(database, actor, "RAG_QUERY")
    with psycopg.connect(database["owner"]) as connection:
        before = connection.execute("SELECT count(*) FROM audit.event").fetchone()[0]
    result = service(database).list_events(actor, AuditQuery(action="RAG_QUERY"))
    assert identity in {item.id for item in result.items}
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event").fetchone()[0] == before


def test_juridico_denied_before_reader_and_one_denial_is_durable(database):
    actor = principal(database, "JURIDICO")
    with pytest.raises(SecurityError):
        service(database, AuditRepository(None)).list_events(actor, AuditQuery())
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE actor_user_id=%s AND action='AUTHORIZATION_DENIED'", (actor.account_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("mutation", ["UPDATE app.access_session SET state='INVALIDATED',invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", "UPDATE app.access_session SET started_at=CURRENT_TIMESTAMP-INTERVAL '2 hours',expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE id=%s"])
def test_revoked_or_expired_session_denied(database, mutation):
    actor = principal(database)
    with psycopg.connect(database["owner"]) as connection:
        connection.execute(mutation, (actor.session_id,))
    with pytest.raises(SecurityError):
        service(database).list_events(actor, AuditQuery())


def test_old_actor_identity_and_180_day_event_preserved(database):
    actor, ti = principal(database), principal(database, "TI")
    identity = event(database, actor, occurred_at=datetime.now(UTC) - timedelta(days=181))
    with psycopg.connect(database["owner"]) as connection:
        connection.execute("UPDATE app.user_account SET state='DISABLED',disabled_at=CURRENT_TIMESTAMP WHERE id=%s", (actor.account_id,))
    page = service(database).list_events(ti, AuditQuery(occurred_to=datetime.now(UTC)-timedelta(days=180)))
    historical = next(item for item in page.items if item.id == identity)
    assert historical.actor_identifier == actor.username and historical.actor_user_id == actor.account_id


def test_audit_failure_blocks_denial_response_without_partial_event(database, monkeypatch):
    actor = principal(database, "JURIDICO")
    instance = service(database)
    def fail(*args, **kwargs):
        raise psycopg.errors.CheckViolation("fallo sintético")
    monkeypatch.setattr(instance.security.repository, "write_audit_event", fail)
    with pytest.raises(AuditUnavailableError):
        instance.list_events(actor, AuditQuery())
    with psycopg.connect(database["owner"]) as connection:
        assert connection.execute("SELECT count(*) FROM audit.event WHERE actor_user_id=%s", (actor.account_id,)).fetchone()[0] == 0


def test_concurrent_revocation_serializes_with_protected_read(database):
    actor = principal(database)
    identity = event(database, actor)
    entered, release = Event(), Event()
    repository = AuditRepository(database["reader"])
    original = repository.list_events
    def blocked(connection, query, own):
        entered.set()
        assert release.wait(5)
        return original(connection, query, own)
    repository.list_events = blocked
    def invalidate():
        with psycopg.connect(database["owner"]) as connection:
            connection.execute("SET LOCAL lock_timeout='5s'")
            connection.execute("UPDATE app.user_account SET authorization_version=authorization_version+1 WHERE id=%s", (actor.account_id,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(service(database, repository).list_events, actor, AuditQuery())
        assert entered.wait(5)
        revocation = pool.submit(invalidate)
        assert not revocation.done()
        release.set()
        assert identity in {item.id for item in reading.result(timeout=6).items}
        revocation.result(timeout=6)
    with pytest.raises(SecurityError):
        service(database).list_events(actor, AuditQuery())


@pytest.mark.parametrize("role,status", [("JURIDICO", 403), ("ANALISTA", 200), ("TI", 200)])
def test_direct_http_uses_real_jwt_and_persisted_authorization(database, role, status):
    from app.audit.api import router
    actor = principal(database, role)
    identity = event(database, actor)
    jwt = JwtService(issuer="synthetic-audit", audience="synthetic-audit", keyring={"synthetic": secrets.token_urlsafe(48)}, active_kid="synthetic")
    security = SecurityService(SecurityRepository(database["runtime"]), jwt)
    application = FastAPI()
    application.include_router(router)
    application.state.security_service = security
    application.state.audit_service = AuditQueryService(AuditRepository(database["reader"]), security)
    token = jwt.issue(account_id=actor.account_id, session_id=actor.session_id, authorization_version=1)
    with TestClient(application) as client:
        response = client.get("/api/audit/events", headers={"Authorization": "Bearer " + token})
        assert response.status_code == status
        if status == 403:
            assert response.json() == {"detail": "Acceso no autorizado"}
            assert str(identity) not in response.text and actor.username not in response.text
        else:
            assert str(identity) in {item["id"] for item in response.json()["items"]}
        with psycopg.connect(database["owner"]) as connection:
            connection.execute("UPDATE app.access_session SET state='INVALIDATED',invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", (actor.session_id,))
        assert client.get("/api/audit/events", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_missing_reader_credentials_is_503_without_fallback(database):
    actor = principal(database)
    with pytest.raises(AuditUnavailableError):
        service(database, AuditRepository(None)).list_events(actor, AuditQuery())


def test_filters_and_stable_tie_pagination_use_persisted_order(database):
    actor = principal(database)
    moment = datetime.now(UTC)
    identities = {event(database, actor, occurred_at=moment) for _ in range(3)}
    query = AuditQuery(limit=1, action="QUARANTINE_VIEW", resource_type="SYNTHETIC", occurred_from=moment, occurred_to=moment)
    observed = []
    while True:
        page = service(database).list_events(actor, query)
        observed.extend(item.id for item in page.items)
        if not page.next_cursor:
            break
        query = query.model_copy(update={"cursor": page.next_cursor})
    assert observed == sorted(identities, reverse=True)


def test_provisioning_validation_rejects_owner_identity(database):
    with psycopg.connect(database["owner"]) as connection:
        owner = connection.execute("SELECT current_user").fetchone()[0]
        with pytest.raises(ProvisioningError):
            validate_reader_privileges(connection, owner)


def test_null_actor_process_and_anonymous_are_not_attributed_to_analyst(database):
    actor, ti = principal(database), principal(database, "TI")
    correlation = uuid4()
    identities = {uuid4(), uuid4()}
    with psycopg.connect(database["owner"]) as connection:
        for identity, kind in zip(identities, ("PROCESS", "ANONYMOUS")):
            connection.execute("INSERT INTO audit.event(id,actor_type,actor_identifier,action,resource_type,result,operation_id,correlation_id) VALUES(%s,%s,%s,'QUARANTINE_VIEW','SYNTHETIC','SUCCESS',%s,%s)", (identity, kind, actor.username, uuid4(), correlation))
    query = AuditQuery(correlation_id=correlation)
    assert service(database).list_events(actor, query).items == []
    assert {item.id for item in service(database).list_events(ti, query).items} == identities
