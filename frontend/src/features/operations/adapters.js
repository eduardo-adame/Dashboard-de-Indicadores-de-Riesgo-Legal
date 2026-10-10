import { ApiError } from '../../api/errors.js'
import { formatTimestamp, formatValue } from '../../shared/formatters.js'
import { readQuery, writeQuery } from '../../shared/query.js'

export const uuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
export const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value) => typeof value === 'string'
const nullable = (value, check = text) => value === null || check(value)
const assert = (condition) => { if (!condition) throw new ApiError(503) }
export const families = ['CONTRATOS_DOCUMENTOS', 'LITIGIOS', 'CUMPLIMIENTO', 'AUDITORIA_INTERNA']
export const locations = [
  { value: 'contracts-documents', label: 'Contratos y documentos' },
  { value: 'litigation', label: 'Litigios' },
  { value: 'compliance', label: 'Cumplimiento' },
  { value: 'internal-audit', label: 'Auditoría interna' },
]
export const quarantineStates = ['Pendiente', 'Reinyectado', 'Descartado']
export const quarantineCauses = ['MISSING_REQUIRED_FIELD', 'INVALID_TYPE', 'INVALID_DATE', 'OUT_OF_CATALOG', 'NEGATIVE_AMOUNT', 'DATE_ORDER_VIOLATION', 'STRUCTURAL_INCONSISTENCY', 'IDENTITY_CONFLICT', 'TECHNICAL_READ_FAILURE', 'OTHER_CAUSE']
const timestamp = (value) => text(value) && formatTimestamp(value) !== '—'
const short = (max) => (value) => text(value) && value.length > 0 && value.length <= max
const limit = (value) => /^(?:[1-9]\d?|1\d\d|200)$/.test(value)
export const filterValidators = {
  quarantine: { ingest_file_id: uuid, source_family: (v) => families.includes(v), rejected_from: timestamp, rejected_to: timestamp, cause_code: (v) => quarantineCauses.includes(v), state: (v) => quarantineStates.includes(v), limit, cursor: short(512) },
  ocr: { document_id: short(256), state: (v) => ['Pendiente', 'Rechazado por baja confianza'].includes(v), processed_from: timestamp, processed_to: timestamp, limit, cursor: short(2048) },
  audit: { occurred_from: timestamp, occurred_to: timestamp, action: short(128), resource_type: short(128), correlation_id: uuid, limit, cursor: short(1024) },
}
export function readFilters(search, kind) { return readQuery(search, filterValidators[kind]) }
export function filterQuery(filters, kind) {
  const validators = filterValidators[kind]
  if (!validators || Object.entries(filters).some(([key, value]) => !validators[key]?.(value))) throw new ApiError(422)
  const prefix = { quarantine: 'rejected', ocr: 'processed', audit: 'occurred' }[kind]
  if (filters[`${prefix}_from`] && filters[`${prefix}_to`] && new Date(filters[`${prefix}_from`]) > new Date(filters[`${prefix}_to`])) throw new ApiError(422)
  return writeQuery({ limit: '100', ...filters }, validators)
}
export function changeFilters(filters, patch) { const next = { ...filters, ...patch }; delete next.cursor; for (const key of Object.keys(next)) if (next[key] === '') delete next[key]; return next }
export function ingestionResult(data) {
  assert(object(data) && uuid(data.file_id) && uuid(data.operation_id) && uuid(data.correlation_id) && ['COMPLETADO', 'CUARENTENA', 'RECHAZADO'].includes(data.state) && nullable(data.format) && families.includes(data.family) && ['VALIDATION', 'DOCUMENT', 'NONE'].includes(data.routing_target) && nullable(data.safe_cause_code) && typeof data.idempotent === 'boolean')
  return { ...data, id: data.file_id, reception: data.state === 'COMPLETADO' ? 'Archivo recibido; procesamiento downstream pendiente' : data.state === 'CUARENTENA' ? 'Archivo retenido en cuarentena' : 'Archivo rechazado' }
}
export function dispatchResult(data) {
  assert(object(data) && text(data.state) && nullable(data.downstream_target) && nullable(data.operation_id, uuid) && nullable(data.downstream_result_id, uuid))
  return { ...data }
}
export function quarantinePage(data) {
  assert(object(data) && Array.isArray(data.items) && nullable(data.next_cursor))
  const items = data.items.map((row) => {
    assert(object(row) && uuid(row.id) && nullable(row.ingest_file_id, uuid) && nullable(row.source_record_id, uuid) && nullable(row.row_number, Number.isInteger) && nullable(row.source_family, (v) => families.includes(v)) && timestamp(row.created_at) && quarantineCauses.includes(row.cause_code) && text(row.cause_description) && quarantineStates.includes(row.state) && nullable(row.original_payload, object) && nullable(row.candidate_payload, object) && nullable(row.discard_justification))
    return { ...row, canAct: row.state === 'Pendiente', canCorrect: row.state === 'Pendiente' && object(row.original_payload), createdLabel: formatTimestamp(row.created_at) }
  })
  return { items, next_cursor: data.next_cursor }
}
export function discardResult(data) { assert(object(data) && uuid(data.id) && data.state === 'Descartado' && text(data.discard_justification)); return data }
export function reinjectionResult(data) {
  // Solo el mensaje contractual confirma el efecto upstream sin un DTO completo.
  if (data?.detail === 'La reinyección fue confirmada; el recálculo sigue pendiente') return { upstreamCommitted: true, pending: true, message: 'Reinyección confirmada. El recálculo sigue pendiente.' }
  assert(object(data) && nullable(data.quarantine_state, (v) => quarantineStates.includes(v)) && uuid(data.job_id) && uuid(data.operation_id) && text(data.job_state))
  const committed = data.quarantine_state === 'Reinyectado'
  const pending = committed && !['COMPLETED', 'NO_RELEVANT_WORK'].includes(data.job_state)
  return { ...data, upstreamCommitted: committed, pending, message: pending ? 'Reinyección confirmada. El recálculo sigue pendiente.' : committed ? 'Reinyección confirmada.' : 'El registro permanece pendiente de validación.' }
}
export function ocrPage(data) {
  assert(object(data) && Array.isArray(data.items) && nullable(data.next_cursor))
  const items = data.items.map((row) => {
    const ocrState = (value) => ['Pendiente', 'Exitoso', 'Rechazado por baja confianza'].includes(value)
    assert(object(row) && text(row.document_id) && uuid(row.document_version_id) && text(row.document_name) && nullable(row.file_name) && text(row.processing_state) && typeof row.ocr_applicable === 'boolean' && typeof row.reprocess_eligible === 'boolean' && nullable(row.ocr_state, ocrState) && nullable(row.processed_at, timestamp) && nullable(row.confidence, (v) => typeof v === 'number' && Number.isFinite(v)) && nullable(row.total_page_count, Number.isInteger) && nullable(row.ocr_processed_page_count, Number.isInteger) && nullable(row.granularity) && nullable(row.outcome, ocrState) && row.outcome === row.ocr_state)
    // Las etiquetas describen aplicabilidad; no sustituyen el estado persistido.
    const ocrStateLabel = !row.ocr_applicable ? 'No aplica' : row.ocr_state ?? 'Sin resultado OCR'
    return { ...row, id: `${row.document_id}:${row.document_version_id}`, ocrStateLabel, outcomeLabel: ocrStateLabel, processedLabel: formatTimestamp(row.processed_at), confidenceLabel: row.confidence === null ? 'No disponible' : String(row.confidence) }
  })
  return { items, next_cursor: data.next_cursor }
}
export function ocrResult(data) {
  if (data?.detail?.cause === 'UPSTREAM_COMMITTED_KPI_RETRYABLE' && uuid(data.detail.operation_id)) return { operation_id: data.detail.operation_id, upstreamCommitted: true, pending: true, message: 'Reproceso confirmado.' }
  assert(object(data) && uuid(data.operation_id) && text(data.document_id) && nullable(data.document_version_id, uuid) && text(data.ocr_state) && text(data.processing_state) && nullable(data.kpi_job_id, uuid) && nullable(data.kpi_job_state))
  return { ...data, upstreamCommitted: true, pending: data.kpi_job_state !== null && !['COMPLETED', 'NO_RELEVANT_WORK'].includes(data.kpi_job_state), message: 'Reproceso confirmado.' }
}
export function auditPage(data) {
  assert(object(data) && Array.isArray(data.items) && nullable(data.next_cursor))
  const items = data.items.map((row) => {
    assert(object(row) && uuid(row.id) && timestamp(row.occurred_at) && text(row.actor_type) && text(row.actor_identifier) && nullable(row.actor_user_id, uuid) && text(row.action) && text(row.resource_type) && nullable(row.resource_identifier) && text(row.result) && nullable(row.safe_cause_code) && uuid(row.operation_id) && uuid(row.correlation_id) && nullable(row.query_sha256) && Array.isArray(row.resources))
    assert(row.resources.every((r) => object(r) && text(r.resource_type) && text(r.resource_identifier) && nullable(r.id_documento) && nullable(r.fragment_id, uuid)))
    return { ...row, occurredLabel: formatTimestamp(row.occurred_at) }
  })
  return { items, next_cursor: data.next_cursor }
}
export function technicalResult(data) {
  assert(object(data) && data.kpi_code === 'KPI-CD-03' && ['DISPONIBLE', 'NO_DISPONIBLE'].includes(data.availability) && nullable(data.value, (v) => text(v) && /^\d+(\.\d+)?$/.test(v)) && nullable(data.calculated_at, timestamp) && !(data.availability === 'DISPONIBLE' && data.value === null))
  return { ...data, displayValue: formatValue(data.value, data.availability, { suffix: ' %' }), calculatedLabel: formatTimestamp(data.calculated_at) }
}
