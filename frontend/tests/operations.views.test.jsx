import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { ApiError } from '../src/api/errors.js'
import { IngestionPage } from '../src/features/operations/IngestionPage.jsx'
import { QuarantinePage } from '../src/features/operations/QuarantinePage.jsx'
import { AuditPage } from '../src/features/operations/AuditPage.jsx'
import { OcrPanel } from '../src/features/operations/OcrPanel.jsx'

const accountId = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
const ingestion = { file_id: accountId, operation_id: otherId, correlation_id: accountId, state: 'COMPLETADO', format: 'CSV', family: 'CUMPLIMIENTO', routing_target: 'VALIDATION', safe_cause_code: null, idempotent: false }
const quarantine = { items: [{ id: otherId, ingest_file_id: accountId, source_record_id: null, row_number: 2, source_family: 'CUMPLIMIENTO', created_at: '2030-01-01T00:00:00Z', cause_code: 'MISSING_REQUIRED_FIELD', cause_description: 'Falta campo obligatorio', state: 'Pendiente', original_payload: { Dato: null }, candidate_payload: { Dato: 'nuevo' }, discard_justification: null }], next_cursor: null }
const ocr = { items: [{ document_id: 'DOC-1', document_version_id: otherId, document_name: 'Documento', file_name: null, processing_state: 'PROCESANDO', ocr_applicable: true, reprocess_eligible: true, ocr_state: 'Pendiente', processed_at: null, confidence: null, total_page_count: null, ocr_processed_page_count: null, granularity: null, outcome: 'Pendiente' }], next_cursor: null }
const audit = { items: [{ id: otherId, occurred_at: '2030-01-01T00:00:00Z', actor_type: 'USER', actor_identifier: 'cuenta-sintetica', actor_user_id: accountId, action: 'INGEST', resource_type: 'FILE', resource_identifier: 'archivo', result: 'SUCCESS', safe_cause_code: null, operation_id: otherId, correlation_id: accountId, query_sha256: null, resources: [] }], next_cursor: null }

function mount(element, { roles = ['ANALISTA'], request, reads } = {}) {
  const client = {
    request: request || vi.fn(async (path) => {
      if (path === '/ingestion/uploads') return ingestion
      if (path === '/coordination/dispatch') return { downstream_target: 'VALIDATION', operation_id: otherId, downstream_result_id: null, state: 'COMPLETED' }
      return null
    }),
    acquireRead: vi.fn((path) => ({ promise: Promise.resolve(reads?.(path) ?? (path.startsWith('/documents/ocr') ? ocr : path.startsWith('/validation/quarantine') ? quarantine : path.startsWith('/audit/events') ? audit : { kpi_code: 'KPI-CD-03', availability: 'DISPONIBLE', value: '99.5', calculated_at: '2030-01-01T00:00:00Z' })), release: vi.fn() })),
    abortAll: vi.fn(),
  }
  const snapshot = { status: 'authenticated', principal: { id: accountId, username: 'cuenta-sintetica', roles }, generation: 1 }
  const manager = { client, getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn(async () => true) }
  render(<SessionProvider manager={manager}><MemoryRouter>{element}</MemoryRouter></SessionProvider>)
  return { client, manager }
}

describe('SRS_REQUIRED: vistas operativas', () => {
  it('deniega una página sin capacidad antes de leer datos', () => {
    const { client } = mount(<IngestionPage />, { roles: ['JURIDICO'] })
    expect(screen.getByRole('alert')).toHaveTextContent('Acceso no autorizado')
    expect(client.acquireRead).not.toHaveBeenCalled()
  })

  it('mantiene recepción visible si el despacho posterior entra en conflicto', async () => {
    const request = vi.fn(async (path) => { if (path === '/ingestion/uploads') return ingestion; throw new ApiError(409) })
    mount(<IngestionPage />, { request })
    const file = new File(['Dato\n1'], 'dato.csv', { type: 'text/csv' })
    await userEvent.upload(screen.getByLabelText(/Archivo/), file)
    fireEvent.submit(screen.getByRole('button', { name: 'Recibir y procesar' }).closest('form'))
    await waitFor(() => expect(request).toHaveBeenCalled())
    expect(await screen.findByText(/Archivo recibido; procesamiento downstream pendiente/)).toBeVisible()
    expect(screen.getByText('Sin confirmación de despacho')).toBeVisible()
    expect(await screen.findByRole('alert')).toHaveTextContent('conflicto')
    expect(screen.getByText(/no representa un historial persistente/i)).toBeVisible()
  })

  it('separa original y candidato y conserva identidad de reinyección', async () => {
    const request = vi.fn(async () => ({ detail: 'La reinyección fue confirmada; el recálculo sigue pendiente' }))
    mount(<QuarantinePage />, { request })
    await screen.findByText('Falta campo obligatorio')
    await userEvent.click(screen.getByRole('button', { name: 'Corregir' }))
    expect(screen.getByText('Original rechazado — solo lectura')).toBeVisible()
    const editor = screen.getByLabelText('Candidato corregido (JSON)')
    expect(editor).toHaveValue(JSON.stringify({ Dato: 'nuevo' }, null, 2))
    fireEvent.change(editor, { target: { value: '{"Dato":"corregido"}' } })
    await userEvent.click(screen.getByRole('button', { name: 'Validar y reinyectar' }))
    expect((await screen.findAllByText('Reinyección confirmada. El recálculo sigue pendiente.')).length).toBeGreaterThan(0)
    expect(request.mock.calls[0][0]).toBe(`/validation/quarantine/${otherId}/reinject`)
    expect(request.mock.calls[0][1].body.corrected_payload).toEqual({ Dato: 'corregido' })
    expect(request.mock.calls[0][1].body.correlation_id).toMatch(/^[0-9a-f-]{36}$/i)
  })

  it('descarta únicamente después de justificación y confirmación', async () => {
    const request = vi.fn(async () => ({ id: otherId, state: 'Descartado', discard_justification: 'Duplicado' }))
    mount(<QuarantinePage />, { request }); await screen.findByText('Falta campo obligatorio')
    await userEvent.click(screen.getByRole('button', { name: 'Descartar' }))
    const submit = screen.getByRole('button', { name: 'Confirmar descarte' }); expect(submit).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Justificación del descarte'), 'Duplicado'); expect(submit).toBeEnabled()
    await userEvent.click(submit); expect(await screen.findByText('Descarte confirmado.')).toBeVisible()
  })

  it('reproceso OCR confirmado con KPI pendiente no se repite', async () => {
    const request = vi.fn(async () => ({ detail: { cause: 'UPSTREAM_COMMITTED_KPI_RETRYABLE', operation_id: accountId } }))
    mount(<IngestionPage />, { request }); await screen.findByText('DOC-1')
    await userEvent.click(screen.getByRole('button', { name: 'Reprocesar' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirmar reproceso' }))
    expect(await screen.findByText('Reproceso confirmado. Trabajo posterior pendiente.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Confirmar reproceso' })).toBeDisabled()
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('auditoría distingue alcance propio de alcance total', async () => {
    const analyst = mount(<AuditPage />); expect(await screen.findByText(/Tus eventos autorizados/)).toBeVisible(); expect(analyst.client.acquireRead.mock.calls[0][0]).toContain('limit=100')
  })

  it('filtros inválidos de cuarentena muestran error seguro y no cambian la consulta', async () => {
    const { client } = mount(<QuarantinePage />); await screen.findByText('Falta campo obligatorio'); const before = client.acquireRead.mock.calls.length
    await userEvent.type(screen.getByLabelText('Rechazo desde (fecha con zona)'), '2030-01-01')
    await userEvent.tab()
    expect(await screen.findByRole('alert')).toHaveTextContent('Revisa los datos')
    expect(client.acquireRead).toHaveBeenCalledTimes(before)
  })

  it.each(['TI', 'ANALISTA'])('OCR autorizado para %s separa estados y ofrece únicamente acciones elegibles', async (role) => {
    const cases = [
      ['DOCX-NATIVO', false, null, false, 'LISTA', null, 'No aplica'],
      ['PDF-TEXTO', false, null, false, 'LISTA', null, 'No aplica'],
      ['ESCANEADO-SIN-OCR', true, null, false, 'PROCESANDO', null, 'Sin resultado OCR'],
      ['ESCANEADO-PENDIENTE', true, 'Pendiente', false, 'PROCESANDO', null, 'Pendiente'],
      ['ESCANEADO-RECHAZADO', true, 'Rechazado por baja confianza', true, 'RECHAZADA', 0, 'Rechazado por baja confianza'],
      ['ESCANEADO-EXITOSO', true, 'Exitoso', false, 'LISTA', 0.96, 'Exitoso'],
      ['ESCANEADO-FALLIDO', true, null, false, 'FALLIDA', null, 'Sin resultado OCR'],
    ]
    const items = cases.map(([document_id, ocr_applicable, ocr_state, reprocess_eligible, processing_state, confidence]) => ({ ...ocr.items[0], document_id, ocr_applicable, ocr_state, outcome: ocr_state, reprocess_eligible, processing_state, confidence }))
    mount(<OcrPanel />, { roles: [role], reads: () => ({ items, next_cursor: null }) })
    await screen.findByText('DOCX-NATIVO')
    for (const [id, , , eligible, processing, confidence, label] of cases) {
      const row = within(screen.getByText(id).closest('tr'))
      expect(row.getAllByText(label)).toHaveLength(2)
      expect(row.getByText(processing)).toBeVisible()
      expect(row.getByText(confidence === null ? 'No disponible' : String(confidence))).toBeVisible()
      expect(Boolean(row.queryByRole('button', { name: 'Reprocesar' }))).toBe(eligible)
    }
    expect(screen.getAllByRole('button', { name: 'Reprocesar' })).toHaveLength(1)
  })

  it('OCR deniega Jurídico sin consultar datos ni ofrecer reproceso', () => {
    const { client } = mount(<OcrPanel />, { roles: ['JURIDICO'] })
    expect(screen.getByRole('alert')).toHaveTextContent('Acceso no autorizado')
    expect(client.acquireRead).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Reprocesar' })).not.toBeInTheDocument()
  })

  it('un rechazo vigente del servidor no confirma el reproceso ni repite el POST', async () => {
    const request = vi.fn(async () => { throw new ApiError(409) })
    mount(<OcrPanel />, { request })
    await userEvent.click(await screen.findByRole('button', { name: 'Reprocesar' }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirmar reproceso' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('conflicto')
    expect(screen.queryByText(/Reproceso confirmado/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirmar reproceso' })).toBeDisabled()
    expect(request).toHaveBeenCalledTimes(1)
  })
})

describe('ROBUSTNESS: contratos de estado', () => {
  it('OCR sin los booleanos obligatorios falla cerrado sin tabla parcial ni acciones', async () => {
    const { ocr_applicable, ...malformed } = ocr.items[0]
    mount(<OcrPanel />, { reads: () => ({ items: [ocr.items[0], malformed], next_cursor: null }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('servicio no está disponible')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reprocesar' })).not.toBeInTheDocument()
  })
  it('un DTO ilegible no presenta datos parciales', async () => {
    mount(<AuditPage />, { reads: () => ({ items: [{ dato: 'inválido' }], next_cursor: null }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('servicio no está disponible')
    expect(screen.queryByText('inválido')).not.toBeInTheDocument()
  })
})
