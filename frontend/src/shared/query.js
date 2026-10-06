// Solo claves declaradas por la vista; consultas sensibles no son filtros URL.
export function readQuery(search, validators) {
  const params = new URLSearchParams(search); const result = {}
  for (const [key, validate] of Object.entries(validators)) { const values = params.getAll(key); if (values.length === 1 && validate(values[0])) result[key] = values[0] }
  return result
}
export function writeQuery(values, validators) {
  const params = new URLSearchParams()
  for (const key of Object.keys(validators).sort()) { const value = values[key]; if (typeof value === 'string' && validators[key](value)) params.set(key, value) }
  return params.toString()
}
