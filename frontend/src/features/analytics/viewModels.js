import { formatDate, formatDecimal } from '../../shared/formatters.js'

export const SEVERITIES = Object.freeze([
  { key: 'alto', label: 'Alto', color: 'var(--critical)' },
  { key: 'medio', label: 'Medio', color: 'var(--warning)' },
  { key: 'bajo', label: 'Bajo', color: 'var(--success)' },
])

function chartNumber(item) {
  if (item.availability !== 'DISPONIBLE' || item.value === null) return null
  const number = Number(item.value)
  const normalize = (value) => value.replace(/^(-?)0+(?=\d)/, '$1').replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '').replace(/^-0$/, '0')
  return Number.isFinite(number) && Math.abs(number) <= Number.MAX_SAFE_INTEGER && normalize(String(number)) === normalize(item.value) ? number : null
}

export function sumDecimals(values) {
  const precision = Math.max(...values.map((value) => String(value).split('.')[1]?.length || 0))
  const factor = 10n ** BigInt(precision)
  const total = values.reduce((sum, value) => {
    const text = String(value)
    const [integer, fraction = ''] = text.replace(/^-/, '').split('.')
    const magnitude = BigInt(integer) * factor + BigInt(fraction.padEnd(precision, '0') || '0')
    return sum + (text.startsWith('-') ? -magnitude : magnitude)
  }, 0n)
  const sign = total < 0n ? '-' : ''
  const absolute = total < 0n ? -total : total
  const integer = absolute / factor
  const fraction = precision ? String(absolute % factor).padStart(precision, '0').replace(/0+$/, '') : ''
  return `${sign}${integer}${fraction ? `.${fraction}` : ''}`
}

function byPeriod(a, b) {
  return a.period_start.localeCompare(b.period_start)
}

function periodRow(item) {
  return {
    id: item.period_start,
    period_start: item.period_start,
    periodText: item.periodText,
    month: item.period_start.slice(0, 7),
  }
}

function groupedPeriods(items, kpiCode) {
  const periods = new Map()
  for (const item of items.filter((row) => row.kpi_code === kpiCode).sort(byPeriod)) {
    if (!periods.has(item.period_start)) periods.set(item.period_start, periodRow(item))
    periods.get(item.period_start).source = item
  }
  return periods
}

export function formatCurrencyMXN(value) {
  return value === null || value === undefined ? 'No disponible' : formatDecimal(String(value), { prefix: '$', suffix: ' MXN' })
}

export function formatCompactCurrencyMXN(value) {
  if (!Number.isFinite(Number(value))) return '—'
  const millions = Number(value) / 1_000_000
  const digits = Math.abs(millions) > 0 && Math.abs(millions) < 1 ? 2 : 1
  return `$${millions.toLocaleString('es-MX', { minimumFractionDigits: 0, maximumFractionDigits: digits })} M`
}

export function formatDays(value) {
  return value === null || value === undefined ? 'No disponible' : `${formatDecimal(String(value))} días`
}

export function formatCount(value, unit = '') {
  if (value === null || value === undefined) return 'No disponible'
  return `${formatDecimal(String(value))}${unit ? ` ${unit}` : ''}`
}

export function formatPercentage(value) {
  if (!Number.isFinite(Number(value))) return 'No disponible'
  return `${Number(value).toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} %`
}

export function formatPeriod(value) {
  return typeof value === 'string' && /^\d{4}-\d{2}$/.test(value) ? value : formatDate(value)
}

export function exposureEvolutionViewModel(items) {
  const periods = groupedPeriods(items, 'KPI-LI-01')
  const present = new Set()
  for (const item of items.filter((row) => row.kpi_code === 'KPI-LI-01' && row.severity)) {
    const value = chartNumber(item)
    if (value === null) continue
    periods.get(item.period_start)[item.severity] = value
    periods.get(item.period_start)[`${item.severity}Raw`] = item.value
    periods.get(item.period_start)[`${item.severity}Text`] = formatCurrencyMXN(item.value)
    present.add(item.severity)
  }
  return {
    rows: [...periods.values()].filter((row) => SEVERITIES.some(({ key }) => row[key] !== undefined)),
    series: SEVERITIES.filter(({ key }) => present.has(key)),
  }
}

export function severityCompositionViewModel(items) {
  const eligible = items.filter((item) => item.kpi_code === 'KPI-LI-01' && item.severity)
  if (!eligible.length) return { period: null, total: null, rows: [] }
  const period = eligible.reduce((latest, item) => item.period_start > latest ? item.period_start : latest, eligible[0].period_start)
  const selected = new Map()
  for (const item of eligible.filter((row) => row.period_start === period)) {
    const value = chartNumber(item)
    if (value !== null) selected.set(item.severity, { item, value })
  }
  if (!selected.size) return { period, total: null, rows: [] }
  const totalRaw = sumDecimals([...selected.values()].map((row) => row.item.value))
  const total = Number(totalRaw)
  const rows = SEVERITIES.flatMap((severity) => {
    const selectedRow = selected.get(severity.key)
    if (!selectedRow) return []
    return [{
      id: severity.key,
      severity: severity.key,
      name: severity.label,
      value: selectedRow.value,
      valueText: formatCurrencyMXN(selectedRow.item.value),
      percentage: total === 0 ? 0 : selectedRow.value / total * 100,
      percentageText: formatPercentage(total === 0 ? 0 : selectedRow.value / total * 100),
      color: severity.color,
    }]
  })
  return {
    period,
    periodText: selected.values().next().value?.item.periodText || period,
    total,
    totalText: formatCurrencyMXN(totalRaw),
    rows,
  }
}

export function temporalSeriesViewModel(items, kpiCode) {
  return items.filter((row) => row.kpi_code === kpiCode).sort(byPeriod).map((item) => ({
    ...periodRow(item),
    value: chartNumber(item),
    valueText: item.displayValue,
    unit: item.unit,
    availability: item.availability,
    dimensionsText: item.dimensionsText,
  }))
}

export function litigationExposureViewModel(items) {
  const periods = groupedPeriods(items, 'KPI-LI-01')
  for (const item of items.filter((row) => row.kpi_code === 'KPI-LI-01' && row.severity)) {
    const value = chartNumber(item)
    if (value === null) continue
    const row = periods.get(item.period_start)
    row[item.severity] = value
    row[`${item.severity}Raw`] = item.value
    row[`${item.severity}Text`] = formatCurrencyMXN(item.value)
  }
  const rows = [...periods.values()].map((row) => {
    const complete = SEVERITIES.every(({ key }) => row[key] !== undefined)
    const totalRaw = complete ? sumDecimals(SEVERITIES.map(({ key }) => row[`${key}Raw`])) : null
    return {
      ...row,
      high: row.alto,
      highText: row.altoText,
      total: complete ? Number(totalRaw) : undefined,
      totalText: complete ? formatCurrencyMXN(totalRaw) : 'No disponible',
    }
  }).filter((row) => row.high !== undefined || row.total !== undefined)
  return { rows, hasTotal: rows.some((row) => row.total !== undefined), hasHigh: rows.some((row) => row.high !== undefined) }
}

export function findingSeverity(dimensions) {
  const value = dimensions?.nivel_severidad
  return SEVERITIES.some(({ key }) => key === value) ? value : null
}
