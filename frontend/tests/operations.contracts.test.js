import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api/errors.js'
import { createOperationsApi, queryPath } from '../src/features/operations/api.js'
import * as operations from '../src/features/operations/routes.jsx'
import { buildRegistry } from '../src/routing/registry.jsx'

const id = '11111111-1111-4111-8111-111111111111'
const id2 = '22222222-2222-4222-8222-222222222222'
const response = { file_id: id, operation_id: id2, correlation_id: id, state: 'COMPLETADO', format: 'CSV', family: 'CUMPLIMIENTO', routing_target: 'VALIDATION', safe_cause_code: null, idempotent: false }

function fixture(result = null) {
  const client = { request: vi.fn(async () => result) }
  return { api: createOperationsApi(client), request: client.request }
}

describe('SRS_REQUIRED: contratos HTTP de operaciones', () => {
  it('envía carga multipart con clave de idempotencia y ubicación controlada', async () => {
    const { api, request } = fixture(response); const file = new File(['dato'], 'dato.csv', { type: 'text/csv' })
    await api.upload(file, 'compliance', id)
    expect(request).toHaveBeenCalledWith('/ingestion/uploads', expect.objectContaining({ method: 'POST', headers: { 'Idempotency-Key': id }, body: expect.any(FormData) }))
    expect(request.mock.calls[0][1].body.get('controlled_location')).toBe('compliance')
    expect(request.mock.calls[0][1].body.get('file')).toBe(file)
  })

  it('ejecuta ubicación y exige que el conteo coincida', async () => {
    const { api, request } = fixture({ processed: 1, results: [response] })
    await expect(api.run('compliance')).resolves.toHaveLength(1)
    expect(request).toHaveBeenCalledWith('/ingestion/runs', { method: 'POST', body: { controlled_location: 'compliance' } })
    const invalid = fixture({ processed: 2, results: [response] })
    await expect(invalid.api.run('compliance')).rejects.toMatchObject({ status: 503 })
  })

  it('despacha solo identidades UUID por la frontera física', async () => {
    const { api, request } = fixture({ downstream_target: 'VALIDATION', operation_id: id2, downstream_result_id: null, state: 'COMPLETED' })
    await api.dispatch(id, id2)
    expect(request).toHaveBeenCalledWith('/coordination/dispatch', { method: 'POST', body: { file_id: id, correlation_id: id2 } })
    await expect(api.dispatch('invalido', id2)).rejects.toBeInstanceOf(ApiError)
  })

  it('descarta con justificación y no acepta vacío', async () => {
    const { api, request } = fixture({ id, state: 'Descartado', discard_justification: 'Duplicado' })
    await api.discard(id, 'Duplicado')
    expect(request).toHaveBeenCalledWith(`/validation/quarantine/${id}/discard`, { method: 'POST', body: { justification: 'Duplicado' } })
    await expect(api.discard(id, '   ')).rejects.toMatchObject({ status: 422 })
  })

  it('reinyecta con identidad estable y acepta exclusivamente el 503 contractual', async () => {
    const { api, request } = fixture({ detail: 'La reinyección fue confirmada; el recálculo sigue pendiente' })
    await api.reinject(id, { Dato: 1 }, id2)
    expect(request).toHaveBeenCalledWith(`/validation/quarantine/${id}/reinject`, { method: 'PATCH', body: { corrected_payload: { Dato: 1 }, correlation_id: id2 }, acceptStatuses: [503] })
  })

  it('reprocesa OCR conservando request_id para el mismo intento', async () => {
    const dto = { operation_id: id, document_id: 'DOC-1', document_version_id: id2, ocr_state: 'Completo', processing_state: 'LISTA', kpi_job_id: null, kpi_job_state: null }
    const { api, request } = fixture(dto); const item = { document_id: 'DOC/1', document_version_id: id2 }
    await api.reprocess(item, id)
    expect(request).toHaveBeenCalledWith('/documents/DOC%2F1/ocr/reprocess', { method: 'POST', body: { source_document_version_id: id2, request_id: id }, acceptStatuses: [503] })
  })

  it.each([
    ['update', { account_id: id, display_name: 'Nombre' }, `/security/users/${id}`, 'PATCH', { display_name: 'Nombre' }],
    ['activate', { account_id: id }, `/security/users/${id}/activate`, 'POST', undefined],
    ['disable', { account_id: id }, `/security/users/${id}/disable`, 'POST', undefined],
    ['reset', { account_id: id, password: 'temporal' }, `/security/users/${id}/reset-access`, 'POST', { password: 'temporal' }],
    ['roles', { account_id: id, roles: ['ANALISTA'] }, `/security/users/${id}/roles`, 'PUT', { roles: ['ANALISTA'] }],
    ['scope', { role_id: 'ANALISTA', source_family: 'CUMPLIMIENTO', active: true }, '/security/document-scopes/ANALISTA/CUMPLIMIENTO', 'PUT', { active: true }],
    ['user-exception', { account_id: id, document_id: 'DOC 1', decision: 'DENY', active: true }, `/security/document-exceptions/user/${id}/DOC%201`, 'PUT', { decision: 'DENY', active: true }],
    ['role-exception', { role_id: 'JURIDICO', document_id: 'DOC 1', decision: 'ALLOW', active: false }, '/security/document-exceptions/role/JURIDICO/DOC%201', 'PUT', { decision: 'ALLOW', active: false }],
  ])('usa la ruta administrativa real para %s', async (action, fields, path, method, body) => {
    const { api, request } = fixture(null); await api.administer(action, fields)
    expect(request).toHaveBeenCalledWith(path, { method, body })
  })

  it('crea cuenta sin colocar contraseña en URL', async () => {
    const { api, request } = fixture({ id }); await api.administer('create', { username: 'usuario', display_name: 'Usuario', password: 'temporal', roles: ['JURIDICO'] })
    expect(request.mock.calls[0][0]).toBe('/security/users'); expect(request.mock.calls[0][0]).not.toContain('temporal')
    expect(request.mock.calls[0][1].body.password).toBe('temporal')
  })

  it('construye lecturas con whitelist y cursor ligado al filtro', () => {
    expect(queryPath('audit', { action: 'LOGIN', limit: '100', cursor: 'abc' })).toBe('/audit/events?action=LOGIN&cursor=abc&limit=100')
  })

  it('registra cuatro rutas con capacidades default-deny', () => {
    const registry = buildRegistry({ operations })
    expect(registry.routes.map((route) => route.path)).toEqual(['/ingesta', '/cuarentena', '/auditoria', '/administracion'])
    expect(registry.routes.map((route) => route.capability)).toEqual(['ingest.upload', 'quarantine.read', 'audit.read.own', 'user.create'])
  })
})
