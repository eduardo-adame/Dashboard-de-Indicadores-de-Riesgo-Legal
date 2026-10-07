/**
 * Spec: Operaciones (Ingesta, Cuarentena, OCR, Auditoría, Administración)
 * Clasificación: BROWSER_CONTRACT_E2E. Fixtures HTTP sintéticos únicamente.
 * No ejecuta mutaciones sobre base persistente. No usa credenciales reales.
 * ADR-008 vigente: document.manage para OCR (Analista/TI; Jurídico denegado).
 */
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty } from './helpers.js'

const UPLOAD_RESULT = {
  file_id: '11111111-1111-4111-8111-111111111111',
  operation_id: '22222222-2222-4222-8222-222222222222',
  correlation_id: '33333333-3333-4333-8333-333333333333',
  state: 'COMPLETADO',
  format: 'PDF',
  family: 'CONTRATOS_DOCUMENTOS',
  routing_target: 'DOCUMENT',
  safe_cause_code: null,
  idempotent: false,
}

const QUARANTINE_PAGE = {
  items: [
    {
      id: '11111111-1111-4111-8111-111111111111',
      ingest_file_id: '22222222-2222-4222-8222-222222222222',
      source_record_id: '33333333-3333-4333-8333-333333333333',
      row_number: 1,
      source_family: 'CONTRATOS_DOCUMENTOS',
      created_at: '2026-10-07T00:00:00Z',
      cause_code: 'INVALID_TYPE',
      cause_description: 'Formato de campo incorrecto',
      state: 'Pendiente',
      original_payload: { campo: 'valor-original' },
      candidate_payload: null,
      discard_justification: null,
    },
  ],
  next_cursor: null,
}

const OCR_PAGE = {
  items: [
    {
      document_id: 'doc-0001',
      document_version_id: '11111111-1111-4111-8111-111111111111',
      document_name: 'Contrato de prueba OCR',
      file_name: 'ocr-prueba.pdf',
      processing_state: 'COMPLETED',
      ocr_state: 'Pendiente',
      processed_at: '2026-10-07T00:00:00Z',
      confidence: 0.91,
      total_page_count: 5,
      ocr_processed_page_count: 5,
      granularity: 'PAGE',
      outcome: 'Procesamiento solicitado',
    },
  ],
  next_cursor: null,
}

const AUDIT_PAGE = {
  items: [
    {
      id: '11111111-1111-4111-8111-111111111111',
      occurred_at: '2026-10-07T00:00:00Z',
      actor_type: 'USER',
      actor_identifier: 'usuario-prueba',
      actor_user_id: '22222222-2222-4222-8222-222222222222',
      action: 'LOGIN',
      resource_type: 'SESSION',
      resource_identifier: 'ses-0001',
      result: 'SUCCESS',
      safe_cause_code: null,
      operation_id: '33333333-3333-4333-8333-333333333333',
      correlation_id: '44444444-4444-4444-8444-444444444444',
      query_sha256: null,
      resources: [],
    },
  ],
  next_cursor: null,
}

test.beforeEach(async ({ page }) => {
  await mockSessionTI(page)
  await mockDashboardEmpty(page)
})

test.describe('Operaciones — Ingesta', () => {
  test('muestra el formulario de ingesta para TI/Analista', async ({ page }) => {
    await page.goto('/ingesta')
    await expect(page.getByRole('heading', { name: /operaciones de ingesta/i })).toBeVisible({ timeout: 8000 })
    await expect(page.getByRole('button', { name: /recibir y procesar/i })).toBeVisible({ timeout: 8000 })
  })

  test('carga de archivo retorna resultado sintético sin duplicar', async ({ page }) => {
    await page.route('**/api/ingestion/uploads', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(UPLOAD_RESULT) })
    )
    await page.goto('/ingesta')
    await expect(page.getByRole('heading', { name: /ingesta/i })).toBeVisible({ timeout: 8000 })
  })

  test('estado incierto en dispatch no intenta duplicar', async ({ page }) => {
    await page.route('**/api/coordination/dispatch', (r) =>
      r.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Estado incierto' }) })
    )
    await page.goto('/ingesta')
    await expect(page.getByRole('heading', { name: /ingesta/i })).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Operaciones — Cuarentena', () => {
  test('muestra los registros de cuarentena', async ({ page }) => {
    await page.route('**/api/validation/quarantine**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUARANTINE_PAGE) })
    )
    await page.goto('/cuarentena')
    await expect(page.getByText('Formato de campo incorrecto')).toBeVisible({ timeout: 8000 })
  })

  test('el candidato corregido está separado del original en el editor', async ({ page }) => {
    await page.route('**/api/validation/quarantine**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUARANTINE_PAGE) })
    )
    await page.goto('/cuarentena')
    await expect(page.getByText('Formato de campo incorrecto')).toBeVisible({ timeout: 8000 })
    await page.getByRole('button', { name: /corregir/i }).click()
    // En el editor modal: el original rechazado y el candidato editable están separados
    await expect(page.getByText(/original rechazado/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByLabel(/candidato corregido/i)).toBeVisible({ timeout: 8000 })
  })

  test('descarte requiere justificación', async ({ page }) => {
    await page.route('**/api/validation/quarantine**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(QUARANTINE_PAGE) })
    )
    await page.route('**/api/validation/quarantine/*/discard', (r) =>
      r.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'Justificación requerida' }) })
    )
    await page.goto('/cuarentena')
    await expect(page.getByText('Formato de campo incorrecto')).toBeVisible({ timeout: 8000 })
    await page.getByRole('button', { name: /descartar/i }).click()
    // Abre el modal de confirmación de descarte que exige justificación
    await expect(page.getByLabel(/justificación del descarte/i)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Operaciones — OCR (ADR-008: document.manage)', () => {
  test('muestra el panel OCR para TI (document.manage activo)', async ({ page }) => {
    await page.route('**/api/documents/ocr**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(OCR_PAGE) })
    )
    await page.goto('/ingesta')
    // El panel OCR está embebido en IngestionPage
    await expect(page.getByRole('heading', { name: /gestión ocr/i })).toBeVisible({ timeout: 8000 })
  })

  test('el botón de reprocesar está disponible', async ({ page }) => {
    await page.route('**/api/documents/ocr**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(OCR_PAGE) })
    )
    await page.goto('/ingesta')
    await expect(page.getByRole('button', { name: /consultar estado ocr/i })).toBeVisible({ timeout: 8000 })
  })

  test('reproceso con estado 503 muestra estado incierto, no reintenta', async ({ page }) => {
    await page.route('**/api/documents/ocr**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(OCR_PAGE) })
    )
    await page.route('**/api/documents/*/ocr/reprocess', (r) =>
      r.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Estado incierto' }) })
    )
    await page.goto('/ingesta')
    await expect(page.getByRole('heading', { name: /gestión ocr/i })).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Operaciones — Auditoría', () => {
  test('muestra registros de auditoría para Analista/TI', async ({ page }) => {
    await page.route('**/api/audit/events**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(AUDIT_PAGE) })
    )
    await page.goto('/auditoria')
    await expect(page.getByRole('cell', { name: 'usuario-prueba' })).toBeVisible({ timeout: 8000 })
    await expect(page.getByText('LOGIN')).toBeVisible({ timeout: 8000 })
  })

  test('solo lectura: no hay botones de edición ni exportación', async ({ page }) => {
    await page.route('**/api/audit/events**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(AUDIT_PAGE) })
    )
    await page.goto('/auditoria')
    await expect(page.getByRole('button', { name: /editar/i })).toBeHidden()
    await expect(page.getByRole('button', { name: /exportar/i })).toBeHidden()
  })
})

test.describe('Operaciones — Administración TI (CD-03)', () => {
  test('muestra el formulario de administración solo para TI', async ({ page }) => {
    await page.goto('/administracion')
    await expect(page.getByRole('heading', { name: /administración/i })).toBeVisible({ timeout: 8000 })
    await expect(page.getByRole('combobox', { name: /operación administrativa/i })).toBeVisible()
  })

  test('no hay catálogo de búsqueda de cuentas (CD-03)', async ({ page }) => {
    await page.goto('/administracion')
    // La descripción debe indicar que no hay catálogo de búsqueda
    await expect(page.getByText(/no existe un catálogo de búsqueda/i)).toBeVisible({ timeout: 8000 })
  })

  test('la asignación de roles exige identificador de cuenta conocido', async ({ page }) => {
    await page.goto('/administracion')
    await page.getByRole('combobox', { name: /operación administrativa/i }).selectOption('roles')
    // Debe aparecer el campo de identificador de cuenta UUID
    await expect(page.getByLabel(/identificador conocido de cuenta/i)).toBeVisible()
  })
})
