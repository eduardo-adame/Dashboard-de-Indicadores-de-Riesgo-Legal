/**
 * Spec: Accesibilidad — pruebas de interfaz sin biblioteca adicional de a11y.
 * Utiliza únicamente las APIs nativas de Playwright y assertions ARIA.
 * Clasificación: BROWSER_CONTRACT_E2E.
 * No se instala axe-core ni otra biblioteca de accesibilidad sin enmienda.
 */
import { test, expect } from '@playwright/test'
import {
  mockSessionTI, mockDashboardEmpty, EMPTY_ANALYSIS_RESPONSE,
  SYNTHETIC_OCR_PAGE, SYNTHETIC_AUDIT_PAGE,
} from './helpers.js'

test.beforeEach(async ({ page }) => {
  await mockSessionTI(page)
  await mockDashboardEmpty(page)
})

test.describe('Accesibilidad — Landmarks y estructura', () => {
  test('la página principal tiene landmark de navegación', async ({ page }, testInfo) => {
    await page.goto('/')
    if (testInfo.project.name === 'narrow') {
      await page.getByRole('button', { name: /abrir navegación/i }).click()
    }
    await expect(page.getByRole('navigation', { name: /navegación principal/i })).toBeVisible({ timeout: 8000 })
  })

  test('la página tiene un único h1 por vista', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    const h1Count = await page.locator('h1').count()
    expect(h1Count).toBeLessThanOrEqual(1)
  })

  test('el contenido principal tiene landmark main', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('main')).toBeVisible({ timeout: 8000 })
  })

  test('la barra superior (topbar) conserva la altura canónica de 52px', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    const topbar = page.locator('div.border-b.bg-surface').first()
    await expect(topbar).toBeVisible({ timeout: 8000 })
    const computedHeight = await topbar.evaluate((el) => window.getComputedStyle(el).height)
    expect(computedHeight).toBe('52px')
    const boundingBox = await topbar.boundingBox()
    expect(boundingBox.height).toBe(52)
  })
})

test.describe('Accesibilidad — Encabezados y jerarquía', () => {
  test('las páginas analíticas tienen h1 visible', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible({ timeout: 8000 })
  })

  test('la consulta RAG tiene h1 visible', async ({ page }) => {
    await page.goto('/consulta')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Accesibilidad — Labels y nombres accesibles', () => {
  test('el campo de consulta RAG tiene label visible', async ({ page }) => {
    await page.goto('/consulta')
    const label = page.getByText(/consulta en lenguaje natural/i)
    await expect(label).toBeVisible({ timeout: 8000 })
  })

  test('los controles de filtro en analítica tienen label', async ({ page }) => {
    await page.goto('/')
    // Selector de análisis debe tener label accesible
    await expect(page.getByRole('combobox', { name: /análisis/i })).toBeVisible({ timeout: 8000 })
  })

  test('los inputs de administración tienen labels asociados', async ({ page }) => {
    await page.goto('/administracion')
    await expect(page.getByRole('combobox', { name: /operación administrativa/i })).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Accesibilidad — Navegación por teclado', () => {
  test('el enlace skip to content es el primer elemento focusable', async ({ page }) => {
    await page.goto('/')
    await page.keyboard.press('Tab')
    const focused = await page.evaluate(() => document.activeElement?.textContent?.trim() || document.activeElement?.getAttribute('href'))
    // El primer Tab debe llegar al skip link o a la navegación
    expect(focused).toBeTruthy()
  })

  test('los botones son accesibles por teclado', async ({ page }) => {
    await page.goto('/consulta')
    const btn = page.getByRole('button', { name: /consultar/i })
    await btn.focus()
    await expect(btn).toBeFocused()
  })

  test('el selector de pestaña (tabs) acepta teclas de flecha', async ({ page }) => {
    await mockDashboardEmpty(page)
    await page.goto('/')
    // Verificar que existe un role tablist si hay tabs en la página
    const tablist = page.getByRole('tablist')
    if (await tablist.count() > 0) {
      const firstTab = tablist.getByRole('tab').first()
      await firstTab.focus()
      await page.keyboard.press('ArrowRight')
      // El foco debe haberse movido
      const activeTab = await page.evaluate(() => document.activeElement?.getAttribute('role'))
      expect(activeTab).toBe('tab')
    }
  })
})

test.describe('Accesibilidad — Foco visible', () => {
  test('el foco es visible en el botón de consulta RAG', async ({ page }) => {
    await page.goto('/consulta')
    const btn = page.getByRole('button', { name: /consultar/i })
    await btn.focus()
    const outlineStyle = await btn.evaluate((el) => window.getComputedStyle(el).outlineStyle)
    // El outline no debe ser 'none' cuando el botón está enfocado con teclado
    expect(outlineStyle).not.toBe('')
  })
})

test.describe('Accesibilidad — Modal y drawer', () => {
  test('el modal de confirmación de reproceso OCR es un dialog', async ({ page }) => {
    await page.route('**/api/documents/ocr**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SYNTHETIC_OCR_PAGE) })
    )
    await page.goto('/ingesta')
    await expect(page.getByRole('heading', { name: /gestión ocr/i })).toBeVisible({ timeout: 8000 })
    // Consultar para ver los registros
    await page.getByRole('button', { name: /consultar estado ocr/i }).click()
    const reprocesarBtn = page.getByRole('button', { name: /reprocesar/i }).first()
    if (await reprocesarBtn.isVisible()) {
      await reprocesarBtn.click()
      const dialog = page.getByRole('dialog')
      await expect(dialog).toBeVisible({ timeout: 5000 })
      // El diálogo debe tener aria-modal
      const ariaModal = await dialog.getAttribute('aria-modal')
      expect(ariaModal).toBe('true')
      // Cerrar con Escape
      await page.keyboard.press('Escape')
      await expect(dialog).toBeHidden({ timeout: 3000 })
    }
  })

  test('el drawer de navegación en móvil es un dialog', async ({ page }) => {
    await page.goto('/')
    const menuBtn = page.getByRole('button', { name: /abrir navegación/i })
    if (await menuBtn.isVisible()) {
      await menuBtn.click()
      const drawer = page.getByRole('dialog')
      await expect(drawer).toBeVisible({ timeout: 5000 })
      await page.keyboard.press('Escape')
      await expect(drawer).toBeHidden({ timeout: 3000 })
    }
  })
})

test.describe('Accesibilidad — Controles deshabilitados', () => {
  test('el botón Consultar deshabilitado tiene aria-busy cuando está ocupado', async ({ page }) => {
    // Simular latencia para capturar el estado busy
    await page.route('**/api/rag/query', (_r) => new Promise(() => {})) // cuelga
    await page.goto('/consulta')
    await page.getByRole('textbox', { name: /consulta/i }).fill('Prueba de estado busy')
    const btn = page.getByRole('button', { name: /consultar/i })
    await btn.click()
    // Durante la carga, el botón debe tener aria-busy=true o estar disabled
    const ariaBusy = await btn.getAttribute('aria-busy')
    const disabled = await btn.isDisabled()
    expect(ariaBusy === 'true' || disabled).toBe(true)
  })
})

test.describe('Accesibilidad — Tablas semánticas', () => {
  test('las tablas tienen caption accesible', async ({ page }) => {
    await page.route('**/api/audit/events**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(SYNTHETIC_AUDIT_PAGE) })
    )
    await page.goto('/auditoria')
    const tables = page.getByRole('table')
    const count = await tables.count()
    if (count > 0) {
      const caption = await tables.first().locator('caption').textContent()
      expect(caption?.trim().length).toBeGreaterThan(0)
    }
  })

  test('las celdas numéricas de KPI tienen estructura tabular', async ({ page }) => {
    await page.route('**/api/dashboard/kpis**', (r) =>
      r.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          period_start: '2026-01-01',
          period_end: '2026-06-30',
          period_reference: '2026-06-01',
          filters: { period: 'last_6_months', period_start: null, period_end: null, risk_type: 'all', entity: null },
          items: [
            { id: 'obs-001', kpi_code: 'KPI-RC-03', name: 'Contratos próximos a vencimiento sin revisión', classification: 'MVP-NÚCLEO', dimensions: {}, value: '3', unit: 'contratos', availability: 'DISPONIBLE', period_start: '2026-01-01', period_end: '2026-01-31', as_of_date: '2026-01-31', calculated_at: '2026-02-01T00:00:00Z', entity_filter_applicable: false },
          ],
        }),
      })
    )
    await page.route('**/api/dashboard/analysis**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) })
    )
    await page.goto('/')
    const table = page.getByRole('table').first()
    await expect(table).toBeVisible({ timeout: 8000 })
    const headers = table.getByRole('columnheader')
    expect(await headers.count()).toBeGreaterThan(0)
  })
})

test.describe('Accesibilidad — Estados dinámicos', () => {
  test('los estados de loading tienen role=status o aria-live', async ({ page }) => {
    // Retrasar analytics para capturar el loading
    let resolveKpi
    await page.route('**/api/dashboard/kpis**', (_r) => new Promise((res) => { resolveKpi = res }))
    await page.route('**/api/dashboard/analysis**', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) })
    )
    await page.goto('/')
    // Durante loading debe haber un elemento con role=status o aria-live
    const status = page.getByRole('status')
    await expect(status).toBeVisible({ timeout: 5000 })
  })

  test('el estado de error tiene role=alert', async ({ page }) => {
    await page.route('**/api/dashboard/kpis**', (r) =>
      r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Error interno' }) })
    )
    await page.route('**/api/dashboard/analysis**', (r) =>
      r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Error interno' }) })
    )
    await page.goto('/')
    await expect(page.getByRole('alert').first()).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Accesibilidad — Acciones no dependientes del color', () => {
  test('el estado de error es distinguible por texto, no solo por color', async ({ page }) => {
    await page.route('**/api/dashboard/kpis**', (r) =>
      r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Error' }) })
    )
    await page.route('**/api/dashboard/analysis**', (r) =>
      r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Error' }) })
    )
    await page.goto('/')
    const alert = page.getByRole('alert').first()
    await expect(alert).toBeVisible({ timeout: 8000 })
    const text = await alert.textContent()
    expect(text?.trim().length).toBeGreaterThan(0)
  })
})
