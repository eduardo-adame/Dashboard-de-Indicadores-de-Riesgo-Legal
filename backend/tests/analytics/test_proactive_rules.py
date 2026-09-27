from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.analytics.models import ProactiveObservation
from app.analytics.proactive_rules import (
    build_finding,
    canonical_fingerprint,
    derive_run_id,
    derive_run_operation_id,
    evaluate_snapshot,
    executive_summary,
    run_metadata,
    select_snapshot_closure,
)


def _row(
    code: str,
    month: int,
    value: str | None,
    *,
    dimensions=None,
    calculated_offset: int = 0,
) -> ProactiveObservation:
    start = date(2026, month, 1)
    end = date(2026, month + 1, 1) - timedelta(days=1) if month < 12 else date(2026, 12, 31)
    return ProactiveObservation(
        uuid4(), uuid4(), code, dimensions or {}, start, end, start,
        "DISPONIBLE" if value is not None else "NO_DISPONIBLE",
        None if value is None else Decimal(value),
        datetime(2026, month, 15, tzinfo=UTC) + timedelta(seconds=calculated_offset),
    )


def _evaluate(code: str, values: list[str | None], *, dimensions=None):
    rows = tuple(_row(code, index + 1, value, dimensions=dimensions) for index, value in enumerate(values))
    run_id = uuid4()
    return evaluate_snapshot(run_id, rows)[0], rows


def test_same_value_recalculation_with_new_calculated_at_changes_fingerprint() -> None:
    original = _row("KPI-RC-03", 1, "5")
    recalculated = ProactiveObservation(
        original.source_observation_id, original.source_analytic_run_id, original.kpi_code,
        original.canonical_dimensions_key, original.period_start, original.period_end,
        original.as_of_date, original.availability, original.value,
        original.calculated_at + timedelta(seconds=1),
    )
    assert canonical_fingerprint((original,)) != canonical_fingerprint((recalculated,))


def _historical_recalculation_case():
    rows = tuple(_row("KPI-RC-03", month, str(month)) for month in range(1, 7))
    changed_historical = ProactiveObservation(
        rows[0].source_observation_id, rows[0].source_analytic_run_id, rows[0].kpi_code,
        rows[0].canonical_dimensions_key, rows[0].period_start, rows[0].period_end,
        rows[0].as_of_date, rows[0].availability, rows[0].value,
        rows[0].calculated_at + timedelta(days=1),
    )
    changed = (changed_historical,) + rows[1:]
    return rows, changed, changed_historical


def test_relevance_fingerprint_uses_full_eligible_input_not_snapshot_closure() -> None:
    rows, changed, _ = _historical_recalculation_case()
    assert canonical_fingerprint(rows) != canonical_fingerprint(changed)
    assert canonical_fingerprint(select_snapshot_closure(rows)) == canonical_fingerprint(select_snapshot_closure(changed))


def test_historical_recalculation_outside_closure_is_relevant() -> None:
    rows, changed, _ = _historical_recalculation_case()
    assert canonical_fingerprint(rows) != canonical_fingerprint(changed)


def test_historical_same_value_new_version_is_relevant() -> None:
    rows, changed, changed_historical = _historical_recalculation_case()
    assert changed_historical.value == rows[0].value
    assert changed_historical.calculated_at > rows[0].calculated_at
    assert canonical_fingerprint(rows) != canonical_fingerprint(changed)


def test_historical_recalculation_reevaluates_current_window() -> None:
    rows, changed, _ = _historical_recalculation_case()
    original_evaluation = evaluate_snapshot(uuid4(), select_snapshot_closure(rows))[0]
    changed_evaluation = evaluate_snapshot(uuid4(), select_snapshot_closure(changed))[0]
    assert original_evaluation.evaluation_period == changed_evaluation.evaluation_period == date(2026, 6, 1)


def test_historical_recalculation_does_not_expand_minimum_snapshot() -> None:
    rows, changed, changed_historical = _historical_recalculation_case()
    original_snapshot = select_snapshot_closure(rows)
    changed_snapshot = select_snapshot_closure(changed)
    assert len(original_snapshot) == len(changed_snapshot) == 5
    assert changed_historical.source_observation_id not in {row.source_observation_id for row in changed_snapshot}


def test_fingerprint_is_order_independent_and_uuid5_is_stable() -> None:
    rows = (_row("KPI-RC-03", 1, "1"), _row("KPI-CN-02", 1, "2"))
    fingerprint = canonical_fingerprint(rows)
    assert fingerprint == canonical_fingerprint(tuple(reversed(rows)))
    parent = uuid4()
    operation = derive_run_operation_id(parent, fingerprint)
    assert operation == derive_run_operation_id(parent, fingerprint)
    assert derive_run_id(operation) == derive_run_id(operation)


def test_snapshot_closure_is_five_months_for_two_month_rules() -> None:
    rows = tuple(_row("KPI-RC-03", month, str(month)) for month in range(1, 7))
    selected = select_snapshot_closure(rows)
    assert [row.period_start.month for row in selected] == [2, 3, 4, 5, 6]


def test_snapshot_closure_is_six_months_for_three_month_rules() -> None:
    rows = tuple(_row("KPI-LI-05", month, str(month)) for month in range(1, 8))
    selected = select_snapshot_closure(rows)
    assert [row.period_start.month for row in selected] == [2, 3, 4, 5, 6, 7]


def test_context_snapshot_keeps_only_latest_observation() -> None:
    rows = tuple(_row("KPI-RC-01", month, str(month)) for month in range(1, 4))
    selected = select_snapshot_closure(rows)
    assert len(selected) == 1 and selected[0].period_start.month == 3
    start, end, codes = run_metadata(selected)
    assert start.date() == date(2026, 3, 1)
    assert end.date() == date(2026, 3, 31)
    assert codes == ()


def test_latest_no_disponible_month_is_not_skipped() -> None:
    evaluation, _ = _evaluate("KPI-RC-03", ["4", None])
    assert evaluation.evaluation_period == date(2026, 2, 1)
    assert evaluation.outcome == "INSUFFICIENT_HISTORY"
    assert evaluation.current_value is None


def test_rc03_appearance_and_increase_overlap_create_one_signal() -> None:
    evaluation, rows = _evaluate("KPI-RC-03", ["0", "4"])
    assert evaluation.signal_detected is True
    assert evaluation.reference_value == 0
    assert evaluation.absolute_variation == 4
    finding = build_finding(uuid4(), evaluation, rows[-1].period_end)
    assert finding is not None and finding.triggered_rule == "RC03_APPEARANCE_OR_INCREASE"


def test_cn02_increase_is_a_signal() -> None:
    evaluation, _ = _evaluate("KPI-CN-02", ["3", "5"])
    assert evaluation.signal_detected is True


def test_three_month_signal_uses_previous_month_reference() -> None:
    evaluation, rows = _evaluate("KPI-LI-01", ["1", "3", "8"], dimensions={"nivel_severidad": "alto"})
    assert evaluation.signal_detected is True
    assert evaluation.reference_value == 3
    assert evaluation.absolute_variation == 5
    assert evaluation.percentage_variation == Decimal(5) / Decimal(3) * Decimal(100)
    finding = build_finding(uuid4(), evaluation, rows[-1].period_end)
    assert finding is not None and finding.period_start == date(2026, 1, 1)


def test_li05_and_cn03_require_strict_three_month_increase() -> None:
    li05, _ = _evaluate("KPI-LI-05", ["1", "2", "3"])
    cn03, _ = _evaluate("KPI-CN-03", ["4", "5", "6"], dimensions={"area": "Legal", "nivel_severidad": "alto"})
    assert li05.signal_detected and cn03.signal_detected
    assert li05.trend_direction == cn03.trend_direction == "INCREASING"


def test_equal_and_mixed_values_have_no_trend_or_signal() -> None:
    equal, _ = _evaluate("KPI-LI-05", ["2", "2", "2"])
    mixed, _ = _evaluate("KPI-LI-05", ["1", "3", "2"])
    assert equal.trend_direction is None and not equal.signal_detected
    assert mixed.trend_direction is None and not mixed.signal_detected


def test_strict_decrease_has_trend_without_signal() -> None:
    evaluation, _ = _evaluate("KPI-LI-05", ["3", "2", "1"])
    assert evaluation.trend_direction == "DECREASING"
    assert not evaluation.signal_detected


def test_missing_immediate_month_is_insufficient_history() -> None:
    rows = (_row("KPI-RC-03", 1, "1"), _row("KPI-RC-03", 3, "3"))
    evaluation = evaluate_snapshot(uuid4(), rows)[0]
    assert evaluation.outcome == "INSUFFICIENT_HISTORY"
    assert evaluation.recurrence_month_count is None


def test_available_zero_is_valid_but_no_disponible_is_not_zero() -> None:
    zero, _ = _evaluate("KPI-RC-03", ["0", "1"])
    unavailable, _ = _evaluate("KPI-RC-03", [None, "1"])
    assert zero.outcome == "EVALUATED" and zero.signal_detected
    assert unavailable.outcome == "INSUFFICIENT_HISTORY" and not unavailable.signal_detected


def test_recurrence_counts_distinct_terminal_months() -> None:
    evaluation, _ = _evaluate("KPI-RC-03", ["0", "1", "2", "3", "4"])
    assert evaluation.recurrence_month_count == 4
    assert evaluation.signal_detected


def test_multiple_daily_runs_same_month_do_not_inflate_recurrence() -> None:
    rows = tuple(_row("KPI-RC-03", month, str(month)) for month in range(1, 6))
    first = evaluate_snapshot(uuid4(), rows)[0]
    second = evaluate_snapshot(uuid4(), rows)[0]
    assert first.recurrence_month_count == second.recurrence_month_count == 4


def test_recurrence_two_of_four_is_not_recurrent() -> None:
    evaluation, rows = _evaluate("KPI-RC-03", ["0", "0", "0", "1", "2"])
    assert evaluation.recurrence_month_count == 2
    finding = build_finding(uuid4(), evaluation, rows[-1].period_end)
    assert finding is not None and finding.recurrent_pattern is None


def test_context_only_kpis_create_no_evaluations_or_findings() -> None:
    snapshot = select_snapshot_closure((_row("KPI-RC-01", 1, "4"), _row("KPI-EO-01", 2, "2")))
    assert evaluate_snapshot(uuid4(), snapshot) == ()
    assert executive_summary(()) == "Análisis completado sin hallazgos."


def test_cd03_is_excluded_from_snapshot_and_evaluation() -> None:
    cd03 = _row("KPI-CD-03", 1, "80")
    assert select_snapshot_closure((cd03,)) == ()
    assert evaluate_snapshot(uuid4(), (cd03,)) == ()
