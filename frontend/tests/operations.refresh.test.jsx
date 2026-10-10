import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { ApiError } from '../src/api/errors.js'
import { IngestionPage } from '../src/features/operations/IngestionPage.jsx'
import { refreshId, refreshUpload, refreshDispatch, refreshOcrItem, refreshOcrFinal, refreshReprocess, refreshKpi } from './fixtures/operationsRefresh.js'

function mount({ role = 'TI', dispatch = refreshDispatch, reprocess = refreshReprocess, dispatchWait, reprocessWait } = {}) {
  let currentOcr = refreshOcrItem; let value = '50'
  const request = vi.fn(async (path) => {
    if (path === '/ingestion/uploads') return refreshUpload
    if (path === '/coordination/dispatch') {
      if (dispatchWait) await dispatchWait
      if (dispatch instanceof Error) throw dispatch
      currentOcr = refreshOcrFinal; value = '66.66666666666667'
      return dispatch
    }
    if (path.endsWith('/ocr/reprocess')) {
      if (reprocessWait) await reprocessWait
      if (reprocess instanceof Error) throw reprocess
      currentOcr = refreshOcrFinal; value = '100'
      return reprocess
    }
    throw new Error('Ruta sintética inesperada')
  })
  const acquireRead = vi.fn((path) => ({ promise: Promise.resolve(path.startsWith('/documents/ocr') ? { items: [currentOcr], next_cursor: null } : refreshKpi(value)), release: vi.fn() }))
  const client = { request, acquireRead, abortAll: vi.fn() }
  const snapshot = { status: 'authenticated', principal: { id: refreshId, username: 'sintetico', roles: [role] }, generation: 1 }
  const manager = { client, getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn(async () => true) }
  render(<SessionProvider manager={manager}><MemoryRouter initialEntries={['/ingesta?document_id=DOC-REF']}><IngestionPage /></MemoryRouter></SessionProvider>)
  return { request, count: (prefix) => acquireRead.mock.calls.filter(([path]) => path.startsWith(prefix)).length, acquireRead }
}
async function upload() {
  await userEvent.upload(screen.getByLabelText(/Archivo \(máximo/), new File(['sintético'], 'sintetico.pdf', { type: 'application/pdf' }))
  fireEvent.submit(screen.getByRole('button', { name: 'Recibir y procesar' }).closest('form'))
}
async function reprocess() {
  await userEvent.click(await screen.findByRole('button', { name: 'Reprocesar', exact: true }))
  await userEvent.click(screen.getByRole('button', { name: 'Confirmar reproceso', exact: true }))
}

describe('SRS_REQUIRED: actualización OCR y CD03 por resultado confirmado', () => {
  it.each(['TI', 'ANALISTA'])('DOCUMENT COMPLETED refresca OCR para %s y CD03 sólo para TI', async (role) => {
    const state = mount({ role }); await screen.findByText('DOC-REF')
    await upload()
    await waitFor(() => expect(state.count('/documents/ocr')).toBe(2))
    expect(await screen.findAllByText('Exitoso')).toHaveLength(2)
    expect(state.count('/technical/kpis')).toBe(role === 'TI' ? 2 : 0)
    expect(screen.getByLabelText('Documento conocido')).toHaveValue('DOC-REF')
    expect(state.acquireRead.mock.calls.filter(([p]) => p.startsWith('/documents/ocr')).every(([p]) => p.includes('document_id=DOC-REF'))).toBe(true)
    if (role === 'TI') expect(await screen.findByText('66.66666666666667 %')).toBeVisible()
  })

  it('recepción no invalida mientras dispatch no confirma y la llegada preserva el modal OCR', async () => {
    let complete; const pending = new Promise((resolve) => { complete = resolve })
    const state = mount({ dispatchWait: pending }); await screen.findByText('DOC-REF')
    await upload()
    await waitFor(() => expect(state.request).toHaveBeenCalledWith('/coordination/dispatch', expect.anything()))
    expect(state.count('/documents/ocr')).toBe(1); expect(state.count('/technical/kpis')).toBe(1)
    await userEvent.click(screen.getByRole('button', { name: 'Reprocesar', exact: true }))
    const dialog = screen.getByRole('dialog')
    await act(async () => complete())
    await waitFor(() => expect(state.count('/documents/ocr')).toBe(2))
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(screen.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeEnabled()
    expect(screen.getByLabelText('Documento conocido')).toHaveValue('DOC-REF')
  })

  it.each(['LISTA', 'RECHAZADA', 'FALLIDA'])('reproceso terminal %s confirmado refresca CD03 y su propia lista OCR', async (processing_state) => {
    const state = mount({ reprocess: { ...refreshReprocess, processing_state } }); await screen.findByText('DOC-REF')
    await reprocess()
    expect(await screen.findByText('100 %')).toBeVisible()
    expect(state.count('/technical/kpis')).toBe(2)
    expect(state.count('/documents/ocr')).toBe(2)
    expect(screen.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeDisabled()
    expect(state.request).toHaveBeenCalledTimes(1)
  })

  it('Analista refresca su reproceso OCR sin consultar ni mostrar CD03', async () => {
    const state = mount({ role: 'ANALISTA' }); await screen.findByText('DOC-REF')
    await reprocess()
    await waitFor(() => expect(state.count('/documents/ocr')).toBe(2))
    expect(state.count('/technical/kpis')).toBe(0)
    expect(screen.queryByText('Calidad técnica de OCR')).not.toBeInTheDocument()
  })
})

describe('ROBUSTNESS: invalidación acotada por lifecycle sin disponibilidad supuesta', () => {
  it.each([
    ['DOCUMENT pendiente', { ...refreshDispatch, state: 'PENDING' }],
    ['VALIDATION completado', { ...refreshDispatch, downstream_target: 'VALIDATION' }],
    ['DOCUMENT sin confirmación', { ...refreshDispatch, state: 'FAILED' }],
    ['error de despacho', new ApiError(503)],
  ])('%s no invalida OCR ni CD03', async (_, dispatch) => {
    const state = mount({ dispatch }); await screen.findByText('DOC-REF')
    await upload()
    await waitFor(() => expect(state.request).toHaveBeenCalledTimes(2))
    if (dispatch instanceof Error) await screen.findByRole('alert')
    else await screen.findByText(dispatch.state)
    expect(state.count('/documents/ocr')).toBe(1); expect(state.count('/technical/kpis')).toBe(1)
    expect(screen.getByText('50 %')).toBeVisible()
  })

  it.each([
    ['KPI pendiente normal', { ...refreshReprocess, kpi_job_state: 'PENDING' }, true],
    ['503 upstream confirmado', { detail: { cause: 'UPSTREAM_COMMITTED_KPI_RETRYABLE', operation_id: refreshId } }, true],
    ['procesamiento todavía en curso', { ...refreshReprocess, processing_state: 'PROCESANDO' }, true],
    ['POST rechazado', new ApiError(409), false],
  ])('%s no invalida CD03 ni inventa disponibilidad', async (_, result, reloadOcr) => {
    const state = mount({ reprocess: result }); await screen.findByText('DOC-REF')
    await reprocess()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeDisabled())
    expect(state.count('/technical/kpis')).toBe(1)
    expect(state.count('/documents/ocr')).toBe(reloadOcr ? 2 : 1)
    expect(screen.getByText('50 %')).toBeVisible()
  })

  it('refresh de dispatch no desmonta ni duplica un reproceso OCR que sigue en curso', async () => {
    let completeDispatch; let completeReprocess
    const dispatchWait = new Promise((resolve) => { completeDispatch = resolve })
    const reprocessWait = new Promise((resolve) => { completeReprocess = resolve })
    const state = mount({ dispatchWait, reprocessWait }); await screen.findByText('DOC-REF')
    await upload()
    await waitFor(() => expect(state.request).toHaveBeenCalledTimes(2))
    await reprocess()
    await waitFor(() => expect(state.request).toHaveBeenCalledTimes(3))
    const dialog = screen.getByRole('dialog')
    await act(async () => completeDispatch())
    await waitFor(() => expect(state.count('/documents/ocr')).toBe(2))
    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(screen.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toHaveAttribute('aria-busy', 'true')
    await act(async () => completeReprocess())
    expect(await screen.findByText('100 %')).toBeVisible()
    expect(state.count('/documents/ocr')).toBe(3); expect(state.count('/technical/kpis')).toBe(3)
    expect(state.request.mock.calls.filter(([path]) => path.endsWith('/ocr/reprocess'))).toHaveLength(1)
  })
})
