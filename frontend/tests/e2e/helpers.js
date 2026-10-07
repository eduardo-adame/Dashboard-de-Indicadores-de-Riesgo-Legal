/**
 * Utilidades compartidas para los specs de interfaz/contrato E2E.
 * Todos los helpers utilizan fixtures HTTP sintéticos.
 * No se persisten credenciales, tokens ni cookies reales.
 * Clasificación: BROWSER_CONTRACT_E2E
 */

/** Respuesta de login sintética sin credenciales reales. */
export const SYNTHETIC_TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.SYNTHETIC'

/** Principal sintético con rol TI para pruebas de interfaz completa. */
export const PRINCIPAL_TI = {
  id: '00000000-0000-0000-0000-000000000001',
  username: 'usuario-prueba',
  display_name: 'Usuario de Prueba',
  roles: ['TI'],
  permissions: [
    'dashboard.read', 'kpi.read', 'document.query',
    'ingest.upload', 'ingest.execute', 'quarantine.read',
    'quarantine.reinject', 'quarantine.discard', 'audit.read.own',
    'document.manage', 'audit.read.all', 'user.create', 'user.update',
    'user.disable', 'user.reset_access', 'role.assign',
    'document_acl.manage', 'technical_config.manage',
  ],
}

/** Principal sintético con rol Jurídico para pruebas de acceso restringido. */
export const PRINCIPAL_JURIDICO = {
  id: '00000000-0000-0000-0000-000000000002',
  username: 'juridico-prueba',
  display_name: 'Jurídico de Prueba',
  roles: ['JURIDICO'],
  permissions: ['dashboard.read', 'kpi.read', 'document.query'],
}

/** Respuesta /api/auth/me sintética para el perfil TI. */
export const ME_RESPONSE_TI = {
  id: PRINCIPAL_TI.id,
  username: PRINCIPAL_TI.username,
  display_name: PRINCIPAL_TI.display_name,
  active: true,
  roles: PRINCIPAL_TI.roles,
  permissions: PRINCIPAL_TI.permissions,
  authorization_version: 1,
}

/** Respuesta /api/auth/me sintética para Jurídico. */
export const ME_RESPONSE_JURIDICO = {
  id: PRINCIPAL_JURIDICO.id,
  username: PRINCIPAL_JURIDICO.username,
  display_name: PRINCIPAL_JURIDICO.display_name,
  active: true,
  roles: PRINCIPAL_JURIDICO.roles,
  permissions: PRINCIPAL_JURIDICO.permissions,
  authorization_version: 1,
}

/**
 * Intercepta las rutas de autenticación para simular una sesión TI
 * sin credenciales reales. No persiste storageState.
 */
export async function mockSessionTI(page) {
  await page.route('**/api/auth/login', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ access_token: SYNTHETIC_TOKEN, token_type: 'bearer' }),
      headers: { 'Set-Cookie': 'refresh_token=synthetic-refresh; HttpOnly; Path=/api/auth/refresh; SameSite=Strict' },
    })
  )
  await mockRefresh(page)
  await mockMeTI(page)
  await mockRevalidate(page)
}

/** Intercepta /api/auth/me para devolver el principal TI sintético. */
export async function mockMeTI(page) {
  await page.route('**/api/auth/me', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ME_RESPONSE_TI) })
  )
}

/** Intercepta /api/auth/me para devolver el principal Jurídico sintético. */
export async function mockMeJuridico(page) {
  await page.route('**/api/auth/me', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ME_RESPONSE_JURIDICO) })
  )
}

/** Configura sesión con rol Jurídico sintético. */
export async function mockSessionJuridico(page) {
  await page.route('**/api/auth/login', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ access_token: SYNTHETIC_TOKEN, token_type: 'bearer' }),
      headers: { 'Set-Cookie': 'refresh_token=synthetic-refresh; HttpOnly; Path=/api/auth/refresh; SameSite=Strict' },
    })
  )
  await mockRefresh(page)
  await mockMeJuridico(page)
}

/** Intercepta la revalidación transaccional devolviendo el principal TI vigente. */
export async function mockRevalidate(page) {
  await page.route('**/api/auth/revalidate', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(ME_RESPONSE_TI) })
  )
}

/** Intercepta logout correctamente sin exponer datos. */
export async function mockLogout(page) {
  await page.route('**/api/auth/logout', (route) =>
    route.fulfill({ status: 204 })
  )
}

/** Intercepta refresh de sesión sintético. */
export async function mockRefresh(page) {
  await page.route('**/api/auth/refresh', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ access_token: SYNTHETIC_TOKEN, token_type: 'bearer' }),
    })
  )
}

/** Respuesta analítica vacía válida (sin KPI) para estado empty. */
export const EMPTY_KPI_RESPONSE = {
  period_start: '2026-01-01',
  period_end: '2026-06-30',
  period_reference: '2026-06-01',
  filters: { period: 'last_6_months', period_start: null, period_end: null, risk_type: 'all', entity: null },
  items: [],
}

/** Respuesta analítica de analysis vacía. */
export const EMPTY_ANALYSIS_RESPONSE = {
  current_analytic_run_id: null,
  alert_count: 0,
  items: [],
}

/** KPI técnico sintético mínimo conforme contrato para KPI-CD-03. */
export const SYNTHETIC_TECHNICAL_KPI = {
  kpi_code: 'KPI-CD-03',
  availability: 'DISPONIBLE',
  value: '95.5',
  calculated_at: '2026-10-07T00:00:00Z',
}

/** Intercepta dashboard devolviendo respuestas analíticas vacías sintéticas. */
export async function mockDashboardEmpty(page) {
  await page.route('**/api/dashboard/kpis**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_KPI_RESPONSE) })
  )
  await page.route('**/api/dashboard/analysis**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) })
  )
  await page.route('**/api/technical/kpis/**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SYNTHETIC_TECHNICAL_KPI) })
  )
}

/** KPI sintético mínimo conforme contrato para KPI-RC-03. */
export const SYNTHETIC_KPI_ITEM = {
  id: 'obs-001',
  kpi_code: 'KPI-RC-03',
  name: 'Contratos próximos a vencimiento sin revisión',
  classification: 'MVP-NÚCLEO',
  unit: 'contratos',
  dimensions: {},
  value: '5',
  availability: 'DISPONIBLE',
  period_start: '2026-01-01',
  period_end: '2026-01-31',
  as_of_date: '2026-01-31',
  calculated_at: '2026-02-01T00:00:00Z',
  entity_filter_applicable: false,
}

/** Respuesta analítica de KPI mínima y sintética. */
export const SYNTHETIC_KPI_RESPONSE = {
  ...EMPTY_KPI_RESPONSE,
  items: [SYNTHETIC_KPI_ITEM],
}

const RAG_FRAG_ID = '33333333-3333-4333-8333-333333333333'
const RAG_BASE_METADATA = {
  fragment_id: RAG_FRAG_ID,
  document_id: 'DOC-001',
  document_name: 'Contrato de prueba A',
  document_type: 'Contrato',
  document_date: '2025-06-01',
  page_start: 1,
  page_end: 2,
  section: 'Cláusula 3',
  clause: 'Plazos',
}

/** Respuesta RAG de Estado 1 — Evidencia suficiente. */
export const RAG_EVIDENCIA_SUFICIENTE = {
  operation_id: '11111111-1111-4111-8111-111111111111',
  correlation_id: '22222222-2222-4222-8222-222222222222',
  state: 'EVIDENCIA_SUFICIENTE',
  operation_status: 'COMPLETED',
  generation_status: 'SUCCEEDED',
  generated_response: 'Respuesta generada sintética [E1]. No constituye asesoría jurídica.',
  safe_result_message: 'Se encontraron fragmentos autorizados que responden a la consulta.',
  context_reference_id: null,
  fragments: [{ ...RAG_BASE_METADATA, fragment_text: 'Cláusula de prueba sintética. Contenido no real.', usage: 'EVIDENCE' }],
  citations: [{ handle: 'E1', ...RAG_BASE_METADATA }],
}

/** Respuesta RAG de Estado 2 — Evidencia insuficiente. */
export const RAG_EVIDENCIA_INSUFICIENTE = {
  operation_id: '11111111-1111-4111-8111-111111111111',
  correlation_id: '22222222-2222-4222-8222-222222222222',
  state: 'EVIDENCIA_INSUFICIENTE',
  operation_status: 'COMPLETED',
  generation_status: 'NOT_REQUESTED',
  generated_response: null,
  safe_result_message: 'Los fragmentos encontrados no respaldan con suficiencia la consulta.',
  context_reference_id: null,
  fragments: [],
  citations: [],
}

/** Respuesta RAG de Estado 3 — Sin evidencia. */
export const RAG_SIN_EVIDENCIA = {
  operation_id: '11111111-1111-4111-8111-111111111111',
  correlation_id: '22222222-2222-4222-8222-222222222222',
  state: 'SIN_EVIDENCIA',
  operation_status: 'COMPLETED',
  generation_status: 'NOT_REQUESTED',
  generated_response: null,
  safe_result_message: 'No se encontraron fragmentos relacionados con la consulta en los documentos autorizados.',
  context_reference_id: null,
  fragments: [],
  citations: [],
}

/** Respuesta RAG de Estado 4 — Sin autorización. */
export const RAG_SIN_AUTORIZACION = {
  operation_id: '11111111-1111-4111-8111-111111111111',
  correlation_id: '22222222-2222-4222-8222-222222222222',
  state: 'SIN_AUTORIZACION',
  operation_status: 'COMPLETED',
  generation_status: 'NOT_REQUESTED',
  generated_response: null,
  safe_result_message: null,
  context_reference_id: null,
  fragments: [],
  citations: [],
}

/** Respuesta RAG con fallo técnico (DTO válido, estado de error de proveedor). */
export const RAG_FALLO_TECNICO = {
  operation_id: '11111111-1111-4111-8111-111111111111',
  correlation_id: '22222222-2222-4222-8222-222222222222',
  state: 'EVIDENCIA_INSUFICIENTE',
  operation_status: 'FAILED',
  generation_status: 'FAILED',
  generated_response: null,
  safe_result_message: 'El proveedor de generación no respondió.',
  context_reference_id: null,
  fragments: [],
  citations: [],
}

/** Página sintética conforme de OCR. */
export const SYNTHETIC_OCR_PAGE = {
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

/** Página sintética conforme de Auditoría. */
export const SYNTHETIC_AUDIT_PAGE = {
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
