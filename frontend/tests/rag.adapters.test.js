import { describe, expect, it } from 'vitest'
import { adaptRagResponse, citationLocation, RAG_STATES } from '../src/features/rag/adapters.js'

export const fragmentDto = { fragment_id: '33333333-3333-4333-8333-333333333333', fragment_text: 'Texto sintético autorizado.', usage: 'EVIDENCE', document_id: 'DOC-SINTETICO', document_name: 'Documento sintético', document_type: 'PDF', document_date: null, page_start: null, page_end: null, section: null, clause: null }
export function dtoFixture(state = 'EVIDENCIA_SUFICIENTE') {
  const { fragment_text, usage, ...metadata } = fragmentDto
  const enough = state === 'EVIDENCIA_SUFICIENTE'
  return { operation_id: '11111111-1111-4111-8111-111111111111', correlation_id: '22222222-2222-4222-8222-222222222222', state, operation_status: 'COMPLETED', generation_status: enough ? 'SUCCEEDED' : 'NOT_REQUESTED', generated_response: enough ? 'Respuesta sintética [E1].' : null, safe_result_message: enough ? null : 'Mensaje seguro sintético.', context_reference_id: null, fragments: enough ? [fragmentDto] : state === 'EVIDENCIA_INSUFICIENTE' ? [{ ...fragmentDto, usage: 'REFERENCE' }] : [], citations: enough ? [{ handle: 'E1', ...metadata }] : [] }
}

describe('SRS_REQUIRED: adaptadores RAG', () => {
  it.each(Object.keys(RAG_STATES))('preserva el estado funcional %s', (state) => { const result = adaptRagResponse(dtoFixture(state)); expect(result.state).toBe(state); expect(result.stateLabel).toBe(RAG_STATES[state]) })
  it('conserva metadatos nulos sin crear fechas ni páginas', () => { const fragment = adaptRagResponse(dtoFixture()).fragments[0]; expect(fragment.documentDate).toBeNull(); expect(fragment.pageStart).toBeNull(); expect(citationLocation(fragment)).toBe('') })
  it('preserva fragmentos de contexto no utilizados como cita en estado suficiente', () => { const dto = dtoFixture(); dto.fragments.push({ ...fragmentDto, fragment_id: '44444444-4444-4444-8444-444444444444', usage: 'CONTEXT' }); expect(adaptRagResponse(dto).fragments[1].usage).toBe('CONTEXT') })
  it('mantiene evidencia recuperada ante fallo de generación sin inventar respuesta', () => { const dto = dtoFixture(); dto.operation_status = 'FAILED'; dto.generation_status = 'FAILED'; dto.generated_response = null; dto.fragments[0] = { ...fragmentDto, usage: 'CONTEXT' }; dto.citations = []; const result = adaptRagResponse(dto); expect(result.technicalFailure).toBe(true); expect(result.state).toBe('EVIDENCIA_SUFICIENTE'); expect(result.response).toBeNull(); expect(result.fragments).toHaveLength(1) })
  it('no transforma fallo técnico de recuperación con estado null en Sin evidencia', () => { const dto = dtoFixture('SIN_EVIDENCIA'); dto.state = null; dto.operation_status = 'FAILED'; const result = adaptRagResponse(dto); expect(result.state).toBeNull(); expect(result.technicalFailure).toBe(true) })
  it('elimina toda información documental de un DTO de denegación', () => { const dto = dtoFixture(); dto.state = 'SIN_AUTORIZACION'; dto.safe_result_message = 'Metadato no autorizado'; const result = adaptRagResponse(dto); expect(result.fragments).toEqual([]); expect(result.citations).toEqual([]); expect(result.response).toBeNull(); expect(JSON.stringify(result)).not.toContain('DOC-SINTETICO'); expect(result.message).toBe('Acceso no autorizado.') })
  it('rechaza síntesis para evidencia insuficiente', () => { const dto = dtoFixture('EVIDENCIA_INSUFICIENTE'); dto.generated_response = 'Síntesis improcedente'; expect(() => adaptRagResponse(dto)).toThrow() })
  it('rechaza citas huérfanas y procedencia divergente', () => { const dto = dtoFixture(); dto.citations[0].document_id = 'OTRO'; expect(() => adaptRagResponse(dto)).toThrow() })
  it('conserva referencia contextual solo como identificador, no evidencia', () => { const dto = dtoFixture('SIN_EVIDENCIA'); dto.context_reference_id = '44444444-4444-4444-8444-444444444444'; const result = adaptRagResponse(dto); expect(result.contextReferenceId).toBe(dto.context_reference_id); expect(result.fragments).toEqual([]) })
})
describe('ROBUSTNESS: validación del contrato físico', () => {
  it.each(['state', 'operation_status', 'generation_status'])('rechaza enum desconocido %s', (field) => { const dto = dtoFixture(); dto[field] = 'DESCONOCIDO'; expect(() => adaptRagResponse(dto)).toThrow() })
  it('rechaza fragmentos duplicados y más de cinco resultados', () => { const dto = dtoFixture(); dto.fragments = Array(6).fill(fragmentDto); expect(() => adaptRagResponse(dto)).toThrow() })
  it('rechaza fecha imposible y rango de páginas invertido', () => { for (const mutation of [{ document_date: '2030-02-30' }, { page_start: 4, page_end: 2 }]) { const dto = dtoFixture(); dto.fragments = [{ ...fragmentDto, ...mutation }]; expect(() => adaptRagResponse(dto)).toThrow() } })
  it('rechaza respuesta truncada sin arrays', () => { expect(() => adaptRagResponse({ state: 'SIN_EVIDENCIA' })).toThrow() })
})
