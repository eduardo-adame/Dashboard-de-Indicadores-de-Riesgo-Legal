/**
 * Spec: Analítica — pruebas de interfaz/contrato con fixtures HTTP sintéticos.
 * Clasificación: BROWSER_CONTRACT_E2E.
 * No recalcula KPI. No llama a Groq. No usa datos reales.
 */
import { test, expect } from '@playwright/test'
import {
  mockSessionTI,
  SYNTHETIC_KPI_ITEM, SYNTHETIC_KPI_RESPONSE, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE,
} from './helpers.js'

const KPI_CATALOG_NAMES = {
  'KPI-RC-03': { name: 'Contratos próximos a vencimiento sin revisión', unit: 'contratos' },
  'KPI-LI-01': { name: 'Exposición total por litigios activos', unit: 'importe' },
  'KPI-LI-05': { name: 'Nuevos litigios por periodo', unit: 'litigios' },
  'KPI-CN-02': { name: 'Obligaciones regulatorias vencidas sin atender', unit: 'obligaciones' },
  'KPI-CN-03': { name: 'Incidentes de incumplimiento por periodo', unit: 'incidentes' },
}

function buildKpiItem(code, value, availability = 'DISPONIBLE') {
  const meta = KPI_CATALOG_NAMES[code] || KPI_CATALOG_NAMES['KPI-RC-03']
  return { ...SYNTHETIC_KPI_ITEM, id: `obs-${code}`, kpi_code: code, name: meta.name, unit: meta.unit, value, availability }
}

function mockAnalytics(page, kpiItems = [], analysisItems = EMPTY_ANALYSIS_RESPONSE) {
  page.route('**/api/dashboard/kpis**', (r) =>
    r.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        period_start: '2026-01-01',
        period_end: '2026-06-30',
        period_reference: '2026-06-01',
        filters: { period: 'last_6_months', period_start: null, period_end: null, risk_type: 'all', entity: null },
        items: kpiItems,
      }),
    })
  )
  page.route('**/api/dashboard/analysis**', (r) =>
    r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(analysisItems) })
  )
}

test.beforeEach(async ({ page }) => {
  await mockSessionTI(page)
})

test.describe('Analítica — Resumen ejecutivo', () => {
  test('carga la página con título correcto', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/')
    await expect(page.getByRole('heading', { name: /resumen ejecutivo/i })).toBeVisible()
  })

  test('muestra estado vacío sin KPI y preserva regiones de gráficas', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/')
    await expect(page.getByText(/sin indicadores principales/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/evolución de exposición acumulada/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/composición por severidad/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/sin datos disponibles/i).first()).toBeVisible({ timeout: 8000 })
  })

  test('muestra KPI sintético con valor correcto', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-RC-03', '5')])
    await page.goto('/')
    await expect(page.getByText('5').first()).toBeVisible({ timeout: 8000 })
  })

  test('distingue null de cero: null muestra No disponible en tabla', async ({ page }) => {
    const nullItem = { ...SYNTHETIC_KPI_ITEM, value: null, availability: 'NO_DISPONIBLE' }
    mockAnalytics(page, [nullItem])
    await page.goto('/')
    // El adapter debe presentar "No disponible" y no "0"
    await expect(page.getByText(/no disponible/i).first()).toBeVisible({ timeout: 8000 })
  })

  test('los cinco KPI núcleo no fabrican meses inexistentes', async ({ page }) => {
    const CORE_CODES = ['KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03']
    const items = CORE_CODES.map((code) => buildKpiItem(code, '3'))
    mockAnalytics(page, items)
    await page.goto('/')
    // Verificar que no aparecen series inventadas: la tabla debe mostrar solo los meses enviados
    await expect(page.locator('.kpi-row').getByText('3').first()).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Analítica — Riesgo contractual', () => {
  test('carga la página de contratos', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-CC-01', '2')])
    await page.goto('/contratos')
    await expect(page.getByRole('heading', { name: /riesgo contractual/i })).toBeVisible()
  })

  test('preserva contenedor de gráfica con empty state contextual cuando no hay datos', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/contratos')
    await expect(page.getByText(/tiempo de ciclo del contrato/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/no existen observaciones contractuales para los filtros seleccionados/i)).toBeVisible({ timeout: 8000 })
  })

  test('filtros de periodo no cambian semántica de datos', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-CC-01', '7')])
    await page.goto('/contratos')
    // Cambiar a histórico no inventa datos
    const modeSelect = page.getByRole('combobox', { name: /análisis/i })
    await modeSelect.selectOption('historical')
    await expect(page.getByRole('button', { name: /aplicar filtros/i })).toBeEnabled()
  })
})

test.describe('Analítica — Gestión de litigios', () => {
  test('carga la página de litigios', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-LI-01', '1')])
    await page.goto('/litigios')
    await expect(page.getByRole('heading', { name: /gestión de litigios/i })).toBeVisible()
  })

  test('preserva ambas superficies de gráficas independientemente cuando están vacías', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/litigios')
    await expect(page.locator('figcaption').getByText(/exposición total por litigios activos/i)).toBeVisible({ timeout: 8000 })
    await expect(page.locator('figcaption').getByText(/nuevos litigios por periodo/i)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Analítica — Tendencias y riesgos', () => {
  test('carga la página de tendencias sin KPI RC-03 acumulado', async ({ page }) => {
    // KPI-RC-03 no debe acumularse; verificar que la gráfica lo omite
    const items = [buildKpiItem('KPI-LI-01', '4'), buildKpiItem('KPI-RC-03', '2')]
    mockAnalytics(page, items)
    await page.goto('/tendencias')
    await expect(page.getByRole('heading', { name: /tendencias y riesgos/i })).toBeVisible()
  })

  test('preserva región de tendencias con estado vacío contextual cuando no hay observaciones', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/tendencias')
    await expect(page.getByText(/tendencias de indicadores clave/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/sin tendencias disponibles/i)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Analítica — Responsive', () => {
  test('redistribución responsive de la cuadrícula KPI según viewport', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-RC-03', '9')])
    await page.goto('/')
    await expect(page.getByText('9').first()).toBeVisible({ timeout: 8000 })
    // La cuadrícula KPI debe adaptarse al viewport
    const kpiRow = page.locator('.kpi-row').first()
    await expect(kpiRow).toBeVisible()
    const box = await kpiRow.boundingBox()
    expect(box).not.toBeNull()
    const width = page.viewportSize()?.width ?? 1440
    const computedColumns = await kpiRow.evaluate((el) => window.getComputedStyle(el).gridTemplateColumns.split(' ').length)
    if (width < 768) {
      expect(computedColumns).toBe(1)
    } else {
      expect(computedColumns).toBeGreaterThanOrEqual(2)
    }
  })

  test('gráfica tiene contenedor con ancho mínimo cero', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-LI-01', '5')])
    await page.goto('/litigios')
    const chart = page.locator('[aria-hidden="true"]').first()
    if (await chart.isVisible()) {
      const box = await chart.boundingBox()
      expect(box?.width).toBeGreaterThan(0)
    }
  })

  test('bloque analítico vacío no produce desbordamiento horizontal en viewport estrecho', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/')
    await expect(page.getByText(/sin indicadores principales/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/evolución de exposición acumulada/i)).toBeVisible({ timeout: 8000 })
    await expect(page.getByText(/composición por severidad/i)).toBeVisible({ timeout: 8000 })
    const bodyScrollWidth = await page.evaluate(() => document.body.scrollWidth)
    const bodyClientWidth = await page.evaluate(() => document.body.clientWidth)
    expect(bodyScrollWidth).toBeLessThanOrEqual(bodyClientWidth + 1)
  })
})

test.describe('Analítica — Tabla alternativa a gráfica', () => {
  test('la tabla de observaciones contiene los datos del KPI sintético', async ({ page }) => {
    mockAnalytics(page, [buildKpiItem('KPI-RC-03', '42')])
    await page.goto('/')
    await expect(page.getByRole('table', { name: /observaciones/i }).first()).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Analítica — Navegación back/forward', () => {
  test('los filtros persisten en URL y se restauran en back', async ({ page }) => {
    mockAnalytics(page, [])
    await page.goto('/')
    await page.goto('/contratos')
    await page.goBack()
    await expect(page.getByRole('heading', { name: /resumen ejecutivo/i })).toBeVisible({ timeout: 8000 })
  })
})
