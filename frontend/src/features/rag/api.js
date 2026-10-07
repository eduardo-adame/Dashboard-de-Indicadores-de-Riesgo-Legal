import { ApiError } from '../../api/errors.js'
import { adaptRagResponse, isUuid } from './adapters.js'

export function createRagSubmission(query, contextReferenceId = null, key = globalThis.crypto.randomUUID()) {
  if (typeof query !== 'string' || query.length < 1 || query.length > 4096 || !isUuid(key) || contextReferenceId !== null && !isUuid(contextReferenceId)) throw new ApiError(422)
  return Object.freeze({ key, payload: Object.freeze({ query, ...(contextReferenceId ? { context_reference_id: contextReferenceId } : {}) }) })
}
export async function requestRag(client, submission, signal) {
  if (!isUuid(submission?.key)) throw new ApiError(422)
  const valid = createRagSubmission(submission.payload?.query, submission.payload?.context_reference_id ?? null, submission.key)
  // Un 503 puede conservar evidencia autorizada; no equivale a ausencia de evidencia.
  return adaptRagResponse(await client.request('/rag/query', { method: 'POST', body: valid.payload, headers: { 'Idempotency-Key': valid.key }, acceptStatuses: [503], signal }))
}
