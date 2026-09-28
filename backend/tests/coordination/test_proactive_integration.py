from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.analytics.models import ProactiveAnalysisResult
from app.coordination.proactive_integration import (
    canonical_interval,
    derive_proactive_correlation_id,
    derive_proactive_operation_id,
    _terminal_references,
    _trigger_references,
)


def _interval():
    return (
        datetime(2026, 5, 1, tzinfo=UTC),
        datetime(2026, 5, 2, tzinfo=UTC),
    )


def test_scheduled_operation_identity_is_stable_and_purpose_specific() -> None:
    start, end = _interval()
    first = derive_proactive_operation_id(interval_start=start, interval_end=end)
    assert first == derive_proactive_operation_id(interval_start=start, interval_end=end)
    assert first != derive_proactive_operation_id(
        interval_start=end,
        interval_end=end + timedelta(days=1),
    )


def test_equivalent_timezones_produce_the_same_identity() -> None:
    start, end = _interval()
    offset = timezone(timedelta(hours=-6))
    assert derive_proactive_operation_id(
        interval_start=start,
        interval_end=end,
    ) == derive_proactive_operation_id(
        interval_start=start.astimezone(offset),
        interval_end=end.astimezone(offset),
    )
    assert derive_proactive_correlation_id(
        interval_start=start,
        interval_end=end,
    ) == derive_proactive_correlation_id(
        interval_start=start.astimezone(offset),
        interval_end=end.astimezone(offset),
    )


def test_timezone_less_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        canonical_interval(datetime(2026, 5, 1), datetime(2026, 5, 2))


def test_started_job_has_canonical_safe_result_references() -> None:
    start, end = _interval()
    references = _trigger_references(start, end)
    assert references == {
        "contract": "SCHEDULED_PROACTIVE_ANALYSIS_V1",
        "trigger": "CONTROLLED_INGESTION",
        "dag_id": "controlled_ingestion",
        "data_interval_start": "2026-05-01T00:00:00+00:00",
        "data_interval_end": "2026-05-02T00:00:00+00:00",
    }


def test_terminal_finalization_preserves_trigger_identity_metadata() -> None:
    start, end = _interval()
    initial = _trigger_references(start, end)
    run_id, operation_id = uuid4(), uuid4()
    terminal = _terminal_references(
        initial,
        ProactiveAnalysisResult(
            "COMPLETED",
            "a" * 64,
            run_id,
            operation_id,
        ),
    )
    for key, value in initial.items():
        assert terminal[key] == value
    assert terminal["analytic_run_id"] == str(run_id)
    assert terminal["run_operation_id"] == str(operation_id)


def test_no_relevant_work_references_do_not_contain_analytical_payload() -> None:
    start, end = _interval()
    terminal = _terminal_references(
        _trigger_references(start, end),
        ProactiveAnalysisResult("NO_RELEVANT_WORK", "b" * 64, None, None),
    )
    assert set(terminal) == {
        "contract",
        "trigger",
        "dag_id",
        "data_interval_start",
        "data_interval_end",
        "status",
        "input_fingerprint_sha256",
    }
