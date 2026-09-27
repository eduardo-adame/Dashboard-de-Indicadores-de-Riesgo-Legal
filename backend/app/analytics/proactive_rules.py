"""Reglas deterministas y puras del análisis proactivo."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid5

from app.analytics.models import (
    ProactiveEvaluationResult,
    ProactiveFindingResult,
    ProactiveObservation,
)


SIGNAL_KPIS = frozenset({"KPI-RC-03", "KPI-LI-01", "KPI-LI-05", "KPI-CN-02", "KPI-CN-03"})
CONTEXT_KPIS = frozenset({"KPI-RC-01", "KPI-EO-01"})
ELIGIBLE_KPIS = SIGNAL_KPIS | CONTEXT_KPIS
THREE_MONTH_KPIS = frozenset({"KPI-LI-01", "KPI-LI-05", "KPI-CN-03"})

RULE_IDS = {
    "KPI-RC-03": "RC03_APPEARANCE_OR_INCREASE",
    "KPI-LI-01": "LI01_STRICT_THREE_MONTH_INCREASE",
    "KPI-LI-05": "LI05_STRICT_THREE_MONTH_INCREASE",
    "KPI-CN-02": "CN02_APPEARANCE_OR_INCREASE",
    "KPI-CN-03": "CN03_STRICT_THREE_MONTH_INCREASE",
}

RUN_OPERATION_NAMESPACE = UUID("c0c18a66-ff77-5ea5-a98d-3dcce36c07cf")
RUN_ENTITY_NAMESPACE = UUID("649e7cd9-470c-55ee-93ba-79d26005fe2c")
EVALUATION_NAMESPACE = UUID("fc14e9c4-79aa-527b-ad8d-fde3d7a2dc63")
FINDING_NAMESPACE = UUID("b761e099-a2db-56e9-9433-a8fe6c55f6df")
CONTEXT_NAMESPACE = UUID("136caf4e-ae0f-51f0-884c-f7b69be3da6b")
CONTEXT_OPERATION_NAMESPACE = UUID("bcdfbcdb-c485-5409-86fa-36c7872e65a2")
SNAPSHOT_NAMESPACE = UUID("651f6d11-b34d-54bc-86db-55175b20d59e")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_dimensions(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def canonical_dimensions_json(value: Mapping[str, Any]) -> str:
    return json.dumps(canonical_dimensions(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_fingerprint(observations: Iterable[ProactiveObservation]) -> str:
    ordered = sorted(
        observations,
        key=lambda row: (
            row.kpi_code,
            canonical_dimensions_json(row.canonical_dimensions_key),
            row.period_start,
            row.period_end,
            str(row.source_observation_id),
        ),
    )
    payload = [
        {
            "source_observation_id": str(row.source_observation_id),
            "source_analytic_run_id": str(row.source_analytic_run_id),
            "kpi_code": row.kpi_code,
            "canonical_dimensions_key": canonical_dimensions(row.canonical_dimensions_key),
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
            "as_of_date": row.as_of_date.isoformat(),
            "availability": row.availability,
            "value": None if row.value is None else format(row.value, "f"),
            "calculated_at": _utc_iso(row.calculated_at),
        }
        for row in ordered
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_fingerprint_contract(rules_reference: object) -> bool:
    if not isinstance(rules_reference, dict) or rules_reference.get("contract") != "PROACTIVE_ANALYSIS_V1":
        return False
    fingerprint = rules_reference.get("input_fingerprint_sha256")
    if not isinstance(fingerprint, str) or _SHA256_RE.fullmatch(fingerprint) is None:
        return False
    try:
        UUID(str(rules_reference["parent_job_run_id"]))
        UUID(str(rules_reference["parent_operation_id"]))
    except (KeyError, TypeError, ValueError):
        return False
    rules = rules_reference.get("signal_rules")
    return isinstance(rules, list) and rules == sorted(rules) and all(isinstance(item, str) for item in rules)


def rules_reference(*, fingerprint: str, job_run_id: UUID, parent_operation_id: UUID) -> dict[str, Any]:
    return {
        "contract": "PROACTIVE_ANALYSIS_V1",
        "input_fingerprint_sha256": fingerprint,
        "parent_job_run_id": str(job_run_id),
        "parent_operation_id": str(parent_operation_id),
        "signal_rules": sorted(RULE_IDS.values()),
    }


def derive_run_operation_id(parent_operation_id: UUID, fingerprint: str) -> UUID:
    return uuid5(RUN_OPERATION_NAMESPACE, f"{parent_operation_id}:{fingerprint}")


def derive_run_id(operation_id: UUID) -> UUID:
    return uuid5(RUN_ENTITY_NAMESPACE, str(operation_id))


def derive_evaluation_id(run_id: UUID, kpi_code: str, dimensions: Mapping[str, Any], period: date) -> UUID:
    return uuid5(EVALUATION_NAMESPACE, f"{run_id}:{kpi_code}:{canonical_dimensions_json(dimensions)}:{period.isoformat()}")


def derive_finding_id(evaluation_id: UUID, rule: str) -> UUID:
    return uuid5(FINDING_NAMESPACE, f"{evaluation_id}:{rule}")


def derive_context_id(finding_id: UUID) -> UUID:
    return uuid5(CONTEXT_NAMESPACE, str(finding_id))


def derive_context_operation_id(run_operation_id: UUID, finding_id: UUID) -> UUID:
    return uuid5(CONTEXT_OPERATION_NAMESPACE, f"{run_operation_id}:{finding_id}")


def derive_snapshot_id(run_id: UUID, observation_id: UUID) -> UUID:
    return uuid5(SNAPSHOT_NAMESPACE, f"{run_id}:{observation_id}")


def select_snapshot_closure(observations: Sequence[ProactiveObservation]) -> tuple[ProactiveObservation, ...]:
    grouped: dict[tuple[str, str], list[ProactiveObservation]] = defaultdict(list)
    for row in observations:
        if row.kpi_code not in ELIGIBLE_KPIS:
            continue
        grouped[(row.kpi_code, canonical_dimensions_json(row.canonical_dimensions_key))].append(row)

    selected: list[ProactiveObservation] = []
    for (code, _), rows in grouped.items():
        rows.sort(key=lambda row: (row.period_start, row.period_end, str(row.source_observation_id)))
        current = rows[-1].period_start
        if code in CONTEXT_KPIS:
            selected.append(rows[-1])
            continue
        depth = 5 if code in {"KPI-RC-03", "KPI-CN-02"} else 6
        allowed = {_shift_month(current, -offset) for offset in range(depth)}
        selected.extend(row for row in rows if row.period_start in allowed)
    return tuple(sorted(selected, key=_observation_sort_key))


def run_metadata(snapshot: Sequence[ProactiveObservation]) -> tuple[datetime, datetime, tuple[str, ...]]:
    signal_series: dict[tuple[str, str], date] = {}
    context = [row for row in snapshot if row.kpi_code in CONTEXT_KPIS]
    for row in snapshot:
        if row.kpi_code in SIGNAL_KPIS:
            key = (row.kpi_code, canonical_dimensions_json(row.canonical_dimensions_key))
            signal_series[key] = max(signal_series.get(key, row.period_start), row.period_start)
    if signal_series:
        starts = [
            _shift_month(current, -4 if code in {"KPI-RC-03", "KPI-CN-02"} else -5)
            for (code, _), current in signal_series.items()
        ]
        ends = [_month_end(current) for current in signal_series.values()]
        codes = tuple(sorted({code for code, _ in signal_series}))
    else:
        starts = [row.period_start for row in context]
        ends = [row.period_end for row in context]
        codes = ()
    return _at_start(min(starts)), _at_end(max(ends)), codes


def evaluate_snapshot(run_id: UUID, snapshot: Sequence[ProactiveObservation]) -> tuple[ProactiveEvaluationResult, ...]:
    grouped: dict[tuple[str, str], list[ProactiveObservation]] = defaultdict(list)
    for row in snapshot:
        if row.kpi_code in SIGNAL_KPIS:
            grouped[(row.kpi_code, canonical_dimensions_json(row.canonical_dimensions_key))].append(row)
    evaluations = [_evaluate_series(run_id, rows) for rows in grouped.values()]
    return tuple(sorted(evaluations, key=lambda item: (item.kpi_code, canonical_dimensions_json(item.canonical_dimensions_key), item.evaluation_period)))


def build_finding(run_operation_id: UUID, evaluation: ProactiveEvaluationResult, period_end: date) -> ProactiveFindingResult | None:
    if not evaluation.signal_detected or evaluation.current_value is None or evaluation.reference_value is None or evaluation.absolute_variation is None:
        return None
    rule = RULE_IDS[evaluation.kpi_code]
    finding_id = derive_finding_id(evaluation.id, rule)
    recurrent = f"{evaluation.recurrence_month_count}_OF_4" if (evaluation.recurrence_month_count or 0) >= 3 else None
    dimensions_text = canonical_dimensions_json(evaluation.canonical_dimensions_key)
    recurrence_text = f"recurrencia {evaluation.recurrence_month_count}/4" if recurrent else "sin patrón recurrente"
    description = (
        f"{evaluation.kpi_code} registró una señal en {evaluation.evaluation_period:%Y-%m} "
        f"para {dimensions_text}: valor actual {evaluation.current_value}, referencia {evaluation.reference_value}, "
        f"variación absoluta {evaluation.absolute_variation}, regla {rule}, {recurrence_text}."
    )
    reference_start = _shift_month(
        evaluation.evaluation_period,
        -2 if evaluation.kpi_code in THREE_MONTH_KPIS else -1,
    )
    return ProactiveFindingResult(
        finding_id,
        evaluation.analytic_run_id,
        evaluation.id,
        evaluation.kpi_code,
        canonical_dimensions(evaluation.canonical_dimensions_key),
        reference_start,
        period_end,
        evaluation.current_value,
        evaluation.reference_value,
        evaluation.absolute_variation,
        rule,
        recurrent,
        description,
    )


def executive_summary(findings: Sequence[ProactiveFindingResult]) -> str:
    if not findings:
        return "Análisis completado sin hallazgos."
    ordered = sorted(findings, key=lambda item: (item.kpi_code, canonical_dimensions_json(item.dimensions), item.period_start, str(item.id)))
    return "\n".join(item.description for item in ordered)


def terminal_months_used(evaluation: ProactiveEvaluationResult) -> tuple[str, ...]:
    return tuple(_shift_month(evaluation.evaluation_period, -offset).isoformat() for offset in range(4))


def _evaluate_series(run_id: UUID, rows: Sequence[ProactiveObservation]) -> ProactiveEvaluationResult:
    ordered = sorted(rows, key=lambda row: row.period_start)
    current = ordered[-1]
    by_month = {row.period_start: row for row in ordered}
    previous = by_month.get(_shift_month(current.period_start, -1))
    earlier = by_month.get(_shift_month(current.period_start, -2))
    code = current.kpi_code
    dimensions = canonical_dimensions(current.canonical_dimensions_key)
    evaluation_id = derive_evaluation_id(run_id, code, dimensions, current.period_start)
    required = (current, previous) if code not in THREE_MONTH_KPIS else (current, previous, earlier)
    rules = (RULE_IDS[code], "TREND_STRICT_THREE_MONTH", "RECURRENCE_THREE_OF_FOUR")
    if any(row is None or not _available(row) for row in required):
        return ProactiveEvaluationResult(
            evaluation_id, run_id, code, dimensions, current.period_start,
            "INSUFFICIENT_HISTORY", False, None, None,
            current.value if _available(current) else None,
            previous.value if previous is not None and _available(previous) else None,
            None, None, rules, "INSUFFICIENT_MONTHLY_HISTORY",
        )

    assert previous is not None and current.value is not None and previous.value is not None
    absolute = current.value - previous.value
    percentage = None if previous.value == 0 else absolute / previous.value * Decimal(100)
    trend = _trend(current, previous, earlier)
    signal = _signal(code, current, previous, earlier)
    recurrence = sum(
        1 for offset in range(4)
        if _terminal_signal(code, by_month, _shift_month(current.period_start, -offset))
    )
    return ProactiveEvaluationResult(
        evaluation_id, run_id, code, dimensions, current.period_start,
        "EVALUATED", signal, trend, recurrence,
        current.value, previous.value, absolute, percentage, rules, None,
    )


def _signal(code: str, current: ProactiveObservation, previous: ProactiveObservation, earlier: ProactiveObservation | None) -> bool:
    assert current.value is not None and previous.value is not None
    if code in {"KPI-RC-03", "KPI-CN-02"}:
        return (current.value > 0 and previous.value == 0) or current.value > previous.value
    return earlier is not None and _available(earlier) and earlier.value < previous.value < current.value


def _terminal_signal(code: str, by_month: Mapping[date, ProactiveObservation], terminal: date) -> bool:
    current = by_month.get(terminal)
    previous = by_month.get(_shift_month(terminal, -1))
    earlier = by_month.get(_shift_month(terminal, -2))
    required = (current, previous) if code not in THREE_MONTH_KPIS else (current, previous, earlier)
    if any(row is None or not _available(row) for row in required):
        return False
    return _signal(code, current, previous, earlier)  # type: ignore[arg-type]


def _trend(current: ProactiveObservation, previous: ProactiveObservation, earlier: ProactiveObservation | None) -> str | None:
    if earlier is None or not _available(earlier):
        return None
    assert current.value is not None and previous.value is not None and earlier.value is not None
    if earlier.value < previous.value < current.value:
        return "INCREASING"
    if earlier.value > previous.value > current.value:
        return "DECREASING"
    return None


def _available(row: ProactiveObservation) -> bool:
    return row.availability == "DISPONIBLE" and row.value is not None


def _shift_month(value: date, offset: int) -> date:
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _month_end(value: date) -> date:
    return _shift_month(value, 1) - timedelta(days=1)


def _at_start(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _at_end(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, 23, 59, 59, 999999, tzinfo=timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _observation_sort_key(row: ProactiveObservation):
    return (row.kpi_code, canonical_dimensions_json(row.canonical_dimensions_key), row.period_start, str(row.source_observation_id))
