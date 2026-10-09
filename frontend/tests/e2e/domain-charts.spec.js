/** Pruebas visuales de contrato con fuentes sintéticas; no certifican la conexión real. */
import { test, expect } from '@playwright/test'
import { KPI_CATALOG } from '../../src/features/analytics/adapters.js'
import { mockSessionTI, SYNTHETIC_KPI_ITEM, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE } from './helpers.js'

const item = (code, month, value, overrides = {}) => {
  const end = new Date(`${month}-01T00:00:00Z`); end.setUTCMonth(end.getUTCMonth() + 1); end.setUTCDate(0)
  return { ...SYNTHETIC_KPI_ITEM, kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO', period_start: `${month}-01`, period_end: end.toISOString().slice(0, 10), value, availability: value === null ? 'NO_DISPONIBLE' : 'DISPONIBLE', ...overrides }
}
const exposure = (levels = ['alto', 'medio', 'bajo']) => levels.map((level) => item('KPI-LI-01', '2026-10', { alto: '200000', medio: '90000', bajo: '25000' }[level], { dimensions: { nivel_severidad: level } }))
async function routes(page, items) {
  await page.route('**/api/dashboard/kpis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...EMPTY_KPI_RESPONSE, items }) }))
  await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
}
const figure = (page, title) => page.locator('figure').filter({ has: page.locator('figcaption').getByText(title, { exact: true }) })
const table = (page, title) => page.getByRole('table', { name: `Datos de ${title}` })
async function checkViewport(page, chart) {
  expect(await page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth + 1)).toBe(true)
  expect(await chart.evaluate((element) => element.getBoundingClientRect().right <= innerWidth + 1)).toBe(true)
}
const cycleTitle = 'Tiempo de ciclo del contrato'
const countTitle = 'Nuevos litigios por periodo'
const exposureTitle = 'Evolución de exposición litigiosa'

test.beforeEach(async ({ page }) => { await mockSessionTI(page) })

test.describe('ROBUSTNESS: calendario, unidades y gráficas aprobadas por dominio', () => {
  test('RC01 conserva espacio julio y dos segmentos, con agosto30 exacto', async ({ page }) => {
    await routes(page, [['2026-08', '30'], ['2026-05', '10'], ['2026-09', '15'], ['2026-06', '20']].map(([month, value]) => item('KPI-RC-01', month, value)))
    await page.goto('/contratos')
    const chart = figure(page, cycleTitle)
    const dots = chart.locator('circle.recharts-line-dot')
    await expect(dots).toHaveCount(4)
    const positions = (await dots.evaluateAll((elements) => elements.map((element) => Number(element.getAttribute('cx'))))).sort((a, b) => a - b)
    expect(Math.abs((positions[2] - positions[1]) / (positions[1] - positions[0]) - 2)).toBeLessThan(0.02)
    await expect(chart.locator('path.recharts-line-curve')).toHaveCount(2)
    expect(await table(page, cycleTitle).locator('tbody tr').allTextContents()).toEqual([
      '01/05/2026 – 31/05/202610díasDisponible', '01/06/2026 – 30/06/202620díasDisponible',
      '01/07/2026 – 31/07/2026Sin observacióndíasSin observación', '01/08/2026 – 31/08/202630díasDisponible', '01/09/2026 – 30/09/202615díasDisponible',
    ])
    await dots.nth(1).hover()
    const tooltip = chart.locator('.recharts-tooltip-wrapper')
    await expect(tooltip).toContainText('Tiempo de ciclo')
    await expect(tooltip).toContainText('20 días')
    const tooltipBounds = await tooltip.boundingBox()
    expect(tooltipBounds.x).toBeGreaterThanOrEqual(0)
    expect(tooltipBounds.x + tooltipBounds.width).toBeLessThanOrEqual(page.viewportSize().width + 1)
    await checkViewport(page, chart)
  })
  test('LI05 cinco unos se comparan como barras iguales, ticks enteros y margen superior', async ({ page }) => {
    await routes(page, ['05', '06', '07', '08', '09'].map((month) => item('KPI-LI-05', `2026-${month}`, '1')))
    await page.goto('/litigios')
    const chart = figure(page, countTitle)
    const bars = chart.locator('.recharts-bar-rectangle')
    await expect(bars).toHaveCount(5)
    const rectangles = await bars.evaluateAll((elements) => elements.map((element) => ({ y: element.getBoundingClientRect().y, height: element.getBoundingClientRect().height })))
    expect(rectangles.every(({ height }) => height > 0 && Math.abs(height - rectangles[0].height) < 1)).toBe(true)
    const top = await chart.locator('.recharts-wrapper').evaluate((element) => element.getBoundingClientRect().top)
    expect(rectangles.every(({ y }) => y - top > 20)).toBe(true)
    const ticks = await chart.locator('.recharts-yAxis-tick-labels .recharts-cartesian-axis-tick-value').allTextContents()
    expect(ticks).toEqual(['0', '1', '2'])
    await expect(chart.locator('path.recharts-line-curve')).toHaveCount(0)
    await bars.nth(2).hover()
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('Nuevos litigios')
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('1 litigios')
    await checkViewport(page, chart)
  })
  test('un único mes observado no crea historia RC01 ni dominio degenerado', async ({ page }) => {
    await routes(page, [item('KPI-RC-01', '2026-10', '5.5')])
    await page.goto('/contratos')
    const chart = figure(page, cycleTitle)
    await expect(chart.locator('circle.recharts-line-dot')).toHaveCount(1)
    await expect(table(page, cycleTitle).locator('tbody tr')).toHaveCount(1)
    await chart.locator('circle.recharts-line-dot').hover()
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('5.5 días')
    await checkViewport(page, chart)
  })
  test('LI01 singlepoint real conserva total315000 y alto200000 sin histórico inventado', async ({ page }) => {
    await routes(page, [...exposure(), item('KPI-LI-05', '2026-10', '1')])
    await page.goto('/litigios')
    const chart = figure(page, exposureTitle)
    await expect(chart.locator('circle.recharts-line-dot')).toHaveCount(2)
    await expect(table(page, exposureTitle).locator('tbody tr')).toHaveCount(1)
    await expect(table(page, exposureTitle)).toContainText('$315,000 MXN')
    await expect(table(page, exposureTitle)).toContainText('$200,000 MXN')
    await chart.locator('circle.recharts-line-dot').first().hover()
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('$315,000 MXN')
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('$90,000 MXN')
    await checkViewport(page, chart)
  })
})

test.describe('SRS_REQUIRED: valores, disponibilidad y partición sin imputación', () => {
  test('RC01 diferencia cero, ND y ausencia; preserva promedio decimal en tabla/tooltip', async ({ page }) => {
    await routes(page, [['2026-05', '0'], ['2026-06', null], ['2026-08', '12.5000']].map(([month, value]) => item('KPI-RC-01', month, value)))
    await page.goto('/contratos')
    const rows = table(page, cycleTitle).locator('tbody tr')
    await expect(rows).toHaveCount(4)
    await expect(rows.nth(0)).toContainText('0díasDisponible')
    await expect(rows.nth(1)).toContainText('No disponible')
    await expect(rows.nth(2)).toContainText('Sin observación')
    await expect(rows.nth(3)).toContainText('12.5000')
    const chart = figure(page, cycleTitle)
    await chart.locator('circle.recharts-line-dot').last().hover()
    await expect(chart.locator('.recharts-tooltip-wrapper')).toContainText('12.5000 días')
  })
  test('LI05 cero observado y huecos ND/ausencia no generan conteos fabricados', async ({ page }) => {
    await routes(page, [['2026-05', '1'], ['2026-07', '0'], ['2026-08', null], ['2026-09', '3']].map(([month, value]) => item('KPI-LI-05', month, value)))
    await page.goto('/litigios')
    const rows = table(page, countTitle).locator('tbody tr')
    await expect(rows).toHaveCount(5)
    await expect(rows.nth(1)).toContainText('Sin observación')
    await expect(rows.nth(2)).toContainText('0litigiosDisponible')
    await expect(rows.nth(3)).toContainText('No disponible')
    const chart = figure(page, countTitle)
    const ticks = await chart.locator('.recharts-yAxis-tick-labels .recharts-cartesian-axis-tick-value').allTextContents()
    expect(ticks.length).toBeGreaterThanOrEqual(2)
    expect(ticks.every((value) => /^\d+$/.test(value))).toBe(true)
    expect(await chart.locator('.recharts-bar-rectangle').evaluateAll((elements) => elements.filter((element) => element.getBoundingClientRect().height > 0).length)).toBe(2)
  })
  test('LI01 partición incompleta retiene medio/bajo sin total parcial', async ({ page }) => {
    await routes(page, exposure(['medio', 'bajo']))
    await page.goto('/litigios')
    const data = table(page, exposureTitle)
    await expect(data).toContainText('$90,000 MXN')
    await expect(data).toContainText('$25,000 MXN')
    await expect(data.getByRole('columnheader', { name: 'Total', exact: true })).toHaveCount(0)
    await expect(data).not.toContainText('$115,000 MXN')
    await expect(figure(page, exposureTitle).locator('.recharts-wrapper')).toHaveCount(0)
  })
})

test.describe('ROBUSTNESS: contextos del contrato sin mezclas silenciosas', () => {
  test('LI01 contextos incompatibles no crean total315000 ni continuidad mezclada', async ({ page }) => {
    await routes(page, exposure().map((row, index) => ({ ...row, dimensions: { ...row.dimensions, entity: index === 1 ? 'B' : 'A' } })))
    await page.goto('/litigios')
    const data = table(page, exposureTitle)
    await expect(data.locator('tbody tr')).toHaveCount(2)
    await expect(data).toContainText('entity: A')
    await expect(data).toContainText('entity: B')
    await expect(data.getByRole('columnheader', { name: 'Total', exact: true })).toHaveCount(0)
    await expect(data).not.toContainText('$315,000 MXN')
    await expect(figure(page, exposureTitle).locator('circle.recharts-line-dot')).toHaveCount(1)
  })
})
