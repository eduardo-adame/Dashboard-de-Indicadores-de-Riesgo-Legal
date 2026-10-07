import { describe, expect, it, vi } from 'vitest'
import { createApiClient } from '../src/api/client.js'
import { createRagSubmission, requestRag } from '../src/features/rag/api.js'
import { navigation, routes } from '../src/features/rag/routes.jsx'

function dtoFixture() {
  const metadata = { fragment_id: '33333333-3333-4333-8333-333333333333', document_id: 'DOC-SINTETICO', document_name: 'Documento sintético', document_type: 'PDF', document_date: null, page_start: null, page_end: null, section: null, clause: null }
  return { operation_id: '11111111-1111-4111-8111-111111111111', correlation_id: '22222222-2222-4222-8222-222222222222', state: 'EVIDENCIA_SUFICIENTE', operation_status: 'COMPLETED', generation_status: 'SUCCEEDED', generated_response: 'Respuesta sintética [E1].', safe_result_message: null, context_reference_id: null, fragments: [{ ...metadata, fragment_text: 'Texto sintético autorizado.', usage: 'EVIDENCE' }], citations: [{ handle: 'E1', ...metadata }] }
}

const session = () => ({ token: 'token-sintetico-no-operativo', generation: 1 })
describe('SRS_REQUIRED: transporte RAG e idempotencia', () => {
  it('registra consulta y visor bajo la única capacidad document.query', () => {
    expect(routes).toHaveLength(1)
    expect(routes[0]).toMatchObject({ id: 'rag-layout', capability: 'document.query' })
    expect(routes[0].children.map((route) => route.path)).toEqual(['/consulta', '/documentos'])
    expect(navigation.map(({ path, capability }) => [path, capability])).toEqual([
      ['/consulta', 'document.query'],
      ['/documentos', 'document.query'],
    ])
  })
  it('usa una identidad nueva para cada consulta explícita', () => { const first = createRagSubmission('Consulta sintética'); const second = createRagSubmission('Consulta sintética'); expect(first.key).not.toBe(second.key) })
  it('reintento explícito conserva query contexto y clave de la operación', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: true, status: 200, json: async () => dtoFixture() }))
    const client = createApiClient({ fetchImpl, getSession: session })
    const submission = createRagSubmission('Consulta\n sintética ', '44444444-4444-4444-8444-444444444444')
    await requestRag(client, submission); await requestRag(client, submission)
    for (const [path, options] of fetchImpl.mock.calls) { expect(path).toBe('/api/rag/query'); expect(options.method).toBe('POST'); expect(options.headers.get('Idempotency-Key')).toBe(submission.key); expect(JSON.parse(options.body)).toEqual(submission.payload) }
  })
  it('consume DTO válido con HTTP503 y conserva el estado suficiente', async () => { const dto = dtoFixture(); dto.operation_status = 'FAILED'; dto.generation_status = 'FAILED'; dto.generated_response = null; const client = createApiClient({ getSession: session, fetchImpl: async () => ({ ok: false, status: 503, json: async () => dto }) }); const result = await requestRag(client, createRagSubmission('Consulta')); expect(result.state).toBe('EVIDENCIA_SUFICIENTE'); expect(result.technicalFailure).toBe(true); expect(result.fragments).toHaveLength(1) })
  it('un 401 confirmado renueva sesión y conserva la identidad idempotente del POST', async () => {
    let token = 'token-inicial-sintetico'
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401, json: vi.fn() })
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => dtoFixture() })
    const recover = vi.fn(async () => { token = 'token-renovado-sintetico'; return true })
    const client = createApiClient({ fetchImpl, recover, getSession: () => ({ token, generation: 1 }) })
    const submission = createRagSubmission('Consulta')
    await expect(requestRag(client, submission)).resolves.toMatchObject({ state: 'EVIDENCIA_SUFICIENTE' })
    expect(recover).toHaveBeenCalledTimes(1)
    expect(fetchImpl).toHaveBeenCalledTimes(2)
    expect(fetchImpl.mock.calls[0][1].headers.get('Idempotency-Key')).toBe(submission.key)
    expect(fetchImpl.mock.calls[1][1].headers.get('Idempotency-Key')).toBe(submission.key)
    expect(fetchImpl.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer token-inicial-sintetico')
    expect(fetchImpl.mock.calls[1][1].headers.get('Authorization')).toBe('Bearer token-renovado-sintetico')
  })
  it('HTTP403 no consume ni expone el cuerpo protegido y revalida identidad', async () => { const json = vi.fn(); const revalidate = vi.fn(); const client = createApiClient({ getSession: session, onForbidden: revalidate, fetchImpl: async () => ({ ok: false, status: 403, json }) }); await expect(requestRag(client, createRagSubmission('Consulta'))).rejects.toMatchObject({ status: 403 }); expect(json).not.toHaveBeenCalled(); expect(revalidate).toHaveBeenCalledTimes(1) })
  it('respuesta incierta nunca repite automáticamente el POST', async () => { const fetchImpl = vi.fn(async () => { throw new TypeError('Red no disponible') }); const client = createApiClient({ fetchImpl, getSession: session }); await expect(requestRag(client, createRagSubmission('Consulta'))).rejects.toMatchObject({ uncertain: true }); expect(fetchImpl).toHaveBeenCalledTimes(1) })
})
describe('ROBUSTNESS: entradas acotadas', () => {
  it.each(['', 'x'.repeat(4097)])('rechaza longitud inválida antes de enviar', (query) => { expect(() => createRagSubmission(query)).toThrow() })
  it('rechaza claves y referencia contextual no UUID', () => { expect(() => createRagSubmission('Consulta', 'invalido')).toThrow(); expect(() => createRagSubmission('Consulta', null, 'invalido')).toThrow() })
  it('no introduce filtros ejecutivos en el payload de consulta', () => { expect(Object.keys(createRagSubmission('Consulta').payload)).toEqual(['query']) })
})
