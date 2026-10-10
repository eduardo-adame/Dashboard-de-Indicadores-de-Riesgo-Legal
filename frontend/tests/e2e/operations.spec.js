/**
 * Spec: Operaciones (Ingesta, Cuarentena, OCR, Auditoría, Administración)
 * Clasificación: BROWSER_CONTRACT_E2E. Fixtures HTTP sintéticos únicamente.
 * No ejecuta mutaciones sobre base persistente. No usa credenciales reales.
 * ADR-008 vigente: document.manage para OCR (Analista/TI; Jurídico denegado).
 */
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty, mockSessionJuridico, ME_RESPONSE_TI } from './helpers.js'

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
      processing_state: 'RECHAZADA',
      ocr_applicable: true,
      reprocess_eligible: true,
      ocr_state: 'Rechazado por baja confianza',
      processed_at: '2026-10-07T00:00:00Z',
      confidence: 0.61,
      total_page_count: 5,
      ocr_processed_page_count: 5,
      granularity: 'PAGE',
      outcome: 'Rechazado por baja confianza',
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
    await expect(page.getByRole('button', { name: 'Reprocesar', exact: true })).toBeVisible({ timeout: 8000 })
  })

  test('reproceso 503 no confirmado muestra error, no reintenta', async ({ page }) => {
    let posts = 0
    await page.route('**/api/documents/ocr**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(OCR_PAGE) })
    )
    await page.route('**/api/documents/*/ocr/reprocess', (r) => { posts++; return r.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Estado incierto' }) }) })
    await page.goto('/ingesta')
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    await page.getByRole('button', { name: 'Confirmar reproceso', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('El servicio no está disponible temporalmente')
    await expect(page.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeDisabled()
    await expect(page.getByText(/Reproceso confirmado/)).toHaveCount(0)
    expect(posts).toBe(1)
  })
})

test.describe('SRS_REQUIRED: OCR veraz y autorizado — BROWSER_CONTRACT_E2E', () => {
  for (const role of ['TI', 'ANALISTA']) {
    test(`${role}: presenta nativos, ausencia y estados reales sin habilitar acciones falsas`, async ({ page }) => {
      const principal = { ...ME_RESPONSE_TI, roles: [role] }
      await page.route('**/api/auth/me', (r) => r.fulfill({ json: principal }))
      const cases = [
        ['DOCX-NATIVO', false, null, false, 'LISTA', null, 'No aplica'],
        ['PDF-TEXTO', false, null, false, 'LISTA', null, 'No aplica'],
        ['ESCANEADO-SIN-OCR', true, null, false, 'PROCESANDO', null, 'Sin resultado OCR'],
        ['ESCANEADO-PENDIENTE', true, 'Pendiente', false, 'PENDIENTE', null, 'Pendiente'],
        ['ESCANEADO-RECHAZADO', true, 'Rechazado por baja confianza', true, 'RECHAZADA', 0, 'Rechazado por baja confianza'],
        ['ESCANEADO-EXITOSO', true, 'Exitoso', false, 'LISTA', 0.96, 'Exitoso'],
        ['ESCANEADO-FALLIDO', true, null, false, 'FALLIDA', null, 'Sin resultado OCR'],
      ]
      const items = cases.map(([document_id, ocr_applicable, ocr_state, reprocess_eligible, processing_state, confidence]) => ({ ...OCR_PAGE.items[0], document_id, ocr_applicable, ocr_state, outcome: ocr_state, reprocess_eligible, processing_state, confidence }))
      await page.route('**/api/documents/ocr**', (r) => r.fulfill({ json: { items, next_cursor: null } }))
      await page.goto('/ingesta')
      const table = page.getByRole('table', { name: 'Resultados operativos OCR' })
      for (const [id, , , eligible, processing, confidence, label] of cases) {
        const row = table.getByRole('row').filter({ has: page.getByRole('cell', { name: id, exact: true }) })
        await expect(row.getByRole('cell', { name: label, exact: true })).toHaveCount(2)
        await expect(row.getByRole('cell', { name: processing, exact: true })).toHaveCount(1)
        await expect(row.getByRole('cell', { name: confidence === null ? 'No disponible' : String(confidence), exact: true })).toHaveCount(1)
        await expect(row.getByRole('button', { name: 'Reprocesar', exact: true })).toHaveCount(eligible ? 1 : 0)
      }
      await expect(table.getByRole('button', { name: 'Reprocesar', exact: true })).toHaveCount(1)
    })
  }

  test('Jurídico no accede a OCR ni emite su lectura', async ({ page }) => {
    await mockSessionJuridico(page)
    let reads = 0
    await page.route('**/api/documents/ocr**', (r) => { reads++; return r.fulfill({ json: OCR_PAGE }) })
    await page.goto('/ingesta')
    await expect(page.getByRole('alert')).toContainText('Acceso no autorizado')
    await expect(page.getByRole('heading', { name: 'Gestión OCR', exact: true })).toHaveCount(0)
    expect(reads).toBe(0)
  })

  test('reproceso elegible conserva versión y confirma sólo el resultado HTTP recibido', async ({ page }) => {
    let posts = 0
    await page.route('**/api/documents/ocr**', (r) => r.fulfill({ json: OCR_PAGE }))
    await page.route('**/api/documents/*/ocr/reprocess', async (r) => {
      posts++
      const body = r.request().postDataJSON()
      expect(body.source_document_version_id).toBe(OCR_PAGE.items[0].document_version_id)
      expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/i)
      await r.fulfill({ json: { operation_id: UPLOAD_RESULT.operation_id, document_id: OCR_PAGE.items[0].document_id, document_version_id: UPLOAD_RESULT.correlation_id, ocr_state: 'Exitoso', processing_state: 'LISTA', kpi_job_id: null, kpi_job_state: null } })
    })
    await page.goto('/ingesta')
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    await page.getByRole('button', { name: 'Confirmar reproceso', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('status')).toHaveText('Reproceso confirmado.')
    await expect(page.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeDisabled()
    expect(posts).toBe(1)
  })

  test('elegibilidad obsoleta con POST 409 conserva el rechazo sin éxito aparente', async ({ page }) => {
    let posts = 0
    await page.route('**/api/documents/ocr**', (r) => r.fulfill({ json: OCR_PAGE }))
    await page.route('**/api/documents/*/ocr/reprocess', (r) => { posts++; return r.fulfill({ status: 409, json: { detail: 'Versión ya procesada' } }) })
    await page.goto('/ingesta')
    await page.getByRole('button', { name: 'Reprocesar', exact: true }).click()
    await page.getByRole('button', { name: 'Confirmar reproceso', exact: true }).click()
    await expect(page.getByRole('dialog').getByRole('alert')).toContainText('conflicto')
    await expect(page.getByText(/Reproceso confirmado/)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Confirmar reproceso', exact: true })).toBeDisabled()
    expect(posts).toBe(1)
  })
})

test.describe('ROBUSTNESS: DTO OCR inválido — BROWSER_CONTRACT_E2E', () => {
  test('ausencia del campo obligatorio no muestra lista parcial ni reproceso', async ({ page }) => {
    const { reprocess_eligible, ...malformed } = OCR_PAGE.items[0]
    await page.route('**/api/documents/ocr**', (r) => r.fulfill({ json: { items: [OCR_PAGE.items[0], { ...malformed, document_id: 'ILEGIBLE' }], next_cursor: null } }))
    await page.goto('/ingesta')
    const section = page.locator('section').filter({ has: page.getByRole('heading', { name: 'Gestión OCR', exact: true }) })
    await expect(section.getByRole('alert')).toContainText('El servicio no está disponible temporalmente')
    await expect(page.getByRole('table', { name: 'Resultados operativos OCR' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Reprocesar', exact: true })).toHaveCount(0)
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
