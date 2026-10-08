import React, { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { useResource } from '../../api/useResource.js'
import { ApiError } from '../../api/errors.js'
import { PageHeader } from '../../components/layout.jsx'
import { Button, FilterBar, Input, Select } from '../../components/controls.jsx'
import { Badge, DataTable, KpiMetric, KpiRow, RiskBadge } from '../../components/data.jsx'
import { EmptyState, ErrorState, ResourceState } from '../../components/feedback.jsx'
import { formatDate, formatTimestamp, validDate } from '../../shared/formatters.js'
import { writeQuery } from '../../shared/query.js'
import { analyticsFilters, analyticsPaths, FILTER_VALIDATORS, PERIODS, RISKS } from './api.js'
import { adaptAnalysis, adaptKpis, KPI_CATALOG, latestSeries, ruleLabel } from './adapters.js'
import { AnalyticsChart, ChartContainer, EmptyChartState, ObservationTable, SeverityChart } from './charts.jsx'

const configs = {
  summary: { title: 'Resumen ejecutivo', risk: 'all', description: 'Indicadores, contexto y hallazgos persistidos del riesgo legal.' },
  contracts: { title: 'Riesgo contractual', risk: 'contractual', description: 'Vencimientos sin revisión y tiempo de ciclo contractual.' },
  litigation: { title: 'Gestión de litigios', risk: 'litigation', description: 'Exposición por severidad y nuevos litigios por periodo.' },
  trends: { title: 'Tendencias y riesgos', risk: 'all', description: 'Evaluaciones deterministas e histórico de hallazgos completados.' },
}
function ValidatedResource({ resource, adapt, children }) {
  return <ResourceState resource={resource}>{(data) => {
    try { return children(adapt(data)) } catch { return <ErrorState error={new ApiError(503)} onRetry={resource.reload} /> }
  }}</ResourceState>
}
function Filters({ filters, entities, setParams }) {
  const [draft, setDraft] = useState({ ...filters.raw, period: filters.period, risk_type: filters.risk_type, mode: filters.analysisMode })
  const [error, setError] = useState(false)
  useEffect(() => { setDraft({ ...filters.raw, period: filters.period, risk_type: filters.risk_type, mode: filters.analysisMode }); setError(false) }, [filters.canonical, filters.risk_type])
  function submit(event) {
    event.preventDefault()
    if (draft.period === 'custom' && (!validDate(draft.period_start) || !validDate(draft.period_end) || draft.period_start > draft.period_end)) { setError(true); return }
    const next = { risk_type: draft.risk_type, entity: draft.entity }
    if (draft.mode === 'historical') {
      next.period = draft.period; next.analytic_run_id = draft.analytic_run_id
      if (draft.period === 'custom') { next.period_start = draft.period_start; next.period_end = draft.period_end }
    }
    setError(false); setParams(writeQuery(next, FILTER_VALIDATORS))
  }
  const change = (key) => (event) => setDraft((value) => ({ ...value, [key]: event.target.value }))
  return <form onSubmit={submit}><FilterBar>
    <Select label="Análisis" value={draft.mode} onChange={change('mode')} options={[{ value: 'current', label: 'Vigente' }, { value: 'historical', label: 'Histórico' }]} />
    <Select label="Periodo" value={draft.period} onChange={change('period')} options={PERIODS} disabled={draft.mode === 'current'} />
    <Select label="Tipo de riesgo" value={draft.risk_type} onChange={change('risk_type')} options={RISKS} />
    <Select label="Entidad" value={draft.entity || ''} onChange={change('entity')} disabled={!entities.length} options={[{ value: '', label: entities.length ? 'Todas las aplicables' : 'No aplicable' }, ...entities.map((value) => ({ value, label: value }))]} />
    {draft.entity && !entities.includes(draft.entity) && <p className="text-xs text-secondary">Entidad seleccionada: {draft.entity}. Sin opciones disponibles en esta lectura.</p>}
    {draft.mode === 'historical' && draft.period === 'custom' && <><Input compact label="Desde" type="date" value={draft.period_start || ''} onChange={change('period_start')} /><Input compact label="Hasta" type="date" value={draft.period_end || ''} onChange={change('period_end')} /></>}
    <Button type="submit" variant="secondary">Aplicar filtros</Button>
    {draft.analytic_run_id && draft.mode === 'historical' && <Button variant="ghost" onClick={() => setDraft((value) => ({ ...value, analytic_run_id: undefined }))}>Quitar selección de ejecución</Button>}
  </FilterBar>{error && <p role="alert" className="mt-2 text-xs text-[var(--critical)]">El rango personalizado requiere fechas válidas y ordenadas.</p>}</form>
}
function MetricGroup({ data }) {
  const codes = Object.keys(KPI_CATALOG).filter((code) => KPI_CATALOG[code].core)
  const present = codes.filter((code) => data.items.some((item) => item.kpi_code === code))
  return <section aria-label="Indicadores principales"><KpiRow>{present.map((code) => {
    const rows = latestSeries(data.items, code)
    // Una dimensión no equivale a un total; mantener cada observación separada.
    if (rows.length === 1) return <KpiMetric key={code} label={rows[0].name} displayValue={rows[0].displayValue} shortContext={rows[0].unit} previousContext={rows[0].periodText} delta={<span className="text-muted">Cálculo: {formatTimestamp(rows[0].calculated_at)}</span>} />
    return <div key={code} className="min-w-0"><h2 className="h-[var(--kpi-title-slot-height)] text-xs font-medium text-secondary">{KPI_CATALOG[code].name}</h2><ul className="space-y-3">{rows.map((row) => <li key={row.id}><p className="text-[11px] text-secondary">{row.dimensionsText}</p><p className="font-mono text-sm tabular-nums">{row.displayValue} {row.unit}</p><p className="text-[11px] text-muted">{row.periodText}</p><p className="text-[11px] text-muted">Cálculo: {formatTimestamp(row.calculated_at)}</p></li>)}</ul></div>
  })}</KpiRow>{!present.length && <EmptyState title="Sin indicadores principales" />}</section>
}
function FindingList({ run, principal }) {
  if (!run.findings.length) return <EmptyState title="Sin hallazgos" message="Esta ejecución completada no produjo hallazgos bajo los filtros activos." />
  return <div className="space-y-4">{run.findings.map((finding) => <article key={finding.id} className="space-y-3 rounded-panel border border-border bg-surface p-5"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold">{finding.name}</h3><p className="text-xs text-secondary">{finding.dimensionsText}</p></div>{finding.dimensions.Nivel_Severidad && <RiskBadge level={{ alto: 'Alto', medio: 'Medio', bajo: 'Bajo' }[finding.dimensions.Nivel_Severidad] || finding.dimensions.Nivel_Severidad} />}</div><dl className="grid grid-cols-1 gap-3 text-xs md:grid-cols-3"><div><dt className="text-secondary">Actual</dt><dd className="font-mono">{finding.currentText} {finding.unit}</dd></div><div><dt className="text-secondary">Referencia</dt><dd className="font-mono">{finding.referenceText} {finding.unit}</dd></div><div><dt className="text-secondary">Variación absoluta</dt><dd className="font-mono">{finding.variationText} {finding.unit}</dd></div></dl><p className="text-xs text-secondary">{formatDate(finding.period_start)} – {formatDate(finding.period_end)}</p><p className="text-xs">{finding.ruleText}</p><p className="text-[13px]">{finding.description}</p>{finding.recurrent_pattern && <p className="text-xs">Patrón recurrente: {finding.recurrent_pattern}</p>}<p className="text-[11px] text-muted">Generado: {formatTimestamp(finding.created_at)}</p>{finding.contextReferenceId && can(principal, 'document.query') && <Link className="inline-block text-xs underline" to={`/consulta?context_reference_id=${encodeURIComponent(finding.contextReferenceId)}`}>Verificar evidencia documental</Link>}</article>)}</div>
}
function AnalysisContent({ data, mode, principal, selectRun }) {
  return <section className="space-y-5" aria-label="Análisis persistido"><div className="flex flex-wrap items-center gap-3"><h2 className="text-base font-semibold">{mode === 'current' ? 'Análisis vigente' : 'Análisis histórico'}</h2><Badge>{data.alert_count} alertas</Badge></div>{!data.items.length && <EmptyState title="Sin análisis completados" message="No se sustituyen por ejecuciones iniciadas o fallidas." />}{data.items.map((run) => <section key={run.analytic_run_id} className="space-y-5"><div><h3 className="text-sm font-semibold">Ejecución completada</h3><p className="text-xs text-secondary">{formatTimestamp(run.completed_at)}</p><p className="text-xs text-secondary">Ventana: {formatTimestamp(run.window_start)} – {formatTimestamp(run.window_end)}</p><p className="mt-3 text-[13px]">{run.executive_summary}</p>{mode === 'historical' && <Button variant="ghost" onClick={() => selectRun(run.analytic_run_id)}>Seleccionar ejecución</Button>}</div><DataTable caption="Evaluaciones persistidas" rows={run.evaluations} columns={[
    { key: 'name', label: 'Indicador' }, { key: 'dimensionsText', label: 'Dimensiones' },
    { key: 'evaluation_period', label: 'Periodo', render: (row) => formatDate(row.evaluation_period) },
    { key: 'outcomeText', label: 'Resultado' }, { key: 'currentText', label: 'Actual', numeric: true },
    { key: 'referenceText', label: 'Referencia', numeric: true }, { key: 'absoluteText', label: 'Variación absoluta', numeric: true },
    { key: 'percentageText', label: 'Variación porcentual', numeric: true }, { key: 'trendText', label: 'Tendencia' },
    { key: 'signal_detected', label: 'Señal', render: (row) => row.signal_detected ? 'Sí' : 'No' },
    { key: 'recurrence_month_count', label: 'Meses recurrentes' }, { key: 'rules_applied', label: 'Reglas', render: (row) => row.rules_applied.map(ruleLabel).join(' · ') },
    { key: 'not_evaluated_reason', label: 'Motivo', render: (row) => row.not_evaluated_reason ? 'Histórico comparable insuficiente' : '—' },
  ]} /><FindingList run={run} principal={principal} /></section>)}</section>
}
export function AnalyticsPage({ view = 'summary' }) {
  const config = configs[view]
  const [params, setParams] = useSearchParams(); const { principal } = useSession()
  const filters = analyticsFilters(params.toString(), config.risk); const paths = analyticsPaths(filters)
  const kpis = useResource(paths.kpis); const analysis = useResource(paths.analysis)
  const [corrected, setCorrected] = useState(false)
  useEffect(() => { if (filters.corrected) { setCorrected(true); setParams(filters.canonical, { replace: true }) } }, [filters.canonical, filters.corrected, setParams])
  let entities = []
  try { if (kpis.data) entities = [...new Set(adaptKpis(kpis.data).items.filter((row) => row.entity_filter_applicable && typeof row.dimensions.entity === 'string').map((row) => row.dimensions.entity))].sort() } catch { /* El estado de lectura informa el contrato inválido sin exponer el payload. */ }
  const changeRun = (id) => setParams(writeQuery({ ...filters.raw, analytic_run_id: id }, FILTER_VALIDATORS))
  return <div className="space-y-8"><PageHeader title={config.title} description={config.description} />{corrected && <p role="status" className="text-xs text-secondary">Se retiraron filtros no válidos de la dirección.</p>}<Filters filters={filters} entities={entities} setParams={setParams} /><p className="text-xs text-secondary">{filters.analysisMode === 'current' ? 'Vigente: último análisis completado. Los indicadores muestran los últimos seis meses completos disponibles.' : 'Histórico: selección de periodos naturales o ejecución completada.'}</p><ValidatedResource resource={kpis} adapt={adaptKpis}>{(data) => <div className="space-y-8"><MetricGroup data={data} />{view === 'summary' && <div className="grid grid-cols-1 gap-6 lg:grid-cols-12"><div className="min-w-0 lg:col-span-8"><AnalyticsChart items={data.items} kpiCode="KPI-LI-01" kind="bar" title="Evolución de exposición acumulada" subtitle="Unidad: importe. Solo periodos recibidos; no se imputan meses ni valores." emptyTitle="Sin datos disponibles" emptyMessage="Aún no existen observaciones para los filtros seleccionados." /></div><div className="min-w-0 lg:col-span-4"><SeverityChart items={data.items} title="Composición por severidad" subtitle="Distribución según clasificación de severidad recibida." emptyTitle="Sin datos disponibles" emptyMessage="Sin observaciones de severidad para los filtros activos." /></div></div>}{view === 'contracts' && <AnalyticsChart items={data.items} kpiCode="KPI-RC-01" kind="line" title="Tiempo de ciclo del contrato" subtitle="Unidad: días. Solo periodos recibidos; no se imputan meses ni valores." emptyTitle="Sin datos disponibles" emptyMessage="No existen observaciones contractuales para los filtros seleccionados." />}{view === 'litigation' && <div className="space-y-6"><AnalyticsChart items={data.items} kpiCode="KPI-LI-01" kind="line" title="Exposición total por litigios activos" subtitle="Unidad: importe. Solo periodos recibidos; no se imputan meses ni valores." emptyTitle="Sin datos disponibles" emptyMessage="Aún no existen observaciones para los filtros seleccionados." /><AnalyticsChart items={data.items} kpiCode="KPI-LI-05" kind="bar" title="Nuevos litigios por periodo" subtitle="Unidad: litigios. Solo periodos recibidos; no se imputan meses ni valores." emptyTitle="Sin datos disponibles" emptyMessage="Aún no existen observaciones para los filtros seleccionados." /></div>}{view === 'trends' && (() => {
    const eligibleCodes = Object.keys(KPI_CATALOG).filter((code) => KPI_CATALOG[code].core && code !== 'KPI-RC-03')
    const presentCodes = eligibleCodes.filter((code) => data.items.some((item) => item.kpi_code === code))
    if (!presentCodes.length) {
      return <ChartContainer title="Tendencias de indicadores clave" subtitle="Evolución temporal de indicadores núcleo evaluados."><EmptyChartState chartTitle="Tendencias de indicadores clave" title="Sin tendencias disponibles" message="Aún no existen suficientes observaciones para mostrar una evolución temporal." /></ChartContainer>
    }
    return <div className="space-y-6">{presentCodes.map((code) => <AnalyticsChart key={code} items={data.items} kpiCode={code} kind="line" />)}</div>
  })()}<section className="space-y-3"><h2 className="text-base font-semibold">Observaciones recibidas</h2><ObservationTable items={data.items.filter((row) => KPI_CATALOG[row.kpi_code].core)} /></section><section className="space-y-3"><h2 className="text-base font-semibold">Contexto operativo</h2><p className="text-xs text-secondary">Estos indicadores no generan señales ejecutivas.</p><ObservationTable items={data.items.filter((row) => !KPI_CATALOG[row.kpi_code].core)} /></section></div>}</ValidatedResource><ValidatedResource resource={analysis} adapt={adaptAnalysis}>{(data) => <AnalysisContent data={data} mode={filters.analysisMode} principal={principal} selectRun={changeRun} />}</ValidatedResource></div>
}
export const SummaryPage = () => <AnalyticsPage view="summary" />
export const ContractsPage = () => <AnalyticsPage view="contracts" />
export const LitigationPage = () => <AnalyticsPage view="litigation" />
export const TrendsPage = () => <AnalyticsPage view="trends" />
