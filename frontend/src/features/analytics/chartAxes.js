import { formatDecimal } from '../../shared/formatters.js'
import { formatCompactCurrencyMXN, formatPercentage } from './viewModels.js'

export const AXIS_UNITS = Object.freeze({ COUNT: 'COUNT', DAYS: 'DAYS', MXN: 'MXN', PERCENTAGE: 'PERCENTAGE' })

export function chartAxisConfig(unit, values = []) {
  const maximum = Math.max(0, ...values.filter((value) => typeof value === 'number' && Number.isFinite(value)))
  if (unit === AXIS_UNITS.COUNT) {
    const headroom = maximum + Math.max(1, maximum * 0.1)
    const target = Math.max(1, headroom / 4)
    const magnitude = 10 ** Math.floor(Math.log10(target))
    const step = magnitude * ([5, 2, 1].find((factor) => factor * magnitude <= target) || 1)
    const ceiling = Math.ceil(headroom / step) * step
    return { domain: [0, ceiling], ticks: Array.from({ length: Math.round(ceiling / step) + 1 }, (_, index) => index * step), allowDecimals: false, tickFormatter: (value) => formatDecimal(String(value)) }
  }
  if (unit === AXIS_UNITS.PERCENTAGE) return { domain: [0, 100], allowDecimals: true, tickFormatter: formatPercentage }
  return {
    domain: [0, maximum > 0 ? maximum * 1.1 : 1], allowDecimals: true,
    tickFormatter: unit === AXIS_UNITS.DAYS ? (value) => `${formatDecimal(String(value))} d` : formatCompactCurrencyMXN,
  }
}
