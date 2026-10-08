import { describe, expect, it } from 'vitest'
import { adaptAnalysis, adaptKpis, chartSeries, KPI_CATALOG, latestSeries } from '../src/features/analytics/adapters.js'

const runId = '11111111-1111-4111-8111-111111111111'
function observation(code = 'KPI-RC-03', overrides = {}) {
  return { kpi_code: code, name: KPI_CATALOG[code].name, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO', unit: KPI_CATALOG[code].unit,
    period_start: '2030-01-01', period_end: '2030-01-31', as_of_date: '2030-01-31', calculated_at: '2030-02-01T00:00:00Z', dimensions: {}, value: '0', availability: 'DISPONIBLE', entity_filter_applicable: false, ...overrides }
}
const page = (items) => ({ period_start: '2029-08-01', period_end: '2030-01-31', period_reference: '2030-01-01', filters: { period: 'last_6_months', period_start: null, period_end: null, risk_type: 'all', entity: null }, items })
function evaluation(overrides = {}) {
  return { id: '22222222-2222-4222-8222-222222222222', analytic_run_id: runId, kpi_code: 'KPI-RC-03', canonical_dimensions_key: {}, evaluation_period: '2030-01-01', outcome: 'EVALUATED', signal_detected: false, trend_direction: null, recurrence_month_count: 2, current_value: '0', reference_value: '1', absolute_variation: '-1', percentage_variation: '-100', rules_applied: ['RC03_APPEARANCE_OR_INCREASE'], not_evaluated_reason: null, entity_filter_applicable: false, ...overrides }
}
const analysis = (overrides = {}) => ({ current_analytic_run_id: runId, alert_count: 0, items: [{ analytic_run_id: runId, completed_at: '2030-02-01T00:00:00Z', window_start: '2029-08-01T00:00:00Z', window_end: '2030-01-31T23:59:59Z', executive_summary: 'Análisis completado sin hallazgos.', evaluations: [evaluation()], findings: [], context_references: [], ...overrides }] })
describe('SRS_REQUIRED: adapters analíticos', () => {
  it('preserva cinco KPI núcleo y dos contextuales sin CD03', () => {
    const data = adaptKpis(page(Object.keys(KPI_CATALOG).map((code) => observation(code))))
    expect(data.items).toHaveLength(7); expect(data.items.filter((item) => item.classification === 'MVP-NÚCLEO')).toHaveLength(5)
    expect(() => adaptKpis(page([{ ...observation(), kpi_code: 'KPI-CD-03' }]))).toThrow()
  })
  it('distingue cero disponible de null y No disponible', () => {
    expect(adaptKpis(page([observation()])).items[0].displayValue).toBe('0')
    expect(adaptKpis(page([observation('KPI-RC-01', { value: null, availability: 'NO_DISPONIBLE' })])).items[0].displayValue).toBe('No disponible')
  })
  it('preserva dimensiones y entidad no aplicable sin inferir nombres', () => {
    const item = adaptKpis(page([observation('KPI-CN-03', { dimensions: { area: 'Área sintética', nivel_severidad: 'alto' } })])).items[0]
    expect(item.dimensionsText).toContain('Área sintética'); expect(item.entityText).toBe('No aplicable')
  })
  it('selecciona RC03 por periodo sin sumarlo ni acumularlo', () => {
    const data = adaptKpis(page([observation('KPI-RC-03', { value: '2' }), observation('KPI-RC-03', { value: '3', period_start: '2030-02-01', period_end: '2030-02-28' })]))
    expect(latestSeries(data.items, 'KPI-RC-03').map((item) => item.value)).toEqual(['3'])
  })
  it('el último periodo no disponible no cae al anterior', () => {
    const data = adaptKpis(page([observation(), observation('KPI-RC-03', { value: null, availability: 'NO_DISPONIBLE', period_start: '2030-02-01', period_end: '2030-02-28' })]))
    expect(latestSeries(data.items, 'KPI-RC-03')[0].displayValue).toBe('No disponible')
  })
  it('no inventa moneda ni total al separar severidades litigiosas', () => {
    const data = adaptKpis(page(['alto', 'medio', 'bajo'].map((nivel_severidad) => observation('KPI-LI-01', { dimensions: { nivel_severidad }, value: '12.50' }))))
    expect(chartSeries(data.items, 'KPI-LI-01')).toHaveLength(3)
    expect(data.items.every((row) => row.unit === 'importe' && !row.displayValue.includes('$'))).toBe(true)
  })
  it('preserva meses ausentes y mantiene unidades separadas', () => {
    const data = adaptKpis(page([observation('KPI-LI-05'), observation('KPI-LI-05', { period_start: '2030-03-01', period_end: '2030-03-31' }), observation('KPI-RC-01')]))
    expect(chartSeries(data.items, 'KPI-LI-05')[0].points.map((row) => row.month)).toEqual(['2030-01', '2030-03'])
    expect(chartSeries(data.items, 'KPI-RC-01')[0].unit).toBe('días')
  })
  it('no redondea el decimal fuente ni grafica magnitudes imprecisas', () => {
    const data = adaptKpis(page([observation('KPI-LI-01', { value: '9007199254740993.125' }), observation('KPI-RC-01', { value: '0.1234567890123456789' })]))
    expect(data.items[0].displayValue).toBe('9,007,199,254,740,993.125')
    expect(chartSeries(data.items, 'KPI-LI-01')[0].points[0].chartValue).toBeNull()
    expect(chartSeries(data.items, 'KPI-RC-01')[0].points[0].chartValue).toBeNull()
  })
  it('consume variaciones, tendencia y recurrencia sin recalcular', () => {
    const data = adaptAnalysis(analysis({ evaluations: [evaluation({ absolute_variation: '17', percentage_variation: '33.3', recurrence_month_count: 3, trend_direction: 'INCREASING' })] }))
    expect(data.items[0].evaluations[0]).toMatchObject({ absoluteText: '17', percentageText: '33.3 %', trendText: 'Creciente', recurrence_month_count: 3 })
  })
  it('conserva mezcla de histórico insuficiente y evaluación válida', () => {
    const data = adaptAnalysis(analysis({ evaluations: [evaluation(), evaluation({ id: '33333333-3333-4333-8333-333333333333', outcome: 'INSUFFICIENT_HISTORY', recurrence_month_count: null, not_evaluated_reason: 'MISSING_MONTH' })] }))
    expect(data.items[0].evaluations[1].outcomeText).toBe('Histórico insuficiente'); expect(data.alert_count).toBe(0)
  })
  it('no reemplaza un resultado completado por STARTED o FAILED', () => {
    expect(() => adaptAnalysis(analysis({ state: 'STARTED' }))).toThrow()
    expect(() => adaptAnalysis(analysis({ state: 'FAILED' }))).toThrow()
  })
  it('mantiene alert_count recibido y resumen exacto sin crear hallazgos', () => {
    const dto = analysis(); dto.alert_count = 8
    expect(adaptAnalysis(dto).alert_count).toBe(8)
    expect(adaptAnalysis(dto).items[0].executive_summary).toBe('Análisis completado sin hallazgos.')
  })
})
describe('ROBUSTNESS: DTO inválido', () => {
  it.each([{ value: 'NaN' }, { period_start: '2030-02-30' }, { dimensions: null }, { unit: 'MXN' }, { calculated_at: '2030-02-30T00:00:00Z' }])('rechaza forma inválida %j sin copiar payload al error', (change) => {
    expect(() => adaptKpis(page([observation('KPI-RC-03', change)]))).toThrow('El servicio no está disponible temporalmente.')
  })
  it('rechaza observaciones duplicadas en una identidad mensual', () => { expect(() => adaptKpis(page([observation(), observation()]))).toThrow() })
  it('no admite contexto-only en evaluaciones', () => { expect(() => adaptAnalysis(analysis({ evaluations: [evaluation({ kpi_code: 'KPI-EO-01' })] }))).toThrow() })
})
