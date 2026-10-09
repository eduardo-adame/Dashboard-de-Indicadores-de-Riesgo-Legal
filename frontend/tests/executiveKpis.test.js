import { describe, expect, it } from 'vitest'
import { adaptKpis, KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { EXECUTIVE_KPI_CATALOG, resolveExecutiveKpis } from '../src/features/analytics/executiveKpis.js'
import { sumDecimals } from '../src/features/analytics/viewModels.js'

const observation = (code, overrides = {}) => ({
  kpi_code: code, ...KPI_CATALOG[code], classification: 'MVP-NÚCLEO',
  period_start: '2030-01-01', period_end: '2030-01-31', as_of_date: '2030-01-31',
  calculated_at: '2030-02-01T00:00:00Z', dimensions: {}, entity_filter_applicable: false,
  availability: 'DISPONIBLE', value: '0', ...overrides,
})
const adapt = (items) => adaptKpis({ items, filters: {}, period_start: null, period_end: null, period_reference: null }).items
const resolve = (items, options = {}) => resolveExecutiveKpis({ items: adapt(items), ...options })
const exposure = (levels = ['alto', 'medio', 'bajo'], overrides = {}) => levels.map((level) => observation('KPI-LI-01', { value: { alto: '200000', medio: '90000', bajo: '25000' }[level], dimensions: { nivel_severidad: level }, ...overrides }))
const li = (items) => resolve(items).find((slot) => slot.code === 'KPI-LI-01')
const cn = (items) => resolve(items).find((slot) => slot.code === 'KPI-CN-03')

describe('SRS_REQUIRED: catálogo y estados ejecutivos sin imputación', () => {
  it('mantiene exactamente cinco identidades y orden del núcleo, sin contexto ni técnico', () => {
    expect(EXECUTIVE_KPI_CATALOG.map((entry) => entry.code)).toEqual(['KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03'])
    expect(Object.isFrozen(EXECUTIVE_KPI_CATALOG)).toBe(true)
  })
  it('all-data conserva las cinco tarjetas y valores persistidos o representación autorizada', () => {
    const slots = resolve([observation('KPI-RC-03', { value: '3' }), ...exposure(), observation('KPI-LI-05', { value: '1' }), observation('KPI-CN-02', { value: '3' }), observation('KPI-CN-03', { value: '2', dimensions: { area: 'Legal', nivel_severidad: 'alto' } })])
    expect(slots.map(({ displayValue }) => displayValue)).toEqual(['3', '$315,000 MXN', '1', '3', 'Desglose disponible'])
  })
  it('partial-data conserva cero real de LI05 y cuatro ausencias distintas de ND', () => {
    const slots = resolve([observation('KPI-LI-05')])
    expect(slots).toHaveLength(5)
    expect(slots.filter((slot) => slot.state === 'ZERO').map((slot) => slot.code)).toEqual(['KPI-LI-05'])
    expect(slots.filter((slot) => slot.state === 'NO_OBSERVATION')).toHaveLength(4)
    expect(slots.find((slot) => slot.state === 'NO_OBSERVATION').accessibleValue).toBe('Sin observación para la selección')
  })
  it('no-data conserva cinco ausencias, nunca cinco ceros o ND', () => {
    expect(resolve([]).map((slot) => slot.state)).toEqual(Array(5).fill('NO_OBSERVATION'))
  })
  it.each(['KPI-RC-03', 'KPI-LI-05', 'KPI-CN-02'])('preserva el cero observado de %s', (code) => {
    expect(resolve([observation(code)]).find((slot) => slot.code === code)).toMatchObject({ state: 'ZERO', displayValue: '0', rawValue: '0' })
  })
  it('ND explícito no elimina la tarjeta ni se convierte en cero', () => {
    expect(resolve([observation('KPI-RC-03', { availability: 'NO_DISPONIBLE', value: null })])[0]).toMatchObject({ state: 'NO_DISPONIBLE', displayValue: 'No disponible' })
  })
  it('litigation distingue NOT_APPLICABLE del dominio aplicable sin observación', () => {
    const slots = resolve([], { riskType: 'litigation' })
    expect(slots.filter((slot) => slot.state === 'NOT_APPLICABLE').map((slot) => slot.code)).toEqual(['KPI-RC-03', 'KPI-CN-02', 'KPI-CN-03'])
    expect(slots.filter((slot) => slot.state === 'NO_OBSERVATION').map((slot) => slot.code)).toEqual(['KPI-LI-01', 'KPI-LI-05'])
  })
  it.each(['LOADING', 'ERROR', 'FORBIDDEN'])('no confunde el estado de recurso %s con datos', (resourceState) => {
    expect(resolve([], { resourceState }).map((slot) => slot.state)).toEqual(Array(5).fill(resourceState))
  })
})

describe('SRS_REQUIRED: representación dimensional ejecutiva', () => {
  it('LI01 suma la partición completa del mismo periodo y contexto sin mutar las fuentes', () => {
    const items = exposure(); const before = JSON.stringify(items)
    expect(li(items)).toMatchObject({ state: 'VALUE', rawValue: '315000', displayValue: '$315,000 MXN' })
    expect(JSON.stringify(items)).toBe(before)
  })
  it('LI01 incompleto conserva desglose, nunca muestra 290000 como total', () => {
    expect(li(exposure(['alto', 'medio']))).toMatchObject({ displayValue: 'Desglose disponible', valueKind: 'state' })
    expect(li(exposure(['alto', 'medio'])).rawValue).toBeUndefined()
  })
  it('LI01 disponible/ND es partición incompleta, no suma parcial', () => {
    const items = exposure(); items[2] = { ...items[2], availability: 'NO_DISPONIBLE', value: null }
    expect(li(items).displayValue).toBe('Desglose disponible')
    expect(li(items).rawValue).toBeUndefined()
  })
  it('LI01 todo ND comunica indisponibilidad, nunca Por dimensión', () => {
    expect(li(exposure(undefined, { availability: 'NO_DISPONIBLE', value: null }))).toMatchObject({ state: 'NO_DISPONIBLE', displayValue: 'No disponible' })
  })
  it('LI01 sin observaciones comunica ausencia', () => { expect(li([]).state).toBe('NO_OBSERVATION') })
  it('no completa el último periodo con una severidad de otro mes', () => {
    const items = exposure(); items[2] = { ...items[2], period_start: '2029-12-01', period_end: '2029-12-31' }
    expect(li(items).displayValue).toBe('Desglose disponible')
    expect(li(items).rawValue).toBeUndefined()
  })
  it('el último periodo ND no recupera valores de un periodo histórico anterior', () => {
    const older = exposure(undefined, { period_start: '2029-12-01', period_end: '2029-12-31' })
    expect(li([...older, ...exposure(undefined, { availability: 'NO_DISPONIBLE', value: null })]).state).toBe('NO_DISPONIBLE')
  })
  it('no suma dimensiones de contextos incompatibles ni period_end diferente', () => {
    const items = exposure(); items[2] = { ...items[2], dimensions: { ...items[2].dimensions, entity: 'Otra entidad' } }
    expect(li(items).rawValue).toBeUndefined()
    const dates = exposure(); dates[2].period_end = '2030-01-30'
    expect(li(dates).rawValue).toBeUndefined()
  })
  it('partición cero completa conserva cero visual legítimo', () => {
    expect(li(exposure(undefined, { value: '0' }))).toMatchObject({ state: 'ZERO', rawValue: '0', displayValue: '$0 MXN' })
  })
  it.each([1, 3])('CN03 con %i grupos nunca fabrica escalar global', (count) => {
    const items = Array.from({ length: count }, (_, index) => observation('KPI-CN-03', { dimensions: { area: `Área ${index}`, nivel_severidad: 'medio' }, value: '0' }))
    expect(cn(items)).toMatchObject({ displayValue: 'Desglose disponible', valueKind: 'state' })
    expect(cn(items).rawValue).toBeUndefined()
  })
  it('CN03 todo ND conserva ND y sin grupos conserva ausencia', () => {
    expect(cn([observation('KPI-CN-03', { dimensions: { area: 'Legal', nivel_severidad: 'alto' }, availability: 'NO_DISPONIBLE', value: null })]).state).toBe('NO_DISPONIBLE')
    expect(cn([]).state).toBe('NO_OBSERVATION')
  })
})

describe('ROBUSTNESS: agregación exacta y grupos no canónicos', () => {
  it('rechaza DISPONIBLE/null como contrato contradictorio, nunca lo convierte en ausencia o cero', () => {
    expect(() => resolve([observation('KPI-RC-03', { value: null })])).toThrow()
  })
  it('preserva decimales superiores a la precisión Number en el total visual', () => {
    const items = exposure(); items[0].value = '9007199254740993.000000000000000001'; items[1].value = '0.000000000000000002'; items[2].value = '0'
    expect(li(items).rawValue).toBe('9007199254740993.000000000000000003')
  })
  it('sumDecimals conserva el signo de las partes fraccionarias', () => { expect(sumDecimals(['-1.2', '0.1'])).toBe('-1.1') })
  it('un grupo adicional impide presentar una partición como total completo', () => {
    expect(li([...exposure(), observation('KPI-LI-01', { dimensions: { nivel_severidad: 'desconocido' }, value: '1' })]).rawValue).toBeUndefined()
  })
  it('no confunde severidades repetidas en distintas entidades con tres severidades únicas', () => {
    const items = ['A', 'B', 'C'].map((entity) => observation('KPI-LI-01', { dimensions: { entity, nivel_severidad: 'alto' }, value: '1' }))
    expect(li(items).rawValue).toBeUndefined()
  })
})
