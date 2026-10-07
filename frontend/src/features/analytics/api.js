import { readQuery, writeQuery } from '../../shared/query.js'
import { validDate } from '../../shared/formatters.js'
import { ApiError } from '../../api/errors.js'

export const PERIODS = Object.freeze([
  { value: 'last_3_months', label: 'Últimos 3 meses' },
  { value: 'last_6_months', label: 'Últimos 6 meses' },
  { value: 'last_12_months', label: 'Últimos 12 meses' },
  { value: 'current_month', label: 'Periodo en curso' },
  { value: 'custom', label: 'Rango personalizado' },
])
export const RISKS = Object.freeze([
  { value: 'all', label: 'Todos' }, { value: 'contractual', label: 'Riesgo contractual' },
  { value: 'litigation', label: 'Riesgo de litigio' }, { value: 'compliance', label: 'Riesgo de cumplimiento normativo' },
])
const uuid = (value) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
export const FILTER_VALIDATORS = Object.freeze({
  period: (value) => PERIODS.some((item) => item.value === value),
  period_start: validDate, period_end: validDate,
  risk_type: (value) => RISKS.some((item) => item.value === value),
  entity: (value) => value.length > 0 && value.length <= 256 && !/[\u0000-\u001f]/.test(value),
  analytic_run_id: uuid,
})
export function analyticsFilters(search, defaultRisk = 'all') {
  const raw = readQuery(search, FILTER_VALIDATORS)
  if (raw.period === 'custom' && (!raw.period_start || !raw.period_end || raw.period_start > raw.period_end)) {
    delete raw.period; delete raw.period_start; delete raw.period_end
  }
  if (raw.period !== 'custom') { delete raw.period_start; delete raw.period_end }
  const canonical = writeQuery(raw, FILTER_VALIDATORS)
  return { raw, canonical, corrected: new URLSearchParams(search).toString() !== canonical,
    period: raw.period || 'last_6_months', risk_type: raw.risk_type || defaultRisk,
    analysisMode: raw.period || raw.analytic_run_id ? 'historical' : 'current' }
}
export function analyticsPaths(filters) {
  const shared = { risk_type: filters.risk_type, entity: filters.raw.entity }
  const kpi = { ...shared, period: filters.period, ...(filters.period === 'custom' ? { period_start: filters.raw.period_start, period_end: filters.raw.period_end } : {}) }
  // Ausencia de period significa vigente en Analysis; no trasladar el default KPI.
  const analysis = { ...shared, ...(filters.analysisMode === 'historical' && filters.raw.period ? { period: filters.raw.period, period_start: filters.raw.period_start, period_end: filters.raw.period_end } : {}), analytic_run_id: filters.raw.analytic_run_id }
  return { kpis: `/dashboard/kpis?${writeQuery(kpi, FILTER_VALIDATORS)}`, analysis: `/dashboard/analysis?${writeQuery(analysis, FILTER_VALIDATORS)}` }
}
export function replaceAnalyticsFilter(raw, key, value) {
  if (!Object.hasOwn(FILTER_VALIDATORS, key) || (value && !FILTER_VALIDATORS[key](value))) throw new ApiError(422)
  const next = { ...raw }
  if (value) next[key] = value; else delete next[key]
  if (key === 'period' && value !== 'custom') { delete next.period_start; delete next.period_end }
  if (key === 'period') delete next.analytic_run_id
  return writeQuery(next, FILTER_VALIDATORS)
}
export function createAnalyticsApi(client) {
  return { kpis: (filters) => client.request(analyticsPaths(filters).kpis), analysis: (filters) => client.request(analyticsPaths(filters).analysis) }
}
