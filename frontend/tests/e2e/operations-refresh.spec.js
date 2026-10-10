/** Contrato HTTP sintético de refresh; sin DB, credenciales reales ni mutación persistida. */
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty, ME_RESPONSE_TI } from './helpers.js'
import { refreshId, refreshUpload, refreshDispatch, refreshOcrItem, refreshOcrFinal, refreshReprocess, refreshKpi } from '../fixtures/operationsRefresh.js'

async function prepare(page, { role = 'TI', dispatch = refreshDispatch, reprocess = refreshReprocess, dispatchWait } = {}) {
  await mockSessionTI(page); await mockDashboardEmpty(page)
  const principal = { ...ME_RESPONSE_TI, roles: [role] }
  for (const path of ['me', 'revalidate']) await page.route(`**/api/auth/${path}`, (r) => r.fulfill({ json: principal }))
  let currentOcr = refreshOcrItem; let value = '50'; let ocrReads = 0; let technicalReads = 0; let reprocessPosts = 0
  const ocrQueries = []
  await page.route('**/api/documents/ocr**', (r) => {
    ocrReads++; ocrQueries.push(new URL(r.request().url()).searchParams.get('document_id'))
    return r.fulfill({ json: { items: [currentOcr], next_cursor: null } })
  })
  await page.route('**/api/technical/kpis/KPI-CD-03', (r) => { technicalReads++; return r.fulfill({ json: refreshKpi(value) }) })
  await page.route('**/api/ingestion/uploads', (r) => r.fulfill({ json: refreshUpload }))
  await page.route('**/api/coordination/dispatch', async (r) => {
    if (dispatchWait) await dispatchWait
    if (dispatch === 503) return r.fulfill({ status: 503, json: { detail: 'No confirmado' } })
    currentOcr = refreshOcrFinal; value = '66.66666666666667'
    return r.fulfill({ json: dispatch })
  })
  await page.route('**/api/documents/*/ocr/reprocess', (r) => {
    reprocessPosts++
    currentOcr = refreshOcrFinal; value = '100'
    return r.fulfill({ status: reprocess.detail ? 503 : 200, json: reprocess })
  })
  await page.goto('/ingesta?document_id=DOC-REF')
  await expect(page.getByRole('cell', { name: 'DOC-REF', exact: true })).toHaveCount(1)
  if (role === 'TI') await expect(page.getByText('50 %', { exact: true })).toBeVisible()
  await page.evaluate(() => { window.__refreshTestIdentity = 'misma-pagina' })
  return { ocr: () => ocrReads, technical: () => technicalReads, posts: () => reprocessPosts, queries: ocrQueries }
}
async function upload(page) {
  await page.getByLabel(/Archivo \(máximo/).setInputFiles({ name: 'sintetico.pdf', mimeType: 'application/pdf', buffer: Buffer.from('PDF sintético para contrato') })
  await page.getByRole('button', { name: 'Recibir y procesar', exact: true }).click()
}
async function assertSamePage(page) {
  expect(await page.evaluate(() => window.__refreshTestIdentity)).toBe('misma-pagina')
  await expect(page.getByLabel('Documento conocido')).toHaveValue('DOC-REF')
  expect(new URL(page.url()).searchParams.get('document_id')).toBe('DOC-REF')
}

test.describe('SRS_REQUIRED: refresh OCR/CD03 confirmado — BROWSER_CONTRACT_E2E', () => {
  for (const role of ['TI', 'ANALISTA']) test(`DOCUMENT COMPLETED refresca automáticamente para ${role} sin hard reload`, async ({ page }) => {
    const state = await prepare(page, { role })
    const ocrResponse = page.waitForResponse((r) => r.url().includes('/api/documents/ocr?'))
    await upload(page); await ocrResponse
    const table = page.getByRole('table', { name: 'Resultados operativos OCR' })
    await expect(table.getByRole('cell', { name: 'Exitoso', exact: true })).toHaveCount(2)
    if (role === 'TI') await expect(page.getByText('66.66666666666667 %', { exact: true })).toBeVisible()
    expect(state.ocr()).toBe(2); expect(state.technical()).toBe(role === 'TI' ? 2 : 0)
    expect(state.queries).toEqual(['DOC-REF', 'DOC-REF'])
    await assertSamePage(page)
  })

  test('reproceso terminal confirmado refresca CD03 sin repetir POST ni hard reload', async ({ page }) => {
    const state = await prepare(page)
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    await page.getByRole('button', { name: 'Confirmar reproceso', exact: true }).click()
    await expect(page.getByText('100 %', { exact: true })).toBeVisible()
    await expect(page.getByRole('dialog').getByRole('status')).toHaveText('Reproceso confirmado.')
    expect(state.ocr()).toBe(2); expect(state.technical()).toBe(2); expect(state.posts()).toBe(1)
    await assertSamePage(page)
  })
})

test.describe('ROBUSTNESS: refresh acotado y conservación de interacción — BROWSER_CONTRACT_E2E', () => {
  for (const [label, dispatch] of [
    ['dispatch DOCUMENT pendiente', { ...refreshDispatch, state: 'PENDING' }],
    ['dispatch VALIDATION completado', { ...refreshDispatch, downstream_target: 'VALIDATION' }],
    ['dispatch 503 no confirmado', 503],
  ]) test(`${label} no invalida OCR/CD03`, async ({ page }) => {
    const state = await prepare(page, { dispatch })
    const response = page.waitForResponse((r) => r.url().endsWith('/api/coordination/dispatch'))
    await upload(page); await response
    if (dispatch === 503) await expect(page.getByRole('alert')).toContainText('El servicio no está disponible temporalmente')
    else await expect(page.getByRole('table', { name: 'Recepción y despacho actuales' }).getByRole('cell', { name: dispatch.state, exact: true })).toHaveCount(1)
    expect(state.ocr()).toBe(1); expect(state.technical()).toBe(1)
    await expect(page.getByText('50 %', { exact: true })).toBeVisible()
    await assertSamePage(page)
  })

  test('reproceso 503 upstream confirmado recarga OCR pero no declara CD03 actualizado', async ({ page }) => {
    const state = await prepare(page, { reprocess: { detail: { cause: 'UPSTREAM_COMMITTED_KPI_RETRYABLE', operation_id: refreshId } } })
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    const ocrResponse = page.waitForResponse((r) => r.url().includes('/api/documents/ocr?'))
    await page.getByRole('button', { name: 'Confirmar reproceso', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('status')).toContainText('El recálculo sigue pendiente')
    await ocrResponse
    expect(state.ocr()).toBe(2); expect(state.technical()).toBe(1); expect(state.posts()).toBe(1)
    await expect(page.getByText('50 %', { exact: true })).toBeVisible()
    await assertSamePage(page)
  })

  test('confirmación DOCUMENT recibida durante un modal OCR conserva su selección y foco', async ({ page }) => {
    let release; const pending = new Promise((resolve) => { release = resolve })
    const state = await prepare(page, { dispatchWait: pending })
    const dispatchRequest = page.waitForRequest((r) => r.url().endsWith('/api/coordination/dispatch'))
    await upload(page); await dispatchRequest
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    const submit = page.getByRole('button', { name: 'Confirmar reproceso', exact: true })
    await submit.focus()
    const ocrResponse = page.waitForResponse((r) => r.url().includes('/api/documents/ocr?'))
    release(); await ocrResponse
    await expect(page.getByRole('dialog')).toBeVisible()
    await expect(submit).toBeEnabled(); await expect(submit).toBeFocused()
    expect(state.posts()).toBe(0); expect(state.ocr()).toBe(2)
    await assertSamePage(page)
  })
})
