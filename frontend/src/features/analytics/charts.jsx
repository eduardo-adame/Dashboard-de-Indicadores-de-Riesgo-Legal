import React from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { DataTable, KpiRow, KpiSkeleton } from '../../components/data.jsx'
import { formatDate, formatTimestamp } from '../../shared/formatters.js'
import {
  exposureEvolutionViewModel,
  formatCompactCurrencyMXN,
  formatCount,
  formatDays,
  litigationExposureViewModel,
  severityCompositionViewModel,
  temporalSeriesViewModel,
} from './viewModels.js'

const AXIS_TICK = Object.freeze({ fill: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 11 })
const axisProps = Object.freeze({ axisLine: { stroke: 'var(--border-default)' }, tickLine: false, tick: AXIS_TICK })
const observationColumns = [
  { key: 'periodText', label: 'Periodo' }, { key: 'displayValue', label: 'Valor', numeric: true },
  { key: 'unit', label: 'Unidad' }, { key: 'dimensionsText', label: 'Dimensiones' },
  { key: 'availability', label: 'Disponibilidad', render: (row) => row.availability === 'DISPONIBLE' ? 'Disponible' : 'No disponible' },
]

function monthDistance(value) {
  return Number(value.slice(0, 4)) * 12 + Number(value.slice(5, 7))
}

function segmentedLines(rows, dataKey) {
  let segment = 0
  let previous = null
  const data = rows.map((row) => {
    const month = monthDistance(row.month)
    if (row[dataKey] === undefined || row[dataKey] === null) return { ...row }
    if (previous !== null && month !== previous + 1) segment++
    previous = month
    return { ...row, [`${dataKey}${segment}`]: row[dataKey] }
  })
  return { data, keys: Array.from({ length: segment + 1 }, (_, index) => `${dataKey}${index}`) }
}

function mergeSegmented(baseRows, definitions) {
  let data = baseRows
  const keys = {}
  for (const definition of definitions) {
    const result = segmentedLines(baseRows, definition.key)
    data = data.map((row, index) => ({ ...row, ...result.data[index] }))
    keys[definition.key] = result.keys
  }
  return { data, keys }
}

function TooltipSurface({ title, rows }) {
  return <div className="min-w-[160px] rounded-control border border-border bg-surface p-3 text-xs shadow-subtle"><p className="mb-2 font-medium text-primary">{title}</p><dl className="space-y-1">{rows.map((row) => <div key={row.label} className="flex items-center justify-between gap-4"><dt className="text-secondary">{row.label}</dt><dd className="font-mono tabular-nums text-primary">{row.value}</dd></div>)}</dl></div>
}

function SeriesTooltip({ active, payload, series, valueText = (row, key) => row[`${key}Text`] || 'No disponible' }) {
  const row = payload?.find((item) => item.payload)?.payload
  if (!active || !row) return null
  const visible = series.filter(({ key }) => row[key] !== undefined && row[key] !== null)
  return <TooltipSurface title={row.periodText || row.month} rows={visible.map(({ key, label }) => ({ label, value: valueText(row, key) }))} />
}

function SingleValueTooltip({ active, payload, formatter }) {
  const row = payload?.find((item) => item.payload)?.payload
  if (!active || !row) return null
  return <TooltipSurface title={row.periodText || row.month} rows={[{ label: 'Valor', value: formatter(row.value) }]} />
}

function CompositionTooltip({ active, payload }) {
  const row = payload?.[0]?.payload
  if (!active || !row) return null
  return <TooltipSurface title={row.name} rows={[{ label: 'Importe', value: row.valueText }, { label: 'Porcentaje', value: row.percentageText }]} />
}

export function ChartLegend({ items }) {
  return <ul aria-label="Leyenda" className="flex flex-wrap gap-x-5 gap-y-2 text-xs">{items.map((item) => <li key={item.key} className="inline-flex items-center gap-2"><span aria-hidden="true" className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: item.color }} /><span className="text-secondary">{item.label}</span></li>)}</ul>
}

export function ChartContainer({ title, subtitle, footer, children, className = '' }) {
  return <figure className={`min-w-0 space-y-3 rounded-panel border border-border bg-surface p-5 ${className}`}><figcaption className="text-sm font-semibold">{title}</figcaption>{subtitle && <p className="text-xs text-secondary">{subtitle}</p>}{children}{footer}</figure>
}

export function EmptyChartState({ chartTitle = '', title = 'Sin datos disponibles', message = 'Aún no existen observaciones para los filtros seleccionados.', className = '' }) {
  const ariaLabel = chartTitle ? `${chartTitle}: ${title.toLowerCase()}` : title
  return <div role="region" aria-label={ariaLabel} className={`flex h-[260px] min-w-0 flex-col items-center justify-center rounded-panel border border-dashed border-border bg-secondary/30 p-6 text-center ${className}`}><p className="text-sm font-medium text-secondary">{title}</p>{message && <p className="mt-1 max-w-sm text-xs text-muted">{message}</p>}</div>
}

export function ChartLoadingState({ label = 'Cargando…', charts = 1, showKpis = false, kpiSlots = Array.from({ length: 5 }, (_, index) => ({ code: index })) }) {
  return <section role="status" aria-live="polite" aria-label={label}><span className={showKpis ? 'sr-only' : 'text-xs text-secondary'}>{label}</span>{showKpis && <KpiRow>{kpiSlots.map((slot) => <KpiSkeleton key={slot.code} label={slot.name} />)}</KpiRow>}<div className="space-y-6">{Array.from({ length: charts }, (_, index) => <div key={index} aria-hidden="true" className="h-[360px] rounded-panel border border-border bg-secondary" />)}</div></section>
}

export function ExposureEvolutionChart({ items = [] }) {
  const model = exposureEvolutionViewModel(items)
  const title = 'Evolución de exposición acumulada'
  if (!model.rows.length) return <ChartContainer title={title} subtitle="Importes por severidad y periodo; no se imputan meses ni valores."><EmptyChartState chartTitle={title} /></ChartContainer>
  const columns = [{ key: 'periodText', label: 'Periodo' }, ...model.series.map(({ key, label }) => ({ key: `${key}Text`, label, numeric: true }))]
  return <ChartContainer title={title} subtitle="Importes por severidad y periodo; no se imputan meses ni valores." footer={<DataTable caption={`Datos de ${title}`} columns={columns} rows={model.rows} />}>
    <ChartLegend items={model.series} />
    <div className="h-[260px] min-w-0" aria-hidden="true"><ResponsiveContainer width="100%" height="100%" minWidth={0}><BarChart data={model.rows} margin={{ left: 10, right: 8 }}><CartesianGrid vertical={false} stroke="var(--border-subtle)" /><XAxis dataKey="month" {...axisProps} /><YAxis {...axisProps} tickFormatter={formatCompactCurrencyMXN} width={62} /><Tooltip content={<SeriesTooltip series={model.series} />} />{model.series.map((series) => <Bar key={series.key} dataKey={series.key} name={series.label} stackId="exposure" fill={series.color} barSize={32} isAnimationActive={false} />)}</BarChart></ResponsiveContainer></div>
  </ChartContainer>
}

export function SeverityChart({ items = [] }) {
  const model = severityCompositionViewModel(items)
  const title = 'Composición por severidad'
  if (!model.rows.length) return <ChartContainer title={title} subtitle="Composición del último periodo disponible dentro de la selección."><EmptyChartState chartTitle={title} message="Sin observaciones de severidad para los filtros activos." /></ChartContainer>
  return <ChartContainer title={title} subtitle={`Periodo: ${model.periodText}. No se acumulan periodos históricos.`} footer={<DataTable caption={`Datos de ${title}`} columns={[{ key: 'name', label: 'Nivel' }, { key: 'valueText', label: 'Importe', numeric: true }, { key: 'percentageText', label: 'Porcentaje', numeric: true }]} rows={model.rows} />}>
    <div className="relative h-[190px] min-w-0" aria-hidden="true"><ResponsiveContainer width="100%" height="100%" minWidth={0}><PieChart><Tooltip content={<CompositionTooltip />} /><Pie data={model.rows} dataKey="value" nameKey="name" innerRadius={46} outerRadius={66} paddingAngle={2} isAnimationActive={false}>{model.rows.map((entry) => <Cell key={entry.id} fill={entry.color} />)}</Pie></PieChart></ResponsiveContainer><div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center"><span className="text-[10px] text-muted">TOTAL</span><span className="font-mono text-[11px] font-semibold tabular-nums text-primary">{model.totalText}</span></div></div>
    <ul aria-label="Detalle de composición" className="space-y-2 text-xs">{model.rows.map((row) => <li key={row.id} className="grid grid-cols-[auto_1fr_auto] items-center gap-2"><span aria-hidden="true" className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: row.color }} /><span className="text-secondary">{row.name}</span><span className="font-mono tabular-nums">{row.valueText} · {row.percentageText}</span></li>)}</ul>
  </ChartContainer>
}

export function TemporalChart({ items = [], kpiCode, title, subtitle, unit, emptyMessage }) {
  const rows = temporalSeriesViewModel(items, kpiCode)
  if (!rows.length) return <ChartContainer title={title} subtitle={subtitle}><EmptyChartState chartTitle={title} message={emptyMessage} /></ChartContainer>
  const { data, keys } = segmentedLines(rows, 'value')
  const formatter = unit === 'días' ? formatDays : (value) => formatCount(value, unit)
  return <ChartContainer title={title} subtitle={subtitle} footer={<DataTable caption={`Datos de ${title}`} columns={[{ key: 'periodText', label: 'Periodo' }, { key: 'valueText', label: 'Valor', numeric: true }, { key: 'availability', label: 'Disponibilidad', render: (row) => row.availability === 'DISPONIBLE' ? 'Disponible' : 'No disponible' }]} rows={rows} />}>
    <div className="h-[260px] min-w-0" aria-hidden="true"><ResponsiveContainer width="100%" height="100%" minWidth={0}><LineChart data={data} margin={{ left: 4, right: 12 }}><CartesianGrid vertical={false} stroke="var(--border-subtle)" /><XAxis dataKey="month" {...axisProps} /><YAxis {...axisProps} tickFormatter={(value) => unit === 'días' ? `${value} d` : String(value)} width={48} /><Tooltip content={<SingleValueTooltip formatter={formatter} />} />{keys.map((key) => <Line key={key} type="linear" dataKey={key} stroke="var(--text-primary)" strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />)}</LineChart></ResponsiveContainer></div>
  </ChartContainer>
}

export function LitigationExposureChart({ items = [] }) {
  const model = litigationExposureViewModel(items)
  const title = 'Evolución de exposición litigiosa'
  if (!model.rows.length) return <ChartContainer title={title} subtitle="Total contractual derivado únicamente con la partición completa; severidad alta cuando está disponible."><EmptyChartState chartTitle={title} /></ChartContainer>
  const series = [
    ...(model.hasTotal ? [{ key: 'total', label: 'Total', color: 'var(--text-primary)' }] : []),
    ...(model.hasHigh ? [{ key: 'high', label: 'Alta', color: 'var(--critical)' }] : []),
  ]
  const { data, keys } = mergeSegmented(model.rows, series)
  const columns = [{ key: 'periodText', label: 'Periodo' }, ...(model.hasTotal ? [{ key: 'totalText', label: 'Total', numeric: true }] : []), ...(model.hasHigh ? [{ key: 'highText', label: 'Alta', numeric: true }] : [])]
  return <ChartContainer title={title} subtitle="Total disponible solo cuando el periodo contiene alto, medio y bajo; no se completan observaciones ausentes." footer={<DataTable caption={`Datos de ${title}`} columns={columns} rows={model.rows} />}>
    <ChartLegend items={series} />
    <div className="h-[260px] min-w-0" aria-hidden="true"><ResponsiveContainer width="100%" height="100%" minWidth={0}><LineChart data={data} margin={{ left: 10, right: 12 }}><CartesianGrid vertical={false} stroke="var(--border-subtle)" /><XAxis dataKey="month" {...axisProps} /><YAxis {...axisProps} tickFormatter={formatCompactCurrencyMXN} width={62} /><Tooltip content={<SeriesTooltip series={series} />} />{series.flatMap((seriesItem) => keys[seriesItem.key].map((key) => <Line key={key} type="linear" dataKey={key} name={seriesItem.label} stroke={seriesItem.color} strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />))}</LineChart></ResponsiveContainer></div>
  </ChartContainer>
}

export function ObservationTable({ items }) {
  return <DataTable caption="Observaciones de indicadores" columns={[{ key: 'name', label: 'Indicador' }, ...observationColumns, { key: 'entityText', label: 'Entidad' }, { key: 'as_of_date', label: 'Fecha de referencia', render: (row) => formatDate(row.as_of_date) }, { key: 'calculated_at', label: 'Fecha de cálculo', render: (row) => formatTimestamp(row.calculated_at) }]} rows={items} />
}
