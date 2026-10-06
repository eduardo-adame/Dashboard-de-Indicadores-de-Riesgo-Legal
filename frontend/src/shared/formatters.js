const decimalPattern = /^(-?)(\d+)(?:\.(\d+))?$/
// La representación decimal textual no pasa por Number ni pierde precisión.
export function formatDecimal(value, { prefix = '', suffix = '' } = {}) {
  if (value === null || value === undefined) return 'No disponible'
  const match = String(value).match(decimalPattern)
  if (!match) return 'No disponible'
  return `${prefix}${match[1]}${match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${match[3] !== undefined ? `.${match[3]}` : ''}${suffix}`
}
export function formatValue(value, availability, options) { return availability === 'NO_DISPONIBLE' ? 'No disponible' : formatDecimal(value, options) }
export function validDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value
}
export function formatDate(value) { if (!validDate(value)) return '—'; const [year, month, day] = value.split('-'); return `${day}/${month}/${year}` }
export function formatTimestamp(value) {
  if (typeof value !== 'string') return '—'
  const parts = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|([+-])(\d{2}):(\d{2}))$/)
  if (!parts || !validDate(parts[1]) || Number(parts[2]) > 23 || Number(parts[3]) > 59 || Number(parts[4]) > 59 || (parts[5] !== 'Z' && (Number(parts[7]) > 23 || Number(parts[8]) > 59))) return '—'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? '—' : `${new Intl.DateTimeFormat('es-MX', { dateStyle: 'short', timeStyle: 'short', timeZone: 'UTC' }).format(date)} UTC`
}
