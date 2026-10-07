import { useEffect, useRef, useState } from 'react'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { useResource } from '../../api/useResource.js'
import { ApiError, safeError } from '../../api/errors.js'
import { uuid, locations, filterQuery, ingestionResult, dispatchResult, discardResult, reinjectionResult, ocrResult } from './adapters.js'

const segment = (value) => encodeURIComponent(String(value))
const requireUuid = (value) => { if (!uuid(value)) throw new ApiError(422); return segment(value) }
export const newOperationId = () => crypto.randomUUID()
export function queryPath(kind, filters) { return `${{ quarantine: '/validation/quarantine', ocr: '/documents/ocr', audit: '/audit/events' }[kind]}?${filterQuery(filters, kind)}` }
export function createOperationsApi(client) {
  return {
    async upload(file, location, key) {
      if (!file || file.size > 52428800 || !locations.some((v) => v.value === location) || !uuid(key)) throw new ApiError(422)
      const body = new FormData(); body.set('file', file); body.set('controlled_location', location)
      return ingestionResult(await client.request('/ingestion/uploads', { method: 'POST', body, headers: { 'Idempotency-Key': key } }))
    },
    async run(location) {
      if (!locations.some((v) => v.value === location)) throw new ApiError(422)
      const data = await client.request('/ingestion/runs', { method: 'POST', body: { controlled_location: location } })
      if (!Number.isInteger(data?.processed) || !Array.isArray(data.results) || data.processed !== data.results.length) throw new ApiError(503)
      return data.results.map(ingestionResult)
    },
    async dispatch(fileId, correlationId) { requireUuid(fileId); requireUuid(correlationId); return dispatchResult(await client.request('/coordination/dispatch', { method: 'POST', body: { file_id: fileId, correlation_id: correlationId } })) },
    async discard(id, justification) { if (!justification.trim()) throw new ApiError(422); return discardResult(await client.request(`/validation/quarantine/${requireUuid(id)}/discard`, { method: 'POST', body: { justification } })) },
    async reinject(id, correctedPayload, correlationId) { requireUuid(correlationId); return reinjectionResult(await client.request(`/validation/quarantine/${requireUuid(id)}/reinject`, { method: 'PATCH', body: { corrected_payload: correctedPayload, correlation_id: correlationId }, acceptStatuses: [503] })) },
    async reprocess(item, requestId) { return ocrResult(await client.request(`/documents/${segment(item.document_id)}/ocr/reprocess`, { method: 'POST', body: { source_document_version_id: item.document_version_id, request_id: requireUuid(requestId) }, acceptStatuses: [503] })) },
    async administer(action, fields) {
      const accountPath = () => `/security/users/${requireUuid(fields.account_id)}`
      let path; let method; let body
      switch (action) {
        case 'create': path = '/security/users'; method = 'POST'; body = { username: fields.username, display_name: fields.display_name, password: fields.password, roles: fields.roles }; break
        case 'update': path = accountPath(); method = 'PATCH'; body = { display_name: fields.display_name }; break
        case 'activate': case 'disable': path = `${accountPath()}/${action}`; method = 'POST'; break
        case 'reset': path = `${accountPath()}/reset-access`; method = 'POST'; body = { password: fields.password }; break
        case 'roles': path = `${accountPath()}/roles`; method = 'PUT'; body = { roles: fields.roles }; break
        case 'scope': path = `/security/document-scopes/${segment(fields.role_id)}/${segment(fields.source_family)}`; method = 'PUT'; body = { active: fields.active }; break
        case 'user-exception': path = `/security/document-exceptions/user/${requireUuid(fields.account_id)}/${segment(fields.document_id)}`; method = 'PUT'; body = { decision: fields.decision, active: fields.active }; break
        case 'role-exception': path = `/security/document-exceptions/role/${segment(fields.role_id)}/${segment(fields.document_id)}`; method = 'PUT'; body = { decision: fields.decision, active: fields.active }; break
        default: throw new ApiError(422)
      }
      const data = await client.request(path, { method, body })
      if (action === 'create' && !uuid(data?.id)) throw new ApiError(503)
      return action === 'create' ? { id: data.id } : { confirmed: true }
    },
  }
}
export function useOperationsRead(path, adapter) {
  const resource = useResource(path, { isEmpty: (data) => Array.isArray(data?.items) && data.items.length === 0 })
  if (!resource.data) return resource
  try { return { ...resource, data: adapter(resource.data) } } catch (error) { return { ...resource, state: 'unrecoverable_error', data: null, error: safeError(error) } }
}
export function useOperation() {
  const session = useSession(); const latest = useRef(session); latest.current = session
  const [state, setState] = useState({ busy: false, result: null, error: null, generation: session.generation })
  const sequence = useRef(0); const active = useRef(false)
  useEffect(() => () => { sequence.current++ }, [])
  async function execute(capability, task) {
    if (active.current || latest.current.status !== 'authenticated') return null
    active.current = true; const generation = latest.current.generation; const current = ++sequence.current
    setState({ busy: true, result: null, error: null, generation })
    try {
      const permitted = await latest.current.revalidate()
      const verified = latest.current.getSnapshot()
      if (!permitted || verified.status !== 'authenticated' || verified.generation !== generation || !can(verified.principal, capability)) throw new ApiError(403)
      const result = await task(createOperationsApi(latest.current.client))
      const completed = latest.current.getSnapshot()
      if (current !== sequence.current || completed.status !== 'authenticated' || completed.generation !== generation) return null
      setState({ busy: false, result, error: null, generation })
      return result
    } catch (cause) {
      const error = safeError(cause)
      if (current === sequence.current && latest.current.generation === generation) setState({ busy: false, result: null, error, generation })
      return null
    } finally { active.current = false }
  }
  const compatible = state.generation === session.generation && session.status === 'authenticated'
  return { ...(compatible ? state : { busy: false, result: null, error: null }), execute, clear: () => { sequence.current++; setState({ busy: false, result: null, error: null, generation: latest.current.generation }) } }
}
