import { describe, expect, it } from 'vitest'
import { AXIS_UNITS, chartAxisConfig } from '../src/features/analytics/chartAxes.js'

describe('ROBUSTNESS: escalas por unidad sin fracciones ni clipping de conteos', () => {
  it.each([[1], [1, 1, 1, 1], [0], [0, 1, 3], [7, 20, 12], [1001]].map((values) => [values]))('COUNT ofrece ticks enteros y margen para %j', (values) => {
    const axis = chartAxisConfig(AXIS_UNITS.COUNT, values)
    expect(axis.domain[0]).toBe(0)
    expect(axis.domain[1]).toBeGreaterThan(Math.max(...values))
    expect(axis.allowDecimals).toBe(false)
    expect(axis.ticks[0]).toBe(0)
    expect(axis.ticks.at(-1)).toBe(axis.domain[1])
    expect(axis.ticks.every((tick) => Number.isInteger(tick) && tick >= 0)).toBe(true)
    expect(axis.ticks.every((tick, index) => index === 0 || tick > axis.ticks[index - 1])).toBe(true)
  })
  it('max1 usa 0..2 por una regla dinámica, no fracciones de litigio', () => {
    expect(chartAxisConfig(AXIS_UNITS.COUNT, [1, 1])).toMatchObject({ domain: [0, 2], ticks: [0, 1, 2] })
  })
  it('ausencias y ND no son máximos cero observados ni producen un dominio degenerado', () => {
    expect(chartAxisConfig(AXIS_UNITS.COUNT, [null, undefined, NaN]).domain).toEqual([0, 1])
  })
  it('DAYS admite promedios decimales y usa el sufijo correcto', () => {
    const axis = chartAxisConfig(AXIS_UNITS.DAYS, [0, 2.5, 12.75])
    expect(axis.allowDecimals).toBe(true)
    expect(axis.domain[1]).toBeGreaterThan(12.75)
    expect(axis.tickFormatter(2.5)).toBe('2.5 d')
    expect(axis.ticks).toBeUndefined()
  })
  it('MXN conserva escala compacta y no usa la precisión COUNT', () => {
    const axis = chartAxisConfig(AXIS_UNITS.MXN, [315000, 200000])
    expect(axis.allowDecimals).toBe(true)
    expect(axis.domain[1]).toBeGreaterThan(315000)
    expect(axis.tickFormatter(315000)).toBe('$0.32 M')
  })
  it('PERCENTAGE tiene límites y formato propios, no headroom de conteos', () => {
    const axis = chartAxisConfig(AXIS_UNITS.PERCENTAGE, [12.5, 100])
    expect(axis.domain).toEqual([0, 100])
    expect(axis.tickFormatter(12.5)).toBe('12.50 %')
  })
})
