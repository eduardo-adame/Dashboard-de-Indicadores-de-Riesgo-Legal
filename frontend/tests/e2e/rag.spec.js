/**
 * Spec: Consulta RAG — pruebas de interfaz/contrato con fixtures HTTP sintéticos.
 * Clasificación: BROWSER_CONTRACT_E2E.
 * No llama a Groq real. No persiste resultados. Los cuatro estados probados.
 */
import { test, expect } from '@playwright/test'
import {
  mockSessionTI, mockDashboardEmpty,
  RAG_EVIDENCIA_SUFICIENTE, RAG_EVIDENCIA_INSUFICIENTE,
  RAG_SIN_EVIDENCIA, RAG_SIN_AUTORIZACION, RAG_FALLO_TECNICO,
} from './helpers.js'

async function mockRag(page, response) {
  await page.route('**/api/rag/query', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) })
  )
}

test.beforeEach(async ({ page }) => {
  await mockSessionTI(page)
  await mockDashboardEmpty(page)
})

test.describe('RAG — Estado 1: Evidencia suficiente', () => {
  test('muestra respuesta generada y fragmentos autorizados', async ({ page }) => {
    await mockRag(page, RAG_EVIDENCIA_SUFICIENTE)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('¿Cuál es el plazo de revisión contractual?')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia suficiente/i)).toBeVisible({ timeout: 10000 })
    await expect(page.getByText(/respuesta generada sintética/i)).toBeVisible()
    await expect(page.getByRole('article').first()).toBeVisible()
  })
})

test.describe('RAG — Estado 2: Evidencia insuficiente', () => {
  test('muestra banner de evidencia insuficiente sin respuesta generada', async ({ page }) => {
    await mockRag(page, RAG_EVIDENCIA_INSUFICIENTE)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Consulta sin suficiente respaldo')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia insuficiente/i)).toBeVisible({ timeout: 10000 })
    // No debe aparecer sección de respuesta generada
    await expect(page.getByRole('heading', { name: /respuesta generada/i })).toBeHidden()
  })
})

test.describe('RAG — Estado 3: Sin evidencia', () => {
  test('muestra estado sin evidencia', async ({ page }) => {
    await mockRag(page, RAG_SIN_EVIDENCIA)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Consulta sin fragmentos')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/sin evidencia/i)).toBeVisible({ timeout: 10000 })
  })
})

test.describe('RAG — Estado 4: Sin autorización', () => {
  test('muestra denegación sin leakage de datos protegidos', async ({ page }) => {
    await mockRag(page, RAG_SIN_AUTORIZACION)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Consulta que carece de permiso')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText('Sin autorización', { exact: true })).toBeVisible({ timeout: 10000 })
    // No debe haber fragmentos visibles
    await expect(page.getByRole('heading', { name: /fragmentos autorizados/i })).toBeHidden()
  })
})

test.describe('RAG — Fallo técnico / proveedor', () => {
  test('muestra error técnico con DTO válido y opción de reintento', async ({ page }) => {
    await mockRag(page, RAG_FALLO_TECNICO)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Consulta con fallo de proveedor')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia insuficiente/i)).toBeVisible({ timeout: 10000 })
    // Debe ofrecer reintento con la misma operación
    await expect(page.getByRole('button', { name: /reintentar/i })).toBeVisible({ timeout: 5000 })
  })
})

test.describe('RAG — Citas y metadata nullable', () => {
  test('muestra cita con fecha de documento null como No disponible', async ({ page }) => {
    const frag = { ...RAG_EVIDENCIA_SUFICIENTE.fragments[0], document_date: null }
    const cite = { ...RAG_EVIDENCIA_SUFICIENTE.citations[0], document_date: null }
    const ragConFechaNull = {
      ...RAG_EVIDENCIA_SUFICIENTE,
      fragments: [frag],
      citations: [cite],
    }
    await mockRag(page, ragConFechaNull)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Cita con fecha nula')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia suficiente/i)).toBeVisible({ timeout: 10000 })
    // En la cita del fragmento, fecha debe ser "No disponible"
    await expect(page.getByText(/no disponible/i)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('RAG — Visor documental', () => {
  test('el visor sin fragmento activo muestra estado vacío', async ({ page }) => {
    await page.goto('/documentos')
    await expect(page.getByText(/no hay un fragmento disponible/i)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('RAG — Ausencia de persistencia', () => {
  test('limpiar consulta elimina el resultado de la vista', async ({ page }) => {
    await mockRag(page, RAG_EVIDENCIA_SUFICIENTE)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Consulta que se limpiará')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia suficiente/i)).toBeVisible({ timeout: 10000 })
    await page.getByRole('button', { name: /limpiar consulta/i }).click()
    // El resultado no debe persistir
    await expect(page.getByText(/evidencia suficiente/i)).toBeHidden()
  })
})

test.describe('RAG — Contenido potencialmente malicioso', () => {
  test('el contenido de fragmento se renderiza como texto, no como HTML ejecutable', async ({ page }) => {
    const xssPayload = '<img src=x onerror=window.__xss_hit=1> texto sintético'
    const ragXss = {
      ...RAG_EVIDENCIA_SUFICIENTE,
      fragments: [{ ...RAG_EVIDENCIA_SUFICIENTE.fragments[0], fragment_text: xssPayload }],
    }
    await mockRag(page, ragXss)
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Prueba de contenido')
    await page.getByRole('button', { name: /consultar/i }).click()
    await expect(page.getByText(/evidencia suficiente/i)).toBeVisible({ timeout: 10000 })
    // Verificar que el payload no ejecutó el evento XSS
    const xssHit = await page.evaluate(() => window.__xss_hit)
    expect(xssHit).toBeUndefined()
  })
})

test.describe('RAG — Responsive', () => {
  test('el área de texto es visible y usable en viewport estrecho', async ({ page }) => {
    await page.goto('/consulta')
    const textarea = page.getByRole('textbox', { name: /consulta/i })
    await expect(textarea).toBeVisible()
    const box = await textarea.boundingBox()
    expect(box?.width).toBeGreaterThan(200)
  })
})
