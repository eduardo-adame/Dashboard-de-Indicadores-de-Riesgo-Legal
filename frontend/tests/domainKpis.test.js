import { describe, expect, it } from 'vitest'
import { adaptKpis, KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { DOMAIN_KPI_CATALOGS, resolveDomainKpis, resolveExecutiveKpis, resolveKpiCatalog } from '../src/features/analytics/executiveKpis.js'

const observation = (code, overrides = {}) => ({
  kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit,
  classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO',
  period_start: '2030-01-01', period_end: '2030-01-31', as_of_date: '2030-01-31',
  calculated_at: '2030-02-01T00:00:00Z', dimensions: {}, entity_filter_applicable: false,
  availability: 'DISPONIBLE', value: '1', ...overrides,
})
const exposure = (levels = ['alto', 'medio', 'bajo'], overrides = {}) => levels.map((level) => observation('KPI-LI-01', {
  dimensions: { nivel_severidad: level }, value: { alto: '200000', medio: '90000', bajo: '25000' }[level], ...overrides,
}))
const resolve = (view, items = [], options = {}) => resolveDomainKpis({
  view, items: adaptKpis({ items, filters: {}, period_start: null, period_end: null, period_reference: null }).items, ...options,
})

describe('SRS_REQUIRED: catálogos estructurales por dominio y estados compartidos', () => {
  it('contratos contiene solo RC03 principal; RC01 sigue siendo contexto', () => {
    expect(DOMAIN_KPI_CATALOGS.contracts.map(({ code }) => code)).toEqual(['KPI-RC-03'])
    expect(resolve('contracts', [observation('KPI-RC-01'), observation('KPI-LI-05')])[0].state).toBe('NO_OBSERVATION')
  })
  it('litigios contiene LI01/LI05, nunca los contextos ni los otros dominios recibidos', () => {
    expect(DOMAIN_KPI_CATALOGS.litigation.map(({ code }) => code)).toEqual(['KPI-LI-01', 'KPI-LI-05'])
    expect(resolve('litigation', [observation('KPI-RC-03'), observation('KPI-RC-01')]).map(({ state }) => state)).toEqual(['NO_OBSERVATION', 'NO_OBSERVATION'])
  })
  it.each(['contracts', 'litigation'])('%s conserva su catálogo congelado e identidades sin datos', (view) => {
    expect(Object.isFrozen(DOMAIN_KPI_CATALOGS)).toBe(true)
    expect(Object.isFrozen(DOMAIN_KPI_CATALOGS[view])).toBe(true)
    expect(resolve(view).map(({ state }) => state)).toEqual(DOMAIN_KPI_CATALOGS[view].map(() => 'NO_OBSERVATION'))
    expect(resolve(view).every(({ accessibleValue }) => accessibleValue === 'Sin observación para la selección')).toBe(true)
  })
  it('all-data conserva RC03 real y exposición completa/LI05 real sin inventar KPI', () => {
    const items = [observation('KPI-RC-03', { value: '3' }), ...exposure(), observation('KPI-LI-05')]
    expect(resolve('contracts', items)[0]).toMatchObject({ state: 'VALUE', rawValue: '3' })
    expect(resolve('litigation', items).map(({ displayValue }) => displayValue)).toEqual(['$315,000 MXN', '1'])
  })
  it('partial mantiene LI05 cero y LI01 ausente, sin eliminar un slot', () => {
    expect(resolve('litigation', [observation('KPI-LI-05', { value: '0' })]).map(({ state }) => state)).toEqual(['NO_OBSERVATION', 'ZERO'])
  })
  it('contratos preserva cero real distinto de ausencia y ND', () => {
    expect(resolve('contracts', [observation('KPI-RC-03', { value: '0' })])[0]).toMatchObject({ state: 'ZERO', rawValue: '0', displayValue: '0' })
    expect(resolve('contracts', [observation('KPI-RC-03', { value: null, availability: 'NO_DISPONIBLE' })])[0].state).toBe('NO_DISPONIBLE')
  })
  it('litigios preserva total cero completo y LI05 cero', () => {
    expect(resolve('litigation', [...exposure(undefined, { value: '0' }), observation('KPI-LI-05', { value: '0' })]).map(({ state }) => state)).toEqual(['ZERO', 'ZERO'])
  })
  it('ND simple y dimensional permanece explícito, no cero ni Por dimensión', () => {
    const nd = { value: null, availability: 'NO_DISPONIBLE' }
    expect(resolve('litigation', [...exposure(undefined, nd), observation('KPI-LI-05', nd)]).map(({ displayValue }) => displayValue)).toEqual(['No disponible', 'No disponible'])
  })
  it('partición incompleta conserva desglose sin suma parcial', () => {
    const slot = resolve('litigation', exposure(['alto', 'medio']))[0]
    expect(slot.displayValue).toBe('Desglose disponible')
    expect(slot.rawValue).toBeUndefined()
  })
  it.each(['LOADING', 'ERROR', 'FORBIDDEN'])('el resolver representa %s sin interpretarlo como dato', (resourceState) => {
    expect(resolve('contracts', [], { resourceState })[0].state).toBe(resourceState)
    expect(resolve('litigation', [], { resourceState }).map(({ state }) => state)).toEqual([resourceState, resourceState])
  })
  it('filtros ajenos conservan NOT_APPLICABLE distinto de NO_OBSERVATION', () => {
    expect(resolve('contracts', [], { riskType: 'litigation' })[0].state).toBe('NOT_APPLICABLE')
    expect(resolve('litigation', [], { riskType: 'contractual' }).map(({ state }) => state)).toEqual(['NOT_APPLICABLE', 'NOT_APPLICABLE'])
  })
  it('las tres vistas consumen la misma política y el resumen conserva cinco identidades', () => {
    const items = adaptKpis({ items: [...exposure(), observation('KPI-LI-05')], filters: {}, period_start: null, period_end: null, period_reference: null }).items
    const summary = resolveExecutiveKpis({ items })
    expect(summary).toHaveLength(5)
    const domain = resolveDomainKpis({ view: 'litigation', items })
    expect(domain).toEqual(summary.filter(({ domain }) => domain === 'litigation'))
    expect(domain).toEqual(resolveKpiCatalog(DOMAIN_KPI_CATALOGS.litigation, { items }))
  })
})

describe('ROBUSTNESS: contratos inválidos y particiones incompatibles', () => {
  it('DISPONIBLE/null se rechaza antes de resolver slots', () => {
    expect(() => resolve('contracts', [observation('KPI-RC-03', { value: null })])).toThrow()
  })
  it('contextos o period_end distintos no forman total en litigios', () => {
    const mixed = exposure(); mixed[1].dimensions.entity = 'Otra entidad'
    expect(resolve('litigation', mixed)[0].rawValue).toBeUndefined()
    const dates = exposure(); dates[2].period_end = '2030-01-30'
    expect(resolve('litigation', dates)[0].rawValue).toBeUndefined()
  })
})
