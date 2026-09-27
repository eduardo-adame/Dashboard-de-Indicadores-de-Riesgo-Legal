from contextlib import contextmanager
from uuid import uuid4

import pytest

from app.analytics.models import (
    ProactiveAnalysisContext,
    ProactiveAnalysisError,
    ProactiveAnalysisQuery,
    ProactiveQueryContext,
)
from app.analytics.service import ProactiveAnalysisQueryService, ProactiveAnalysisService
from app.security.models import AuthenticatedPrincipal


def _job():
    return {
        "id": uuid4(),
        "case_type": "PROACTIVE_ANALYSIS",
        "state": "STARTED",
        "operation_id": uuid4(),
        "correlation_id": uuid4(),
        "actor_process": "controlled_ingestion.proactive_analysis",
    }


def _context(job):
    return ProactiveAnalysisContext(job["id"], job["operation_id"], job["correlation_id"], job["actor_process"])


def test_context_operation_must_match_persisted_job() -> None:
    job = _job()
    with pytest.raises(ProactiveAnalysisError) as error:
        ProactiveAnalysisService._validate_job(job, ProactiveAnalysisContext(job["id"], uuid4(), job["correlation_id"], job["actor_process"]))
    assert error.value.safe_cause_code == "JOB_OPERATION_MISMATCH"


def test_context_correlation_must_match_persisted_job() -> None:
    job = _job()
    with pytest.raises(ProactiveAnalysisError) as error:
        ProactiveAnalysisService._validate_job(job, ProactiveAnalysisContext(job["id"], job["operation_id"], uuid4(), job["actor_process"]))
    assert error.value.safe_cause_code == "JOB_CORRELATION_MISMATCH"


def test_context_process_identifier_must_match_persisted_job() -> None:
    job = _job()
    with pytest.raises(ProactiveAnalysisError) as error:
        ProactiveAnalysisService._validate_job(job, ProactiveAnalysisContext(job["id"], job["operation_id"], job["correlation_id"], "caller-process"))
    assert error.value.safe_cause_code == "JOB_PROCESS_MISMATCH"


class _NoInputRepository:
    def __init__(self, job):
        self.job = job

    @contextmanager
    def transaction(self):
        yield object()

    def proactive_job(self, connection, job_run_id):
        return self.job

    def proactive_run_for_job(self, connection, job_run_id, *, for_update=False):
        return None

    def eligible_proactive_observations(self, connection):
        return ()


class _AuditSpy:
    def __init__(self):
        self.calls = []

    def write_audit_event(self, connection, **kwargs):
        self.calls.append(kwargs)


def test_first_execution_with_no_eligible_observations_is_no_relevant_work() -> None:
    job, audit = _job(), _AuditSpy()
    result = ProactiveAnalysisService(_NoInputRepository(job), audit).execute(_context(job))
    assert result.status == "NO_RELEVANT_WORK"
    assert result.analytic_run_id is None
    assert audit.calls == []


def test_no_relevant_work_core_does_not_finalize_job_or_audit() -> None:
    job, audit = _job(), _AuditSpy()
    repository = _NoInputRepository(job)
    ProactiveAnalysisService(repository, audit).execute(_context(job))
    assert job["state"] == "STARTED"
    assert audit.calls == []


class _QueryRepository:
    @contextmanager
    def transaction(self):
        yield object()

    def list_completed_proactive_runs(self, connection, **kwargs):
        return []


class _SecuritySpy:
    def __init__(self):
        self.capabilities = []

    def revalidate_functional_access(self, connection, actor, capability):
        self.capabilities.append(capability)
        return actor


def test_proactive_query_requires_dashboard_read() -> None:
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), "reader", 1, frozenset({"JURIDICO"}), frozenset({"dashboard.read"}))
    context = ProactiveQueryContext(uuid4(), uuid4(), principal)
    security = _SecuritySpy()
    service = ProactiveAnalysisQueryService(_QueryRepository(), security, _AuditSpy())
    assert service.list_completed(ProactiveAnalysisQuery(), context) == ()
    assert security.capabilities == ["dashboard.read"]
