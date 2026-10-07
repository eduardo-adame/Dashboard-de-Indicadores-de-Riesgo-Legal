import React from 'react'
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { DataTable } from '../../components/data.jsx'
import { formatDate, formatTimestamp } from '../../shared/formatters.js'
import { chartSeries } from './adapters.js'

const columns = [
  { key: 'periodText', label: 'Periodo' }, { key: 'displayValue', label: 'Valor', numeric: true },
  { key: 'unit', label: 'Unidad' }, { key: 'dimensionsText', label: 'Dimensiones' },
  { key: 'availability', label: 'Disponibilidad', render: (row) => row.availability === 'DISPONIBLE' ? 'Disponible' : 'No disponible' },
]
function contiguousLines(points) {
  let segment = 0; let previous = null
  const data = points.map((point) => {
    const month = Number(point.month.slice(0, 4)) * 12 + Number(point.month.slice(5, 7))
    if (previous !== null && month !== previous + 1) segment++
    previous = month
    return { ...point, [`value${segment}`]: point.chartValue }
  })
  return { data, keys: Array.from({ length: segment + 1 }, (_, index) => `value${index}`) }
}
function ValueTooltip({ active, payload }) {
  const row = payload?.find((item) => item.payload)?.payload
  if (!active || !row) return null
  return <div className="rounded-control border border-border bg-surface p-3 text-xs shadow-subtle"><p>{row.periodText}</p><p className="font-mono tabular-nums">{row.displayValue} {row.unit}</p><p>{row.dimensionsText}</p></div>
}
export function AnalyticsChart({ items, kpiCode, kind = 'bar' }) {
  const series = chartSeries(items, kpiCode)
  if (!series.length) return null
  return <div className="grid grid-cols-1 gap-6">{series.map((item) => {
    const { data, keys } = contiguousLines(item.points)
    const severity = { alto: 'var(--critical)', medio: 'var(--warning)', bajo: 'var(--success)' }[item.dimensions.Nivel_Severidad]
    const color = severity || 'var(--text-primary)'
    const title = `${item.name} · ${item.dimensionsText}`
    return <figure key={item.id} className="min-w-0 space-y-3 rounded-panel border border-border bg-surface p-5"><figcaption className="text-sm font-semibold">{title}</figcaption><p className="text-xs text-secondary">Unidad: {item.unit}. Solo periodos recibidos; no se imputan meses ni valores.</p><div className="h-[260px] min-w-0" aria-hidden="true"><ResponsiveContainer width="100%" height="100%" minWidth={0}>
      {kind === 'line' ? <LineChart data={data}><CartesianGrid vertical={false} stroke="var(--border-subtle)" /><XAxis dataKey="month" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip content={<ValueTooltip />} />{keys.map((key) => <Line key={key} type="linear" dataKey={key} stroke={color} strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />)}</LineChart>
        : <BarChart data={item.points}><CartesianGrid vertical={false} stroke="var(--border-subtle)" /><XAxis dataKey="month" tick={{ fontSize: 11 }} /><YAxis tick={{ fontSize: 11 }} /><Tooltip content={<ValueTooltip />} /><Bar dataKey="chartValue" fill={color} barSize={32} isAnimationActive={false} /></BarChart>}
    </ResponsiveContainer></div><p className="text-[11px] text-muted">La tabla conserva la precisión original. Los valores no representables con seguridad se omiten de la gráfica.</p><DataTable caption={`Datos de ${title}`} columns={columns} rows={item.points} /></figure>
  })}</div>
}
export function ObservationTable({ items }) {
  return <DataTable caption="Observaciones de indicadores" columns={[{ key: 'name', label: 'Indicador' }, ...columns, { key: 'entityText', label: 'Entidad' }, { key: 'as_of_date', label: 'Fecha de referencia', render: (row) => formatDate(row.as_of_date) }, { key: 'calculated_at', label: 'Fecha de cálculo', render: (row) => formatTimestamp(row.calculated_at) }]} rows={items} />
}
