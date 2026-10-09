import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { ApiError } from '../src/api/errors.js'
import { AnalyticsPage } from '../src/features/analytics/pages.jsx'
import { SeverityChart, TemporalChart } from '../src/features/analytics/charts.jsx'
import { adaptKpis, KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { buildRegistry } from '../src/routing/registry.jsx'
import * as analytics from '../src/features/analytics/routes.jsx'

// Aislar el render SVG: la geometría real se verifica en la revisión visual.
vi.mock('recharts', () => {
  const Container = ({ children }) => <div>{children}</div>
  const Graph = ({ data, children }) => <div data-testid="chart" data-points={JSON.stringify(data)}>{children}</div>
  const Plot = ({ data, dataKey, connectNulls, type }) => <span data-testid="plot" data-points={data ? JSON.stringify(data) : undefined} data-key={dataKey} data-connect={String(connectNulls)} data-type={type} />
  return { ResponsiveContainer: Container, BarChart: Graph, LineChart: Graph, PieChart: Graph, Pie: Plot, Cell: () => null, CartesianGrid: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null, Line: Plot, Bar: Plot }
})
const runId = '11111111-1111-4111-8111-111111111111'
const contextId = '44444444-4444-4444-8444-444444444444'
function observation(code = 'KPI-RC-03', overrides = {}) {
  return { kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO', period_start: '2030-01-01', period_end: '2030-01-31', as_of_date: '2030-01-31', calculated_at: '2030-02-01T00:00:00Z', dimensions: {}, entity_filter_applicable: false, availability: 'DISPONIBLE', value: '0', ...overrides }
}
const kpiPage = (items = [observation()]) => ({ period_start: '2029-08-01', period_end: '2030-01-31', period_reference: '2030-01-01', filters: { period: 'last_6_months', period_start: null, period_end: null, risk_type: 'all', entity: null }, items })
function analysisPage(withFinding = false) {
  const evaluationId = '22222222-2222-4222-8222-222222222222'; const findingId = '33333333-3333-4333-8333-333333333333'
  return { current_analytic_run_id: runId, alert_count: withFinding ? 1 : 0, items: [{ analytic_run_id: runId, completed_at: '2030-02-01T00:00:00Z', window_start: '2029-08-01T00:00:00Z', window_end: '2030-01-31T23:59:59Z', executive_summary: withFinding ? 'Resumen sintético persistido.' : 'Análisis completado sin hallazgos.', evaluations: [{ id: evaluationId, analytic_run_id: runId, kpi_code: 'KPI-RC-03', canonical_dimensions_key: {}, evaluation_period: '2030-01-01', outcome: 'EVALUATED', signal_detected: withFinding, trend_direction: null, recurrence_month_count: 3, current_value: withFinding ? '2' : '0', reference_value: '0', absolute_variation: withFinding ? '2' : '0', percentage_variation: null, rules_applied: ['RC03_APPEARANCE_OR_INCREASE'], not_evaluated_reason: null, entity_filter_applicable: false }], findings: withFinding ? [{ id: findingId, analytic_run_id: runId, proactive_evaluation_id: evaluationId, kpi_code: 'KPI-RC-03', dimensions: {}, period_start: '2029-12-01', period_end: '2030-01-31', current_value: '2', reference_value: '0', variation: '2', triggered_rule: 'RC03_APPEARANCE_OR_INCREASE', recurrent_pattern: '3/4', description: 'Descripción sintética sustentada.', created_at: '2030-02-01T00:00:00Z', entity_filter_applicable: false }] : [], context_references: withFinding ? [{ id: contextId, finding_id: findingId, kpi_code: 'KPI-RC-03', dimensions: {}, period_start: '2029-12-01', period_end: '2030-01-31', context_identifiers: {}, operation_id: runId, correlation_id: runId }] : [] }] }
}
function LocationControls() {
  const location = useLocation(); const navigate = useNavigate()
  return <><output aria-label="Dirección actual">{location.search}</output><button onClick={() => navigate(-1)}>Atrás de prueba</button><button onClick={() => navigate(1)}>Adelante de prueba</button></>
}
function fixture({ search = '', view = 'summary', kpis = kpiPage(), analysis = analysisPage(), error = null, pending = false, roles = ['JURIDICO'] } = {}) {
  const client = { acquireRead: vi.fn((path) => ({ promise: pending ? new Promise(() => {}) : error ? Promise.reject(new ApiError(error)) : Promise.resolve(path.startsWith('/dashboard/kpis') ? kpis : analysis), release: vi.fn() })), abortAll: vi.fn() }
  const snapshot = { status: 'authenticated', principal: { id: runId, username: 'cuenta-sintetica', roles }, generation: 1 }
  const manager = { client, getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn() }
  render(<SessionProvider manager={manager}><MemoryRouter initialEntries={[`/${search}`]}><AnalyticsPage view={view} /><LocationControls /></MemoryRouter></SessionProvider>)
  return client
}
describe('SRS_REQUIRED: cuatro vistas analíticas', () => {
  it.each([['summary', 'Resumen ejecutivo'], ['contracts', 'Riesgo contractual'], ['litigation', 'Gestión de litigios'], ['trends', 'Tendencias y riesgos']])('encabeza %s y usa API física', async (view, title) => {
    const client = fixture({ view }); expect(screen.getByRole('heading', { name: title })).toBeVisible()
    expect(await screen.findByText('Análisis completado sin hallazgos.')).toBeVisible()
    expect(client.acquireRead.mock.calls.every(([path]) => /^\/dashboard\/(kpis|analysis)\?/.test(path))).toBe(true)
  })
  it('muestra KPI/contextos, diferencia cero de no disponible y limita moneda a exposición', async () => {
    fixture({ kpis: kpiPage(Object.keys(KPI_CATALOG).map((code) => observation(code, code === 'KPI-RC-01' ? { availability: 'NO_DISPONIBLE', value: null } : code === 'KPI-LI-01' ? { dimensions: { nivel_severidad: 'alto' }, value: '17.50' } : {}))) })
    expect(await screen.findByRole('heading', { name: 'Contexto operativo' })).toBeVisible()
    expect(screen.getAllByText('No disponible').length).toBeGreaterThan(0)
    expect(screen.getAllByText('0').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/MXN|\$/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('No aplicable').length).toBeGreaterThan(0)
    expect(document.body.textContent).not.toContain('KPI-RC-03')
  })
  it('mantiene geometría KPI estable y conserva el decimal dimensional en las tablas', async () => {
    fixture({ kpis: kpiPage([
      observation('KPI-LI-01', { dimensions: { nivel_severidad: 'alto' }, value: '12.5000', calculated_at: '2030-02-01T01:02:03Z' }),
      observation('KPI-LI-01', { dimensions: { nivel_severidad: 'medio' }, value: '0.000000000000000001', calculated_at: '2030-02-02T04:05:06Z' }),
    ]) })
    expect((await screen.findAllByText('12.5000')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('0.000000000000000001').length).toBeGreaterThan(0)
    expect(screen.getByText('Por dimensión')).toBeVisible()
    expect(screen.getByText('Desglose en gráfica y tabla')).toBeVisible()
  })
  it('muestra backend alert_count y contexto RF048 sin seleccionar documentos', async () => {
    fixture({ analysis: analysisPage(true) })
    expect(await screen.findByText('1 alertas')).toBeVisible()
    const link = screen.getByRole('link', { name: 'Verificar evidencia documental' })
    expect(link).toHaveAttribute('href', `/consulta?context_reference_id=${contextId}`)
    expect(screen.getByText('Resumen sintético persistido.')).toBeVisible()
    expect(screen.getByText('Patrón recurrente: 3/4')).toBeVisible()
  })
  it('normaliza nivel_severidad canónico del hallazgo y muestra su badge', async () => {
    const payload = analysisPage(true)
    payload.items[0].findings[0].dimensions = { nivel_severidad: 'alto' }
    fixture({ view: 'trends', analysis: payload })
    expect(await screen.findByText('Alto')).toBeVisible()
  })
  it('no presenta link de contexto sin document.query', async () => {
    fixture({ analysis: analysisPage(true), roles: [] })
    await screen.findByText('1 alertas'); expect(screen.queryByRole('link', { name: 'Verificar evidencia documental' })).not.toBeInTheDocument()
  })
  it('vigente no envía period al análisis y conserva default KPI', async () => {
    const client = fixture(); await screen.findByText('0 alertas')
    expect(client.acquireRead.mock.calls.some(([path]) => path === '/dashboard/analysis?risk_type=all')).toBe(true)
    expect(screen.getByLabelText('Periodo')).toBeDisabled()
    expect(screen.getAllByRole('columnheader', { name: 'Fecha de cálculo' })).toHaveLength(1)
    expect(screen.getByText(/Cálculo:.*UTC/)).toBeVisible()
  })
  it('aplica filtros históricos y back/forward restaura selección', async () => {
    const client = fixture(); await screen.findByText('0 alertas')
    await userEvent.selectOptions(screen.getByLabelText('Análisis'), 'historical')
    await userEvent.selectOptions(screen.getByLabelText('Periodo'), 'last_3_months')
    await userEvent.selectOptions(screen.getByLabelText('Tipo de riesgo'), 'compliance')
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar filtros' }))
    await waitFor(() => expect(client.acquireRead.mock.calls.some(([path]) => path === '/dashboard/analysis?period=last_3_months&risk_type=compliance')).toBe(true))
    await userEvent.click(screen.getByRole('button', { name: 'Atrás de prueba' }))
    await waitFor(() => expect(screen.getByLabelText('Análisis')).toHaveValue('current'))
    await userEvent.click(screen.getByRole('button', { name: 'Adelante de prueba' }))
    await waitFor(() => expect(screen.getByLabelText('Periodo')).toHaveValue('last_3_months'))
  })
  it('fechas custom inválidas no disparan lectura histórica', async () => {
    const client = fixture(); await screen.findByText('0 alertas'); const before = client.acquireRead.mock.calls.length
    await userEvent.selectOptions(screen.getByLabelText('Análisis'), 'historical')
    await userEvent.selectOptions(screen.getByLabelText('Periodo'), 'custom')
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar filtros' }))
    expect(screen.getByRole('alert')).toHaveTextContent('fechas válidas y ordenadas')
    expect(client.acquireRead).toHaveBeenCalledTimes(before)
  })
  it('retira parámetros desconocidos con aviso seguro y sin usarlos en API', async () => {
    const client = fixture({ search: '?query=contenido-no-permitido&risk_type=alto' })
    await screen.findByText('Se retiraron filtros no válidos de la dirección.')
    expect(screen.getByLabelText('Dirección actual')).not.toHaveTextContent('query')
    expect(client.acquireRead.mock.calls.every(([path]) => !path.includes('contenido-no-permitido'))).toBe(true)
  })
  it('conserva lectura vacía sin convertirla en riesgo', async () => {
    fixture({ kpis: kpiPage([]), analysis: { current_analytic_run_id: null, alert_count: 0, items: [] } })
    expect(await screen.findByText('Sin análisis completados')).toBeVisible()
    expect(screen.getByText('Sin indicadores principales')).toBeVisible(); expect(screen.getByText('0 alertas')).toBeVisible()
  })
  it('loading no muestra valores cero provisionales', () => { fixture({ pending: true }); expect(screen.getAllByText('Cargando…')).toHaveLength(2); expect(screen.queryByText('0')).not.toBeInTheDocument() })
  it.each([401, 403, 422, 503])('muestra error %i sin contenido previo', async (error) => {
    fixture({ error }); const alerts = await screen.findAllByRole('alert'); expect(alerts).toHaveLength(2)
    expect(screen.queryByText('Análisis completado sin hallazgos.')).not.toBeInTheDocument()
    if (error === 403) expect(alerts[0]).toHaveTextContent('Sin autorización')
  })
  it('incluye alternativa tabular y rompe líneas donde faltan meses sin inventarlos', () => {
    const items = adaptKpis(kpiPage([observation('KPI-LI-05', { value: '3' }), observation('KPI-LI-05', { value: '8', period_start: '2030-03-01', period_end: '2030-03-31' })])).items
    render(<TemporalChart items={items} kpiCode="KPI-LI-05" title="Serie temporal" subtitle="Prueba" unit="litigios" />)
    expect(screen.getByRole('table')).toBeInTheDocument()
    const points = JSON.parse(screen.getByTestId('chart').getAttribute('data-points'))
    expect(points.map((row) => row.month)).toEqual(['2030-01', '2030-03']); expect(screen.getAllByTestId('plot')).toHaveLength(2)
    expect(screen.getAllByTestId('plot').every((plot) => plot.dataset.connect === 'false' && plot.dataset.type === 'linear')).toBe(true)
  })
  it('registry descubre cuatro rutas distintas protegidas', () => {
    const registry = buildRegistry({ analytics }); expect(registry.routes).toHaveLength(4)
    expect(registry.routes.every((route) => route.capability === 'dashboard.read')).toBe(true)
    expect(registry.navigation.map((item) => item.path)).toEqual(['/', '/contratos', '/litigios', '/tendencias'])
  })
  it('resumen ejecutivo con datasets vacíos preserva bloque 8/4 con ChartContainer, títulos y EmptyChartState sin datos inventados', async () => {
    fixture({ view: 'summary', kpis: kpiPage([]), analysis: { current_analytic_run_id: null, alert_count: 0, items: [] } })
    expect(await screen.findByText('Sin indicadores principales')).toBeVisible()
    expect(screen.getByText('Evolución de exposición acumulada')).toBeVisible()
    expect(screen.getByText('Composición por severidad')).toBeVisible()
    expect(screen.getAllByText('Sin datos disponibles')).toHaveLength(2)
    expect(screen.getByLabelText('Evolución de exposición acumulada: sin datos disponibles')).toBeInTheDocument()
    expect(screen.getByLabelText('Composición por severidad: sin datos disponibles')).toBeInTheDocument()
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
    expect(screen.queryByText(/0 %/)).not.toBeInTheDocument()
  })
  it('compone severidades desde la dimensión canónica del DTO y no muestra estado vacío', () => {
    const items = adaptKpis(kpiPage([
      observation('KPI-LI-01', { dimensions: { nivel_severidad: 'alto' }, value: '200000' }),
      observation('KPI-LI-01', { dimensions: { nivel_severidad: 'medio' }, value: '90000' }),
      observation('KPI-LI-01', { dimensions: { nivel_severidad: 'bajo' }, value: '25000' }),
      observation('KPI-CN-03', { dimensions: { area: 'Finanzas', nivel_severidad: 'medio' }, value: '1' }),
    ])).items

    render(<SeverityChart items={items} />)

    expect(screen.queryByLabelText('Composición por severidad: sin datos disponibles')).not.toBeInTheDocument()
    expect(screen.getAllByText('Alto').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Medio').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Bajo').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/\$200,000 MXN/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/63\.49 %/).length).toBeGreaterThan(0)
    expect(screen.getAllByText('$315,000 MXN').length).toBeGreaterThan(0)
    expect(JSON.parse(screen.getByTestId('plot').dataset.points).map(({ name, value }) => ({ name, value }))).toEqual([
      { name: 'Alto', value: 200000 },
      { name: 'Medio', value: 90000 },
      { name: 'Bajo', value: 25000 },
    ])
  })
  it('contratos con dataset vacío preserva ChartContainer para tiempo de ciclo y muestra EmptyChartState contextual', async () => {
    fixture({ view: 'contracts', kpis: kpiPage([]) })
    expect(await screen.findByText('Tiempo de ciclo del contrato')).toBeVisible()
    expect(screen.getByText('No existen observaciones contractuales para los filtros seleccionados.')).toBeVisible()
    expect(screen.getByLabelText('Tiempo de ciclo del contrato: sin datos disponibles')).toBeInTheDocument()
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
  })
  it('litigios con ambos datasets vacíos preserva ambas superficies de gráficas independientemente', async () => {
    fixture({ view: 'litigation', kpis: kpiPage([]) })
    expect(await screen.findByText('Evolución de exposición litigiosa')).toBeVisible()
    expect(screen.getByText('Nuevos litigios por periodo')).toBeVisible()
    expect(screen.getByLabelText('Evolución de exposición litigiosa: sin datos disponibles')).toBeInTheDocument()
    expect(screen.getByLabelText('Nuevos litigios por periodo: sin datos disponibles')).toBeInTheDocument()
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
  })
  it('litigios con estado mixto: una gráfica poblada y otra vacía conviven sin anularse', async () => {
    fixture({ view: 'litigation', kpis: kpiPage([observation('KPI-LI-01', { dimensions: { nivel_severidad: 'alto' }, value: '15.00' })]) })
    expect((await screen.findAllByText('Evolución de exposición litigiosa')).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Nuevos litigios por periodo')).toBeVisible()
    expect(screen.getAllByTestId('chart')).toHaveLength(1)
    expect(screen.getByLabelText('Nuevos litigios por periodo: sin datos disponibles')).toBeInTheDocument()
  })
  it('tendencias sin hallazgos prioriza el resumen de ejecución y no crea una galería KPI', async () => {
    fixture({ view: 'trends', kpis: kpiPage([]) })
    expect((await screen.findAllByText(/Análisis completado sin hallazgos/)).length).toBeGreaterThan(0)
    expect(screen.getByText(/reglas deterministas/)).toBeVisible()
    expect(screen.queryAllByTestId('chart')).toHaveLength(0)
  })
})
