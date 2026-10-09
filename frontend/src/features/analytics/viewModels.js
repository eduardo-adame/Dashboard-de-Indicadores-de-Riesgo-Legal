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
  const rows = items.filter((row) => row.kpi_code === kpiCode).sort(byPeriod).map((item) => ({
    ...periodRow(item),
    value: chartNumber(item),
    rawValue: item.value,
    valueText: item.displayValue,
    unit: item.unit,
    availability: item.availability,
    dimensionsText: item.dimensionsText,
  }))
  return monthlyTimeline(rows).map((row) => ({ ...row, unit: row.unit || rows[0]?.unit }))
}

function monthOrdinal(month) {
  return Number(month.slice(0, 4)) * 12 + Number(month.slice(5, 7)) - 1
}

function monthFromOrdinal(ordinal) {
  return `${String(Math.floor(ordinal / 12)).padStart(4, '0')}-${String(ordinal % 12 + 1).padStart(2, '0')}`
}

export function monthlyTimeline(rows) {
  if (!rows.length) return []
  const ordered = [...rows].sort((a, b) => a.month.localeCompare(b.month))
  const start = monthOrdinal(ordered[0].month)
  const end = monthOrdinal(ordered.at(-1).month)
  return Array.from({ length: end - start + 1 }, (_, index) => {
    const month = monthFromOrdinal(start + index)
    const present = ordered.filter((row) => row.month === month)
    if (present.length) return present
    const period_start = `${month}-01`
    const date = new Date(`${period_start}T00:00:00Z`)
    date.setUTCMonth(date.getUTCMonth() + 1); date.setUTCDate(0)
    const period_end = date.toISOString().slice(0, 10)
    // El hueco pertenece al calendario visual, no a las observaciones productivas.
    return [{ id: `gap-${month}`, month, period_start, period_end, periodText: `${formatDate(period_start)} – ${formatDate(period_end)}`, value: null, rawValue: null, valueText: 'Sin observación', availability: 'NO_OBSERVATION', observed: false }]
  }).flat()
}

export function exposureContextKey(row) {
  return JSON.stringify(Object.entries(row.dimensions).filter(([key]) => key !== 'nivel_severidad').sort(([a], [b]) => a.localeCompare(b)))
}

export function hasCompleteExposurePartition(rows) {
  return rows.length === 3 && rows.every((row) => row.kpi_code === 'KPI-LI-01' && row.availability === 'DISPONIBLE' && row.value !== null)
    && SEVERITIES.every(({ key }) => rows.filter((row) => row.severity === key).length === 1)
    && rows.every((row) => row.period_start === rows[0].period_start && row.period_end === rows[0].period_end && exposureContextKey(row) === exposureContextKey(rows[0]))
}

export function litigationExposureViewModel(items) {
  const partitions = new Map()
  for (const item of items.filter((row) => row.kpi_code === 'KPI-LI-01').sort(byPeriod)) {
    const contextKey = exposureContextKey(item)
    const id = `${item.period_start}:${item.period_end}:${contextKey}`
    if (!partitions.has(id)) partitions.set(id, { ...periodRow(item), id, period_end: item.period_end, contextKey, contextText: Object.entries(item.dimensions).filter(([key]) => key !== 'nivel_severidad').sort(([a], [b]) => a.localeCompare(b)).map(([key, value]) => `${key.replace(/_/g, ' ')}: ${typeof value === 'object' ? JSON.stringify(value) : String(value)}`).join(' · ') || 'Sin dimensiones adicionales', sources: [] })
    partitions.get(id).sources.push(item)
  }
  const rows = [...partitions.values()].map((row) => {
    const complete = hasCompleteExposurePartition(row.sources)
    const values = {}
    for (const { key } of SEVERITIES) {
      const selected = row.sources.filter((item) => item.severity === key)
      const item = selected.length === 1 ? selected[0] : null
      if (item) Object.assign(values, { [key]: chartNumber(item), [`${key}Raw`]: item.value, [`${key}Text`]: item.availability === 'NO_DISPONIBLE' ? 'No disponible' : formatCurrencyMXN(item.value), [`${key}Availability`]: item.availability })
      else values[`${key}Text`] = selected.length ? 'Observaciones no comparables' : 'Sin observación'
    }
    const totalRaw = complete ? sumDecimals(row.sources.map((item) => item.value)) : null
    return {
      ...row, ...values, complete,
      high: values.alto,
      highText: values.altoText,
      total: complete ? chartNumber({ availability: 'DISPONIBLE', value: totalRaw }) : undefined,
      totalRaw,
      totalText: complete ? formatCurrencyMXN(totalRaw) : 'No disponible',
    }
  })
  const contexts = new Map()
  for (const row of rows) {
    if (!contexts.has(row.contextKey)) contexts.set(row.contextKey, [])
    contexts.get(row.contextKey).push(row)
  }
  const traces = [...contexts.entries()].flatMap(([id, selected]) => {
    // Dos particiones simultáneas no justifican asociarlas arbitrariamente entre meses.
    const ambiguous = new Set(selected.map((row) => row.month)).size !== selected.length
    const groups = ambiguous ? selected.map((row) => [row]) : [selected]
    return groups.map((group, index) => ({ id: `${id}:${index}`, label: ambiguous ? `${group[0].contextText} · ${group[0].periodText}` : group[0].contextText, rows: monthlyTimeline(group) }))
  })
  return { rows, traces, hasTotal: rows.some((row) => row.complete), hasHigh: rows.some((row) => row.high !== undefined && row.high !== null) }
}

export function findingSeverity(dimensions) {
  const value = dimensions?.nivel_severidad
  return SEVERITIES.some(({ key }) => key === value) ? value : null
}
