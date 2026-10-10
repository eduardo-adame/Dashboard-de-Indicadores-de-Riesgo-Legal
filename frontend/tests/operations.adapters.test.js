import { describe, expect, it } from 'vitest'
import { ApiError } from '../src/api/errors.js'
import {
  auditPage, changeFilters, filterQuery, ingestionResult, ocrPage, ocrResult,
  quarantinePage, readFilters, reinjectionResult, technicalResult,
} from '../src/features/operations/adapters.js'

const id = '11111111-1111-4111-8111-111111111111'
const id2 = '22222222-2222-4222-8222-222222222222'
const ingestion = (overrides = {}) => ({ file_id: id, operation_id: id2, correlation_id: id, state: 'COMPLETADO', format: 'CSV', family: 'CUMPLIMIENTO', routing_target: 'VALIDATION', safe_cause_code: null, idempotent: false, ...overrides })
const ocrItem = (overrides = {}) => ({ document_id: 'DOC-1', document_version_id: id, document_name: 'Documento', file_name: null, processing_state: 'PROCESANDO', ocr_applicable: true, reprocess_eligible: true, ocr_state: 'Pendiente', processed_at: null, confidence: null, total_page_count: null, ocr_processed_page_count: null, granularity: null, outcome: 'Pendiente', ...overrides })
const readOcr = (row) => ocrPage({ items: [row], next_cursor: null }).items[0]

describe('SRS_REQUIRED: adaptadores de operaciones', () => {
  it('distingue recepción de procesamiento downstream', () => {
    expect(ingestionResult(ingestion()).reception).toMatch(/downstream pendiente/)
    expect(ingestionResult(ingestion({ state: 'CUARENTENA' })).reception).toBe('Archivo retenido en cuarentena')
    expect(() => ingestionResult(ingestion({ family: 'INVENTADA' }))).toThrow(ApiError)
  })

  it('preserva original, candidato y estado terminal de cuarentena', () => {
    const original = { Importe: null }; const candidate = { Importe: 0 }
    const page = quarantinePage({ items: [{ id, ingest_file_id: id2, source_record_id: null, row_number: 4, source_family: 'CUMPLIMIENTO', created_at: '2030-01-01T00:00:00Z', cause_code: 'MISSING_REQUIRED_FIELD', cause_description: 'Falta campo', state: 'Pendiente', original_payload: original, candidate_payload: candidate, discard_justification: null }], next_cursor: null })
    expect(page.items[0]).toMatchObject({ original_payload: original, candidate_payload: candidate, canAct: true, canCorrect: true })
    const terminal = quarantinePage({ items: [{ ...page.items[0], state: 'Descartado', discard_justification: 'Dato inválido' }], next_cursor: null })
    expect(terminal.items[0].canAct).toBe(false)
  })

  it('trata la reinyección confirmada con KPI pendiente como efecto confirmado', () => {
    expect(reinjectionResult({ detail: 'La reinyección fue confirmada; el recálculo sigue pendiente' })).toEqual({ upstreamCommitted: true, pending: true, message: 'Reinyección confirmada. El recálculo sigue pendiente.' })
    expect(reinjectionResult({ quarantine_state: 'Reinyectado', job_id: id, operation_id: id2, job_state: 'COMPLETED' })).toMatchObject({ upstreamCommitted: true, pending: false })
    expect(() => reinjectionResult({ detail: 'mensaje no contractual' })).toThrow(ApiError)
  })

  it('trata el reproceso OCR confirmado con KPI pendiente como efecto confirmado', () => {
    expect(ocrResult({ detail: { cause: 'UPSTREAM_COMMITTED_KPI_RETRYABLE', operation_id: id } })).toMatchObject({ operation_id: id, upstreamCommitted: true, pending: true })
    expect(ocrResult({ operation_id: id, document_id: 'DOC-1', document_version_id: id2, ocr_state: 'Completo', processing_state: 'LISTA', kpi_job_id: null, kpi_job_state: null })).toMatchObject({ upstreamCommitted: true, pending: false })
  })

  it('preserva null de confianza OCR sin convertirlo en cero', () => {
    const page = ocrPage({ items: [ocrItem()], next_cursor: null })
    expect(page.items[0].confidenceLabel).toBe('No disponible')
    expect(readOcr(ocrItem({ confidence: 0 })).confidenceLabel).toBe('0')
  })

  it('distingue OCR no aplicable de escaneado sin resultado sin fabricar Pendiente', () => {
    expect(readOcr(ocrItem({ ocr_applicable: false, reprocess_eligible: false, ocr_state: null, outcome: null, processing_state: 'LISTA' }))).toMatchObject({ ocr_state: null, outcome: null, ocrStateLabel: 'No aplica', outcomeLabel: 'No aplica', reprocess_eligible: false })
    expect(readOcr(ocrItem({ reprocess_eligible: false, ocr_state: null, outcome: null }))).toMatchObject({ ocr_state: null, ocrStateLabel: 'Sin resultado OCR', outcomeLabel: 'Sin resultado OCR' })
  })

  it.each(['Pendiente', 'Exitoso', 'Rechazado por baja confianza'])('preserva el estado OCR contractual %s y la decisión de elegibilidad recibida', (state) => {
    expect(readOcr(ocrItem({ ocr_state: state, outcome: state, reprocess_eligible: false }))).toMatchObject({ ocr_state: state, ocrStateLabel: state, outcomeLabel: state, reprocess_eligible: false })
  })

  it('limita filtros, valida zona horaria y reinicia cursor cuando cambian', () => {
    const query = filterQuery({ state: 'Pendiente', limit: '100', cursor: 'abc' }, 'quarantine')
    expect(query).toBe('cursor=abc&limit=100&state=Pendiente')
    expect(changeFilters({ cursor: 'abc', state: 'Pendiente' }, { state: 'Descartado' })).toEqual({ state: 'Descartado' })
    expect(() => filterQuery({ occurred_from: '2030-01-01', limit: '100' }, 'audit')).toThrow(ApiError)
    expect(readFilters('limit=100&query=secreto&action=LOGIN', 'audit')).toEqual({ action: 'LOGIN', limit: '100' })
  })

  it('rechaza intervalos invertidos', () => {
    expect(() => filterQuery({ occurred_from: '2030-02-01T00:00:00Z', occurred_to: '2030-01-01T00:00:00Z' }, 'audit')).toThrow(ApiError)
  })

  it('valida auditoría sin fabricar recursos', () => {
    const event = { id, occurred_at: '2030-01-01T00:00:00Z', actor_type: 'USER', actor_identifier: 'usuario', actor_user_id: id2, action: 'INGEST', resource_type: 'FILE', resource_identifier: 'archivo', result: 'SUCCESS', safe_cause_code: null, operation_id: id, correlation_id: id2, query_sha256: null, resources: [] }
    expect(auditPage({ items: [event], next_cursor: null }).items[0].resources).toEqual([])
    expect(() => auditPage({ items: [{ ...event, operation_id: 'invalido' }], next_cursor: null })).toThrow(ApiError)
  })

  it('presenta CD-03 sin confundir no disponible con cero', () => {
    expect(technicalResult({ kpi_code: 'KPI-CD-03', availability: 'DISPONIBLE', value: '0', calculated_at: '2030-01-01T00:00:00Z' }).displayValue).toBe('0 %')
    expect(technicalResult({ kpi_code: 'KPI-CD-03', availability: 'NO_DISPONIBLE', value: null, calculated_at: null }).displayValue).toBe('No disponible')
  })
})

describe('ROBUSTNESS: DTO OCR estricto y denegación predeterminada', () => {
  it.each([
    { ocr_applicable: undefined }, { reprocess_eligible: undefined },
    { ocr_applicable: 'false' }, { reprocess_eligible: 1 },
    { ocr_state: 'No aplica', outcome: 'No aplica' },
    { ocr_state: 'Sin resultado OCR', outcome: 'Sin resultado OCR' },
    { ocr_state: 'Inventado', outcome: 'Inventado' },
    { ocr_state: undefined, outcome: undefined },
    { outcome: 'Exitoso' }, { confidence: NaN },
  ])('rechaza el DTO inválido %j sin otorgar acciones', (patch) => {
    expect(() => readOcr(ocrItem(patch))).toThrow(ApiError)
  })
})
