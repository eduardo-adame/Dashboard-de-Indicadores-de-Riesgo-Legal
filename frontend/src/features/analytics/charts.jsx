import React from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { DataTable } from '../../components/data.jsx'
import { formatDate, formatTimestamp } from '../../shared/formatters.js'
import { chartSeries, KPI_CATALOG } from './adapters.js'

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

export function ChartContainer({ title, subtitle, footer, children, className = '' }) {
  return (
    <figure className={`min-w-0 space-y-3 rounded-panel border border-border bg-surface p-5 ${className}`}>
      <figcaption className="text-sm font-semibold">{title}</figcaption>
      {subtitle && <p className="text-xs text-secondary">{subtitle}</p>}
      {children}
      {footer}
    </figure>
  )
}

export function EmptyChartState({
  chartTitle = '',
  title = 'Sin datos disponibles',
  message = 'Aún no existen observaciones para los filtros seleccionados.',
  className = '',
}) {
  const ariaLabel = chartTitle ? `${chartTitle}: ${title.toLowerCase()}` : title
  return (
    <div
      role="region"
      aria-label={ariaLabel}
      className={`flex h-[260px] min-w-0 flex-col items-center justify-center rounded-panel border border-dashed border-border bg-secondary/30 p-6 text-center ${className}`}
    >
      <p className="text-sm font-medium text-secondary">{title}</p>
      {message && <p className="mt-1 max-w-sm text-xs text-muted">{message}</p>}
    </div>
  )
}

export function AnalyticsChart({
  items = [],
  kpiCode,
  kind = 'bar',
  title: customTitle,
  subtitle: customSubtitle,
  emptyTitle = 'Sin datos disponibles',
  emptyMessage = 'Aún no existen observaciones para los filtros seleccionados.',
}) {
  const series = chartSeries(items, kpiCode)
  const meta = KPI_CATALOG[kpiCode]
  const defaultTitle = meta?.name || kpiCode

  if (!series.length) {
    const title = customTitle || defaultTitle
    const subtitle = customSubtitle || (meta ? `Unidad: ${meta.unit}. Solo periodos recibidos; no se imputan meses ni valores.` : undefined)
    return (
      <ChartContainer title={title} subtitle={subtitle}>
        <EmptyChartState chartTitle={title} title={emptyTitle} message={emptyMessage} />
      </ChartContainer>
    )
  }

  return (
    <div className="grid grid-cols-1 gap-6">
      {series.map((item) => {
        const { data, keys } = contiguousLines(item.points)
        const severity = { alto: 'var(--critical)', medio: 'var(--warning)', bajo: 'var(--success)' }[item.severity]
        const color = severity || 'var(--text-primary)'
        const title = customTitle && series.length === 1 ? customTitle : `${item.name} · ${item.dimensionsText}`
        const subtitle = customSubtitle || `Unidad: ${item.unit}. Solo periodos recibidos; no se imputan meses ni valores.`
        return (
          <ChartContainer
            key={item.id}
            title={title}
            subtitle={subtitle}
            footer={
              <>
                <p className="text-[11px] text-muted">La tabla conserva la precisión original. Los valores no representables con seguridad se omiten de la gráfica.</p>
                <DataTable caption={`Datos de ${title}`} columns={columns} rows={item.points} />
              </>
            }
          >
            <div className="h-[260px] min-w-0" aria-hidden="true">
              <ResponsiveContainer width="100%" height="100%" minWidth={0}>
                {kind === 'line' ? (
                  <LineChart data={data}>
                    <CartesianGrid vertical={false} stroke="var(--border-subtle)" />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip content={<ValueTooltip />} />
                    {keys.map((key) => (
                      <Line key={key} type="linear" dataKey={key} stroke={color} strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />
                    ))}
                  </LineChart>
                ) : (
                  <BarChart data={item.points}>
                    <CartesianGrid vertical={false} stroke="var(--border-subtle)" />
                    <XAxis dataKey="month" tick={{ fontSize: 11 }} />
                    <YAxis tick={{ fontSize: 11 }} />
                    <Tooltip content={<ValueTooltip />} />
                    <Bar dataKey="chartValue" fill={color} barSize={32} isAnimationActive={false} />
                  </BarChart>
                )}
              </ResponsiveContainer>
            </div>
          </ChartContainer>
        )
      })}
    </div>
  )
}

export function SeverityChart({
  items = [],
  kpiCode = 'KPI-LI-01',
  title = 'Composición por severidad',
  subtitle = 'Distribución según clasificación de severidad recibida.',
  emptyTitle = 'Sin datos disponibles',
  emptyMessage = 'Sin observaciones de severidad para los filtros activos.',
}) {
  const severityRows = items.filter(
    (item) =>
      item.kpi_code === kpiCode &&
      item.severity &&
      item.availability === 'DISPONIBLE' &&
      item.value !== null &&
      Number.isFinite(Number(item.value))
  )

  if (!severityRows.length) {
    return (
      <ChartContainer title={title} subtitle={subtitle}>
        <EmptyChartState chartTitle={title} title={emptyTitle} message={emptyMessage} />
      </ChartContainer>
    )
  }

  const totals = { alto: 0, medio: 0, bajo: 0 }
  let hasAny = false
  for (const row of severityRows) {
    const sev = row.severity
    if (sev in totals) {
      totals[sev] += Number(row.value)
      hasAny = true
    }
  }

  if (!hasAny) {
    return (
      <ChartContainer title={title} subtitle={subtitle}>
        <EmptyChartState chartTitle={title} title={emptyTitle} message={emptyMessage} />
      </ChartContainer>
    )
  }

  const data = [
    { name: 'Alto', value: totals.alto, color: 'var(--critical)' },
    { name: 'Medio', value: totals.medio, color: 'var(--warning)' },
    { name: 'Bajo', value: totals.bajo, color: 'var(--success)' },
  ].filter((slice) => slice.value > 0)

  return (
    <ChartContainer title={title} subtitle={subtitle}>
      <div className="h-[260px] min-w-0" aria-hidden="true">
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          <PieChart>
            <Tooltip />
            <Pie
              data={data}
              dataKey="value"
              nameKey="name"
              innerRadius={46}
              outerRadius={66}
              paddingAngle={2}
              isAnimationActive={false}
            >
              {data.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.color} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
      </div>
      <div className="flex flex-wrap justify-center gap-4 text-xs">
        {data.map((item) => (
          <span key={item.name} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: item.color }} />
            <span className="text-secondary">{item.name}:</span>
            <span className="font-mono tabular-nums font-medium">{item.value}</span>
          </span>
        ))}
      </div>
    </ChartContainer>
  )
}

export function ObservationTable({ items }) {
  return <DataTable caption="Observaciones de indicadores" columns={[{ key: 'name', label: 'Indicador' }, ...columns, { key: 'entityText', label: 'Entidad' }, { key: 'as_of_date', label: 'Fecha de referencia', render: (row) => formatDate(row.as_of_date) }, { key: 'calculated_at', label: 'Fecha de cálculo', render: (row) => formatTimestamp(row.calculated_at) }]} rows={items} />
}
