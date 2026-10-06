// Catálogo de presentación; la autorización efectiva sigue perteneciendo al backend.
const common = ['dashboard.read', 'kpi.read', 'document.query']
const analyst = [...common, 'ingest.upload', 'ingest.execute', 'quarantine.read', 'quarantine.reinject', 'quarantine.discard', 'audit.read.own', 'document.manage']
export const ROLE_PERMISSIONS = Object.freeze({
  JURIDICO: Object.freeze(common), ANALISTA: Object.freeze(analyst),
  TI: Object.freeze([...analyst, 'audit.read.all', 'user.create', 'user.update', 'user.disable', 'user.reset_access', 'role.assign', 'document_acl.manage', 'technical_config.manage']),
})
export const KNOWN_CAPABILITIES = Object.freeze([...new Set(Object.values(ROLE_PERMISSIONS).flat())])
export function capabilitiesFor(principal) { return new Set((principal?.roles || []).flatMap((role) => ROLE_PERMISSIONS[role] || [])) }
export function can(principal, capability) { return KNOWN_CAPABILITIES.includes(capability) && capabilitiesFor(principal).has(capability) }
export function validatePrincipal(data) {
  if (!data || typeof data.id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(data.id) || typeof data.username !== 'string' || !data.username || !Array.isArray(data.roles) || data.roles.some((role) => typeof role !== 'string')) return null
  return Object.freeze({ id: data.id, username: data.username, roles: Object.freeze([...new Set(data.roles)].sort()) })
}
