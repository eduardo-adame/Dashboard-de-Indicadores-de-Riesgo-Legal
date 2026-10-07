import { ApiError } from '../../api/errors.js'
import { validDate } from '../../shared/formatters.js'

export const RAG_STATES = Object.freeze({ EVIDENCIA_SUFICIENTE: 'Evidencia suficiente', EVIDENCIA_INSUFICIENTE: 'Evidencia insuficiente', SIN_EVIDENCIA: 'Sin evidencia', SIN_AUTORIZACION: 'Sin autorización' })
export const isUuid = (value) => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
const fail = () => { throw new ApiError(503) }
const text = (value) => typeof value === 'string'
const nullableText = (value) => value === null || text(value)
const page = (value) => value === null || Number.isInteger(value) && value > 0
function metadata(item) {
  if (!item || !isUuid(item.fragment_id) || !text(item.document_id) || !item.document_id || !text(item.document_name) || !text(item.document_type) || !(item.document_date === null || validDate(item.document_date)) || !page(item.page_start) || !page(item.page_end) || !nullableText(item.section) || !nullableText(item.clause) || item.page_start !== null && item.page_end !== null && item.page_end < item.page_start) fail()
  return Object.freeze({ fragmentId: item.fragment_id, documentId: item.document_id, documentName: item.document_name, documentType: item.document_type, documentDate: item.document_date, pageStart: item.page_start, pageEnd: item.page_end, section: item.section, clause: item.clause })
}
export function adaptRagResponse(dto) {
  if (!dto || !isUuid(dto.operation_id) || !isUuid(dto.correlation_id) || !(dto.state === null || Object.hasOwn(RAG_STATES, dto.state)) || !['COMPLETED', 'FAILED'].includes(dto.operation_status) || !['NOT_REQUESTED', 'SUCCEEDED', 'FAILED'].includes(dto.generation_status) || !nullableText(dto.generated_response) || !nullableText(dto.safe_result_message) || !(dto.context_reference_id === null || isUuid(dto.context_reference_id)) || !Array.isArray(dto.fragments) || !Array.isArray(dto.citations)) fail()
  // Una denegación nunca se transforma en una vista de recursos, incluso ante un DTO defectuoso.
  if (dto.state === 'SIN_AUTORIZACION') return Object.freeze({ state: dto.state, stateLabel: RAG_STATES[dto.state], operationStatus: dto.operation_status, generationStatus: dto.generation_status, response: null, message: 'Acceso no autorizado.', fragments: [], citations: [], technicalFailure: false, contextReferenceId: null })
  if (dto.fragments.length > 5 || dto.citations.length > 5 || dto.state === null && dto.operation_status !== 'FAILED') fail()
  const fragments = dto.fragments.map((item) => {
    if (!text(item.fragment_text) || !['EVIDENCE', 'REFERENCE', 'CONTEXT'].includes(item.usage)) fail()
    return Object.freeze({ ...metadata(item), text: item.fragment_text, usage: item.usage })
  })
  if (new Set(fragments.map((item) => item.fragmentId)).size !== fragments.length) fail()
  const citations = dto.citations.map((item) => {
    if (!/^E[1-5]$/.test(item?.handle)) fail()
    const citation = metadata(item)
    const fragment = fragments.find((candidate) => candidate.fragmentId === citation.fragmentId)
    if (!fragment || fragment.usage !== 'EVIDENCE' || Object.keys(citation).some((key) => citation[key] !== fragment[key])) fail()
    return Object.freeze({ ...citation, handle: item.handle })
  })
  if (new Set(citations.map((item) => item.handle)).size !== citations.length || new Set(citations.map((item) => item.fragmentId)).size !== citations.length || dto.state === 'SIN_EVIDENCIA' && (fragments.length || citations.length)) fail()
  if (dto.state !== 'EVIDENCIA_SUFICIENTE' && (dto.generated_response !== null || citations.length || fragments.some((item) => item.usage !== 'REFERENCE'))) fail()
  const technicalFailure = dto.operation_status === 'FAILED' || dto.generation_status === 'FAILED'
  if (!technicalFailure && dto.state === 'EVIDENCIA_SUFICIENTE' && (dto.generation_status !== 'SUCCEEDED' || !dto.generated_response || !citations.length)) fail()
  return Object.freeze({ state: dto.state, stateLabel: dto.state === null ? null : RAG_STATES[dto.state], operationId: dto.operation_id, correlationId: dto.correlation_id, operationStatus: dto.operation_status, generationStatus: dto.generation_status, response: technicalFailure ? null : dto.generated_response, message: dto.safe_result_message, contextReferenceId: dto.context_reference_id, fragments: Object.freeze(fragments), citations: Object.freeze(citations), technicalFailure })
}

export function citationLocation(item) {
  return [item.pageStart === null ? null : item.pageEnd && item.pageEnd !== item.pageStart ? `Páginas ${item.pageStart}–${item.pageEnd}` : `Página ${item.pageStart}`, item.section && `Sección ${item.section}`, item.clause && `Cláusula ${item.clause}`].filter(Boolean).join(' · ')
}
