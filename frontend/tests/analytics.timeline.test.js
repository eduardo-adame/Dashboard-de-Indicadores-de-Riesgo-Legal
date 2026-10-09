import { describe, expect, it } from 'vitest'
import { adaptKpis, KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { hasCompleteExposurePartition, litigationExposureViewModel, monthlyTimeline, temporalSeriesViewModel } from '../src/features/analytics/viewModels.js'

const observation = (code, month, value, overrides = {}) => {
  const end = new Date(`${month}-01T00:00:00Z`); end.setUTCMonth(end.getUTCMonth() + 1); end.setUTCDate(0)
  return { kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO', period_start: `${month}-01`, period_end: end.toISOString().slice(0, 10), as_of_date: `${month}-01`, calculated_at: '2030-02-01T00:00:00Z', dimensions: {}, entity_filter_applicable: false, value, availability: value === null ? 'NO_DISPONIBLE' : 'DISPONIBLE', ...overrides }
}
const adapted = (items) => adaptKpis({ items, filters: {}, period_start: null, period_end: null, period_reference: null }).items
const series = (code, values) => adapted(values.map(([month, value]) => observation(code, month, value)))
const exposure = (month = '2026-10', overrides = {}) => ['alto', 'medio', 'bajo'].map((severity, index) => observation('KPI-LI-01', month, ['200000', '90000', '25000'][index], { dimensions: { nivel_severidad: severity }, ...overrides }))

describe('ROBUSTNESS: calendario mensual aprobado con espaciado cronológico', () => {
  it.each(['KPI-RC-01', 'KPI-LI-05'])('%s ordena meses recibidos, conserva hueco y no altera fuentes', (code) => {
    const items = series(code, [['2026-08', '30'], ['2026-05', '10'], ['2026-09', '15'], ['2026-06', '20']])
    const before = structuredClone(items)
    const rows = temporalSeriesViewModel(items, code)
    expect(rows.map(({ month }) => month)).toEqual(['2026-05', '2026-06', '2026-07', '2026-08', '2026-09'])
    expect(rows.map(({ value }) => value)).toEqual([10, 20, null, 30, 15])
    expect(rows[2]).toMatchObject({ rawValue: null, valueText: 'Sin observación', availability: 'NO_OBSERVATION', observed: false, periodText: '01/07/2026 – 31/07/2026' })
    expect(items).toEqual(before)
    expect(items).toHaveLength(4)
  })
  it('el calendario común respeta cambio de año y febrero bisiesto', () => {
    const rows = monthlyTimeline([{ month: '2023-12' }, { month: '2024-03' }])
    expect(rows.map(({ month }) => month)).toEqual(['2023-12', '2024-01', '2024-02', '2024-03'])
    expect(rows[2].period_end).toBe('2024-02-29')
  })
})

describe('SRS_REQUIRED: fuentes temporales sin imputación ni pérdida de precisión', () => {
  it.each(['KPI-RC-01', 'KPI-LI-05'])('%s distingue cero, ND explícito y ausencia de mes', (code) => {
    const rows = temporalSeriesViewModel(series(code, [['2026-05', '0'], ['2026-06', null], ['2026-08', '1']]), code)
    expect(rows.map(({ availability }) => availability)).toEqual(['DISPONIBLE', 'NO_DISPONIBLE', 'NO_OBSERVATION', 'DISPONIBLE'])
    expect(rows.map(({ valueText }) => valueText)).toEqual(['0', 'No disponible', 'Sin observación', '1'])
    expect(rows.map(({ value }) => value)).toEqual([0, null, null, 1])
  })
  it.each(['KPI-RC-01', 'KPI-LI-05'])('%s singlepoint no inventa historia ni meses fuera de selección', (code) => {
    expect(temporalSeriesViewModel(series(code, [['2026-09', '1']]), code).map(({ month }) => month)).toEqual(['2026-09'])
    expect(temporalSeriesViewModel([], code)).toEqual([])
  })
  it('RC01 conserva promedios decimales y precisión textual aunque la coordenada no sea representable', () => {
    const rows = temporalSeriesViewModel(series('KPI-RC-01', [['2026-05', '12.5000'], ['2026-06', '0.1234567890123456789']]), 'KPI-RC-01')
    expect(rows[0]).toMatchObject({ value: 12.5, rawValue: '12.5000', valueText: '12.5000' })
    expect(rows[1]).toMatchObject({ value: null, rawValue: '0.1234567890123456789', valueText: '0.1234567890123456789', availability: 'DISPONIBLE' })
  })
})

describe('SRS_REQUIRED: exposición completa y trazas de contexto comparables', () => {
  it('partición completa entrega total exacto, tres niveles y un único punto real', () => {
    const model = litigationExposureViewModel(adapted(exposure()))
    expect(model.rows).toHaveLength(1)
    expect(model.rows[0]).toMatchObject({ complete: true, total: 315000, totalRaw: '315000', high: 200000, medio: 90000, bajo: 25000 })
    expect(model.traces).toHaveLength(1)
    expect(model.traces[0].rows).toHaveLength(1)
  })
  it('incompleta conserva medio/bajo, no elimina el periodo ni crea total/alto', () => {
    const model = litigationExposureViewModel(adapted(exposure().slice(1)))
    expect(model.rows).toHaveLength(1)
    expect(model).toMatchObject({ hasTotal: false, hasHigh: false })
    expect(model.rows[0]).toMatchObject({ medioText: '$90,000 MXN', bajoText: '$25,000 MXN', altoText: 'Sin observación' })
    expect(model.rows[0].total).toBeUndefined()
  })
  it('todo ND conserva las observaciones en tabla, sin total ni cero', () => {
    const model = litigationExposureViewModel(adapted(exposure('2026-10', { availability: 'NO_DISPONIBLE', value: null })))
    expect(model.rows).toHaveLength(1)
    expect(model.rows[0]).toMatchObject({ alto: null, medio: null, bajo: null, altoText: 'No disponible', medioText: 'No disponible', bajoText: 'No disponible' })
    expect(model).toMatchObject({ hasTotal: false, hasHigh: false })
    expect(model.rows[0].total).toBeUndefined()
  })
  it('cero completo es total real y ausencia no genera historia', () => {
    expect(litigationExposureViewModel(adapted(exposure('2026-10', { value: '0' }))).rows[0]).toMatchObject({ complete: true, total: 0, high: 0, totalText: '$0 MXN' })
    expect(litigationExposureViewModel([])).toMatchObject({ rows: [], traces: [], hasTotal: false, hasHigh: false })
  })
})

describe('ROBUSTNESS: evolución por periodos y contextos sin continuidad falsa', () => {
  it('varios periodos compatibles crecen cronológicamente y rompen el mes ausente', () => {
    const model = litigationExposureViewModel(adapted([...exposure('2026-10'), ...exposure('2026-08')]))
    expect(model.rows.map(({ month }) => month)).toEqual(['2026-08', '2026-10'])
    expect(model.traces[0].rows.map(({ month }) => month)).toEqual(['2026-08', '2026-09', '2026-10'])
    expect(model.traces[0].rows[1].total).toBeUndefined()
    expect(model.rows).toHaveLength(2)
  })
})

describe('ROBUSTNESS: particiones incompatibles y precisión financiera', () => {
  it('alto A/medio B/bajo A no produce un total125 ni traza mezclada', () => {
    const items = exposure().map((item, index) => ({ ...item, value: ['100', '20', '5'][index], dimensions: { ...item.dimensions, entity: index === 1 ? 'B' : 'A' } }))
    const model = litigationExposureViewModel(adapted(items))
    expect(model.hasTotal).toBe(false)
    expect(model.rows).toHaveLength(2)
    expect(model.traces).toHaveLength(2)
    expect(model.rows.every(({ total }) => total === undefined)).toBe(true)
    expect(hasCompleteExposurePartition(adapted(items))).toBe(false)
  })
  it('period_end distinto separa particiones y no enlaza simultáneas arbitrariamente', () => {
    const items = exposure(); items[2].period_end = '2026-10-30'
    const model = litigationExposureViewModel(adapted(items))
    expect(model.hasTotal).toBe(false)
    expect(model.rows).toHaveLength(2)
    expect(model.traces).toHaveLength(2)
    expect(model.traces.every(({ rows }) => rows.length === 1)).toBe(true)
  })
  it('cada contexto completo conserva su total propio, sin total global mezclado', () => {
    const items = ['A', 'B'].flatMap((entity) => exposure().map((item) => ({ ...item, dimensions: { ...item.dimensions, entity } })))
    const model = litigationExposureViewModel(adapted(items))
    expect(model.traces).toHaveLength(2)
    expect(model.rows.map(({ total }) => total)).toEqual([315000, 315000])
    expect(hasCompleteExposurePartition(adapted(items))).toBe(false)
  })
  it('grupo adicional no habilita un total completo', () => {
    const extra = observation('KPI-LI-01', '2026-10', '1', { dimensions: { nivel_severidad: 'desconocido' } })
    expect(litigationExposureViewModel(adapted([...exposure(), extra])).hasTotal).toBe(false)
  })
  it('total y moneda mantienen el decimal exacto aunque no haya coordenada segura', () => {
    const items = exposure(); items[0].value = '9007199254740993.000000000000000001'; items[1].value = '0.000000000000000002'; items[2].value = '0'
    const model = litigationExposureViewModel(adapted(items))
    expect(model.rows[0]).toMatchObject({ complete: true, total: null, totalRaw: '9007199254740993.000000000000000003', totalText: '$9,007,199,254,740,993.000000000000000003 MXN' })
  })
})
