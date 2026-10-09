import { KPI_CATALOG } from './adapters.js'
import { formatCurrencyMXN, sumDecimals } from './viewModels.js'

export const EXECUTIVE_KPI_STATES = Object.freeze({
  VALUE: 'VALUE', ZERO: 'ZERO', NO_DISPONIBLE: 'NO_DISPONIBLE',
  NO_OBSERVATION: 'NO_OBSERVATION', LOADING: 'LOADING', ERROR: 'ERROR',
  FORBIDDEN: 'FORBIDDEN', NOT_APPLICABLE: 'NOT_APPLICABLE',
})

export const EXECUTIVE_KPI_CATALOG = Object.freeze([
  ['KPI-RC-03', 'contractual'], ['KPI-LI-01', 'litigation'],
  ['KPI-LI-05', 'litigation'], ['KPI-CN-02', 'compliance'], ['KPI-CN-03', 'compliance'],
].map(([code, domain]) => Object.freeze({ code, domain, ...KPI_CATALOG[code] })))

export const DOMAIN_KPI_CATALOGS = Object.freeze({
  contracts: Object.freeze(EXECUTIVE_KPI_CATALOG.filter((entry) => entry.domain === 'contractual')),
  litigation: Object.freeze(EXECUTIVE_KPI_CATALOG.filter((entry) => entry.domain === 'litigation')),
})

const isZero = (value) => /^-?0+(?:\.0+)?$/.test(value)
const usable = (row) => row.availability === 'DISPONIBLE' && row.value !== null
const contextKey = (row) => JSON.stringify(Object.entries(row.dimensions).filter(([key]) => key !== 'nivel_severidad').sort(([a], [b]) => a.localeCompare(b)))

function stateValue(state, rows = []) {
  const displayValue = {
    NO_OBSERVATION: 'Sin observación', NO_DISPONIBLE: 'No disponible',
    NOT_APPLICABLE: 'No aplicable', LOADING: 'Cargando…',
    ERROR: 'Error de lectura', FORBIDDEN: 'Sin autorización',
  }[state]
  return {
    state, displayValue, valueKind: 'state', shortContext: '', rows,
    accessibleValue: state === 'NO_OBSERVATION' ? 'Sin observación para la selección' : displayValue,
  }
}

function dimensionalValue(rows) {
  return { state: 'VALUE', displayValue: 'Desglose disponible', accessibleValue: 'Desglose disponible en gráfica y tabla', valueKind: 'state', shortContext: '', dimensional: true, rows }
}

function resolveValue(entry, items) {
  const observations = items.filter((row) => row.kpi_code === entry.code)
  if (!observations.length) return stateValue('NO_OBSERVATION')
  // Se selecciona un periodo para todo el KPI; nunca el último mes de cada dimensión.
  const period = observations.reduce((latest, row) => row.period_start > latest ? row.period_start : latest, observations[0].period_start)
  const rows = observations.filter((row) => row.period_start === period)
  if (rows.every((row) => row.availability === 'NO_DISPONIBLE' && row.value === null)) return stateValue('NO_DISPONIBLE', rows)
  const available = rows.filter(usable)
  if (!available.length) return stateValue('NO_OBSERVATION', rows)
  if (entry.code === 'KPI-CN-03') return dimensionalValue(rows)
  if (entry.code === 'KPI-LI-01') {
    const complete = rows.length === 3 && available.length === 3
      && ['alto', 'medio', 'bajo'].every((severity) => available.filter((row) => row.severity === severity).length === 1)
      && available.every((row) => contextKey(row) === contextKey(available[0]) && row.period_end === available[0].period_end)
    if (!complete) return dimensionalValue(rows)
    // Suma exacta de presentación de una partición autorizada, sin crear observaciones.
    const total = sumDecimals(available.map((row) => row.value))
    return { state: isZero(total) ? 'ZERO' : 'VALUE', displayValue: formatCurrencyMXN(total), accessibleValue: formatCurrencyMXN(total), rawValue: total, valueKind: 'number', shortContext: '', rows }
  }
  if (available.length !== 1 || rows.length !== 1) return dimensionalValue(rows)
  const row = available[0]
  return { state: isZero(row.value) ? 'ZERO' : 'VALUE', displayValue: row.displayValue, accessibleValue: row.displayValue, rawValue: row.value, valueKind: 'number', shortContext: entry.unit, rows }
}

export function resolveKpiCatalog(catalog, { items = [], riskType = 'all', resourceState = null } = {}) {
  return catalog.map((entry) => {
    const result = riskType !== 'all' && riskType !== entry.domain
      ? stateValue('NOT_APPLICABLE')
      : resourceState ? stateValue(resourceState) : resolveValue(entry, items)
    const latest = result.rows.reduce((selected, row) => !selected || row.calculated_at > selected.calculated_at ? row : selected, null)
    return { ...entry, ...result, previousContext: latest?.periodText || '', calculatedAt: latest?.calculated_at || null }
  })
}

export function resolveExecutiveKpis(options = {}) {
  return resolveKpiCatalog(EXECUTIVE_KPI_CATALOG, options)
}

export function resolveDomainKpis({ view, ...options }) {
  return resolveKpiCatalog(DOMAIN_KPI_CATALOGS[view], options)
}
