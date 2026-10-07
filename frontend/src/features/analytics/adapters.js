import { ApiError } from '../../api/errors.js'
import { formatDate, formatDecimal, formatTimestamp, formatValue, validDate } from '../../shared/formatters.js'

export const KPI_CATALOG = Object.freeze({
  'KPI-RC-03': { name: 'Contratos próximos a vencimiento sin revisión', unit: 'contratos', core: true },
  'KPI-LI-01': { name: 'Exposición total por litigios activos', unit: 'importe', core: true },
  'KPI-LI-05': { name: 'Nuevos litigios por periodo', unit: 'litigios', core: true },
  'KPI-CN-02': { name: 'Obligaciones regulatorias vencidas sin atender', unit: 'obligaciones', core: true },
  'KPI-CN-03': { name: 'Incidentes de incumplimiento por periodo', unit: 'incidentes', core: true },
  'KPI-RC-01': { name: 'Tiempo de ciclo del contrato', unit: 'días', core: false },
  'KPI-EO-01': { name: 'Volumen de asuntos jurídicos gestionados', unit: 'asuntos', core: false },
})
const fail = () => { throw new ApiError(503) }
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const decimal = (value) => typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value)
const nullableDecimal = (value) => value === null || decimal(value)
const id = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const timestamp = (value) => formatTimestamp(value) !== '—'
const nullableString = (value) => value === null || typeof value === 'string'
const code = (value) => Object.hasOwn(KPI_CATALOG, value)
const core = (value) => code(value) && KPI_CATALOG[value].core
export function dimensionsLabel(dimensions) {
  return Object.entries(dimensions).sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key.replace(/_/g, ' ')}: ${typeof value === 'object' ? JSON.stringify(value) : String(value)}`).join(' · ') || 'Sin dimensiones'
}
export function seriesIdentity(item) {
  const dimensions = item.dimensions || item.canonical_dimensions_key
  return `${item.kpi_code}:${JSON.stringify(Object.entries(dimensions).sort(([a], [b]) => a.localeCompare(b)))}`
}
export function adaptKpis(data) {
  if (!object(data) || !Array.isArray(data.items) || !object(data.filters) || !['period_start', 'period_end', 'period_reference'].every((key) => data[key] === null || validDate(data[key]))) fail()
  const items = data.items.map((item, index) => {
    if (!object(item) || !code(item.kpi_code) || item.name !== KPI_CATALOG[item.kpi_code].name || item.unit !== KPI_CATALOG[item.kpi_code].unit || !['MVP-NÚCLEO', 'MVP-COMPLEMENTARIO'].includes(item.classification) || !object(item.dimensions) || typeof item.entity_filter_applicable !== 'boolean' || !['DISPONIBLE', 'NO_DISPONIBLE'].includes(item.availability) || !nullableDecimal(item.value) || (item.availability === 'NO_DISPONIBLE' && item.value !== null) || !validDate(item.period_start) || !validDate(item.period_end) || item.period_start > item.period_end || !validDate(item.as_of_date) || !timestamp(item.calculated_at)) fail()
    return { ...item, id: `observation-${index}`, displayValue: formatValue(item.value, item.availability), dimensionsText: dimensionsLabel(item.dimensions), periodText: `${formatDate(item.period_start)} – ${formatDate(item.period_end)}`, entityText: item.entity_filter_applicable ? String(item.dimensions.entity) : 'No aplicable' }
  })
  const identities = new Set()
  for (const item of items) { const key = `${seriesIdentity(item)}:${item.period_start}`; if (identities.has(key)) fail(); identities.add(key) }
  return { ...data, items }
}
export function latestSeries(items, kpiCode) {
  const selected = new Map()
  for (const item of items.filter((row) => row.kpi_code === kpiCode)) {
    const key = seriesIdentity(item); const previous = selected.get(key)
    if (!previous || item.period_start > previous.period_start || (item.period_start === previous.period_start && item.calculated_at > previous.calculated_at)) selected.set(key, item)
  }
  return [...selected.values()].sort((a, b) => seriesIdentity(a).localeCompare(seriesIdentity(b)))
}
const RULE_LABELS = Object.freeze({
  RC03_APPEARANCE_OR_INCREASE: 'Aparición o incremento de contratos sin revisión',
  CN02_APPEARANCE_OR_INCREASE: 'Aparición o incremento de obligaciones vencidas',
  LI01_STRICT_THREE_MONTH_INCREASE: 'Incremento durante tres meses consecutivos de exposición litigiosa',
  LI05_STRICT_THREE_MONTH_INCREASE: 'Incremento durante tres meses consecutivos de nuevos litigios',
  CN03_STRICT_THREE_MONTH_INCREASE: 'Incremento durante tres meses consecutivos de incidentes',
})
export const ruleLabel = (rule) => RULE_LABELS[rule] || 'Regla persistida de análisis'
export function adaptAnalysis(data) {
  if (!object(data) || !(data.current_analytic_run_id === null || id(data.current_analytic_run_id)) || !Array.isArray(data.items) || !Number.isSafeInteger(data.alert_count) || data.alert_count < 0) fail()
  const runIds = new Set()
  const items = data.items.map((run) => {
    if (!object(run) || !id(run.analytic_run_id) || runIds.has(run.analytic_run_id) || !timestamp(run.completed_at) || !timestamp(run.window_start) || !timestamp(run.window_end) || typeof run.executive_summary !== 'string' || !Array.isArray(run.evaluations) || !Array.isArray(run.findings) || !Array.isArray(run.context_references) || (run.state !== undefined && run.state !== 'COMPLETED')) fail()
    runIds.add(run.analytic_run_id)
    const evaluations = run.evaluations.map((row) => {
      if (!object(row) || !id(row.id) || row.analytic_run_id !== run.analytic_run_id || !core(row.kpi_code) || !object(row.canonical_dimensions_key) || !validDate(row.evaluation_period) || !['EVALUATED', 'INSUFFICIENT_HISTORY'].includes(row.outcome) || typeof row.signal_detected !== 'boolean' || ![null, 'INCREASING', 'DECREASING'].includes(row.trend_direction) || !(row.recurrence_month_count === null || Number.isInteger(row.recurrence_month_count) && row.recurrence_month_count >= 0 && row.recurrence_month_count <= 4) || !['current_value', 'reference_value', 'absolute_variation', 'percentage_variation'].every((key) => nullableDecimal(row[key])) || !Array.isArray(row.rules_applied) || !row.rules_applied.length || row.rules_applied.some((value) => typeof value !== 'string') || !nullableString(row.not_evaluated_reason) || typeof row.entity_filter_applicable !== 'boolean') fail()
      if (row.outcome === 'INSUFFICIENT_HISTORY' && (row.signal_detected || row.trend_direction !== null || row.recurrence_month_count !== null || !row.not_evaluated_reason)) fail()
      if (row.outcome === 'EVALUATED' && row.not_evaluated_reason !== null) fail()
      return { ...row, name: KPI_CATALOG[row.kpi_code].name, unit: KPI_CATALOG[row.kpi_code].unit, dimensionsText: dimensionsLabel(row.canonical_dimensions_key), currentText: formatDecimal(row.current_value), referenceText: formatDecimal(row.reference_value), absoluteText: formatDecimal(row.absolute_variation), percentageText: formatDecimal(row.percentage_variation, { suffix: ' %' }), trendText: { INCREASING: 'Creciente', DECREASING: 'Decreciente' }[row.trend_direction] || 'Sin tendencia declarada', outcomeText: row.outcome === 'INSUFFICIENT_HISTORY' ? 'Histórico insuficiente' : 'Evaluado' }
    })
    const references = run.context_references.map((row) => {
      if (!object(row) || !id(row.id) || !id(row.finding_id) || !core(row.kpi_code) || !object(row.dimensions) || !validDate(row.period_start) || !validDate(row.period_end) || !object(row.context_identifiers) || !id(row.operation_id) || !id(row.correlation_id)) fail()
      return { ...row }
    })
    const findings = run.findings.map((row) => {
      if (!object(row) || !id(row.id) || row.analytic_run_id !== run.analytic_run_id || !id(row.proactive_evaluation_id) || !core(row.kpi_code) || !object(row.dimensions) || !validDate(row.period_start) || !validDate(row.period_end) || !['current_value', 'reference_value', 'variation'].every((key) => decimal(row[key])) || typeof row.triggered_rule !== 'string' || !nullableString(row.recurrent_pattern) || typeof row.description !== 'string' || !timestamp(row.created_at) || typeof row.entity_filter_applicable !== 'boolean') fail()
      const reference = references.find((item) => item.finding_id === row.id)
      return { ...row, name: KPI_CATALOG[row.kpi_code].name, unit: KPI_CATALOG[row.kpi_code].unit, dimensionsText: dimensionsLabel(row.dimensions), currentText: formatDecimal(row.current_value), referenceText: formatDecimal(row.reference_value), variationText: formatDecimal(row.variation), ruleText: ruleLabel(row.triggered_rule), contextReferenceId: reference?.id || null }
    })
    if (references.some((row) => !findings.some((finding) => finding.id === row.finding_id && finding.kpi_code === row.kpi_code))) fail()
    return { ...run, evaluations, findings, context_references: references }
  })
  return { ...data, items }
}
export function chartSeries(items, kpiCode) {
  const groups = new Map()
  for (const item of items.filter((row) => row.kpi_code === kpiCode)) {
    const key = seriesIdentity(item)
    if (!groups.has(key)) groups.set(key, { id: key, name: item.name, unit: item.unit, dimensions: item.dimensions, dimensionsText: item.dimensionsText, points: [] })
    // Number se limita a coordenadas; tablas y cifras conservan el decimal original.
    const number = item.availability === 'DISPONIBLE' && item.value !== null ? Number(item.value) : null
    const normalizeDecimal = (value) => value.replace(/^(-?)0+(?=\d)/, '$1').replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '').replace(/^-0$/, '0')
    const chartValue = number !== null && Number.isFinite(number) && Math.abs(number) <= Number.MAX_SAFE_INTEGER && normalizeDecimal(String(number)) === normalizeDecimal(item.value) ? number : null
    groups.get(key).points.push({ ...item, chartValue, month: item.period_start.slice(0, 7) })
  }
  return [...groups.values()].sort((a, b) => a.id.localeCompare(b.id)).map((series) => ({ ...series, points: series.points.sort((a, b) => a.period_start.localeCompare(b.period_start)) }))
}
