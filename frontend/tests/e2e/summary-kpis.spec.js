/** Pruebas de contrato y geometría con respuestas sintéticas; no modifican el backend. */
import { test, expect } from '@playwright/test'
import { KPI_CATALOG } from '../../src/features/analytics/adapters.js'
import { mockSessionTI, SYNTHETIC_KPI_ITEM, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE } from './helpers.js'

const item = (code, overrides = {}) => ({ ...SYNTHETIC_KPI_ITEM, kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, ...overrides })
const exposure = (levels = ['alto', 'medio', 'bajo'], overrides = {}) => levels.map((level) => item('KPI-LI-01', { dimensions: { nivel_severidad: level }, value: { alto: '200000', medio: '90000', bajo: '25000' }[level], ...overrides }))
const cards = (page) => page.locator('section[aria-label="Indicadores principales"] .kpi-row > .kpi-metric')

async function responseRoutes(page, items = [], status = 200) {
  await page.route('**/api/dashboard/kpis**', (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ ...EMPTY_KPI_RESPONSE, items }) }))
  await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
}

async function settledGeometry(page, locator) {
  await expect.poll(() => locator.evaluateAll((elements) => elements.some((element) => element.getAnimations().some((animation) => animation.playState === 'running' || animation.pending)))).toBe(false)
  return locator.evaluateAll((elements) => elements.map((element) => {
    const rect = element.getBoundingClientRect()
    const value = element.querySelector('.kpi-value-slot > span')?.getBoundingClientRect()
    const delta = element.querySelector('.kpi-delta-slot')?.getBoundingClientRect()
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, valueBottom: value?.bottom, deltaTop: delta?.top, valueFont: element.querySelector('.kpi-value-slot > span') ? getComputedStyle(element.querySelector('.kpi-value-slot > span')).fontSize : null }
  }))
}

test.beforeEach(async ({ page }) => { await mockSessionTI(page) })

test.describe('SRS_REQUIRED: estructura ejecutiva y estados', () => {
  test('all-data mantiene cinco identidades ordenadas, total LI01 y CN03 sin escalar', async ({ page }) => {
    await responseRoutes(page, [item('KPI-RC-03', { value: '3' }), ...exposure(), item('KPI-LI-05', { value: '1' }), item('KPI-CN-02', { value: '3' }), item('KPI-CN-03', { value: '7', dimensions: { area: 'Legal', nivel_severidad: 'alto' } })])
    await page.goto('/?period=current_month&risk_type=all')
    await expect(cards(page)).toHaveCount(5)
    await expect(cards(page).nth(1)).toContainText('$315,000 MXN')
    await expect(cards(page).nth(4)).toContainText('Desglose disponible')
    expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.getAttribute('aria-label')))).toEqual(['KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03'].map((code) => KPI_CATALOG[code].name))
    await expect(cards(page).nth(4).locator('.kpi-value-slot')).not.toContainText('7')
    await expect(page.locator('body')).not.toContainText('KPI-RC-03')
  })
  test('partial-data LI05=0 conserva las cuatro ausencias y cero observado', async ({ page }) => {
    await responseRoutes(page, [item('KPI-LI-05', { value: '0' })])
    await page.goto('/')
    await expect(cards(page)).toHaveCount(5)
    await expect(cards(page).filter({ has: page.locator('.sr-only').getByText('Sin observación para la selección', { exact: true }) })).toHaveCount(4)
    const accessibleSnapshot = await page.locator('section[aria-label="Indicadores principales"]').ariaSnapshot()
    expect(accessibleSnapshot.match(/Sin observación para la selección/g)).toHaveLength(4)
    expect(accessibleSnapshot).not.toMatch(/(?:text|generic): ["']?Sin observación["']?(?:\r?\n|$)/)
    await expect(cards(page).nth(2)).toHaveAttribute('data-state', 'ZERO')
    await expect(cards(page).nth(2).locator('.kpi-value-slot > span').first()).toHaveText('0')
  })
  test('no-data muestra cinco ausencias compactas sin desbordamiento ni crecimiento', async ({ page }) => {
    await responseRoutes(page)
    await page.goto('/')
    await expect(cards(page)).toHaveCount(5)
    await expect(cards(page).getByText('Sin observación', { exact: true })).toHaveCount(5)
    const geometry = await settledGeometry(page, cards(page))
    geometry.forEach((rect) => { expect(rect.height).toBe(128); expect(rect.valueBottom).toBeLessThanOrEqual(rect.deltaTop); expect(rect.valueFont).toBe('14px') })
    const width = page.viewportSize().width
    const columns = await page.locator('.kpi-row').first().evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(' ').length)
    expect(columns).toBe(width >= 1280 ? 5 : width >= 1024 ? 3 : 1)
    expect(await page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth + 1)).toBe(true)
    await expect(page.getByText('Sin indicadores principales')).toHaveCount(0)
  })
  test('ND simple y dimensional conserva cinco estados compactos distintos de cero', async ({ page }) => {
    const nd = { value: null, availability: 'NO_DISPONIBLE' }
    await responseRoutes(page, [item('KPI-RC-03', nd), ...exposure(undefined, nd), item('KPI-LI-05', nd), item('KPI-CN-02', nd), item('KPI-CN-03', { ...nd, dimensions: { area: 'Legal', nivel_severidad: 'medio' } }), item('KPI-CN-03', { ...nd, dimensions: { area: 'Finanzas', nivel_severidad: 'alto' } })])
    await page.goto('/')
    await expect(cards(page)).toHaveCount(5)
    await expect(cards(page).getByText('No disponible', { exact: true })).toHaveCount(5)
    const geometry = await settledGeometry(page, cards(page))
    geometry.forEach((rect) => { expect(rect.height).toBe(128); expect(rect.valueBottom).toBeLessThanOrEqual(rect.deltaTop); expect(rect.valueFont).toBe('14px') })
    expect(await cards(page).evaluateAll((elements) => elements.every((element) => element.dataset.state === 'NO_DISPONIBLE'))).toBe(true)
    expect((await page.locator('section[aria-label="Indicadores principales"]').ariaSnapshot()).match(/No disponible/g)).toHaveLength(5)
    await expect(cards(page).getByText('Por dimensión')).toHaveCount(0)
  })
  test('cero real RC03 LI05 CN02 no se confunde con las ausencias dimensionales', async ({ page }) => {
    await responseRoutes(page, ['KPI-RC-03', 'KPI-LI-05', 'KPI-CN-02'].map((code) => item(code, { value: '0' })))
    await page.goto('/')
    await expect(cards(page)).toHaveCount(5)
    expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.dataset.state))).toEqual(['ZERO', 'NO_OBSERVATION', 'ZERO', 'ZERO', 'NO_OBSERVATION'])
    await expect(cards(page).getByText('0', { exact: true })).toHaveCount(3)
  })
  test('LI01 incompleto no presenta 290000 como exposición total', async ({ page }) => {
    await responseRoutes(page, exposure(['alto', 'medio']))
    await page.goto('/')
    await expect(cards(page)).toHaveCount(5)
    await expect(cards(page).nth(1)).toContainText('Desglose disponible')
    await expect(cards(page).nth(1)).not.toContainText('290,000')
  })
  test('filtro litigation conserva dos slots aplicables sin cuatro ausencias ajenas al dominio', async ({ page }) => {
    await responseRoutes(page)
    await page.goto('/?period=current_month&risk_type=litigation')
    await expect(cards(page)).toHaveCount(2)
    expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.getAttribute('aria-label')))).toEqual([KPI_CATALOG['KPI-LI-01'].name, KPI_CATALOG['KPI-LI-05'].name])
    await expect(cards(page).getByText('Sin observación', { exact: true })).toHaveCount(2)
  })
  for (const status of [403, 503]) {
    test(`error KPI ${status} preserva frontera sin cinco ceros`, async ({ page }) => {
      await responseRoutes(page, [], status)
      await page.goto('/')
      await expect(page.getByRole('alert').first()).toContainText(status === 403 ? 'Sin autorización' : 'No se pudo completar')
      await expect(cards(page)).toHaveCount(0)
      await expect(page.locator('.kpi-metric')).toHaveCount(0)
    })
  }
})

test.describe('SRS_REQUIRED: geometría compartida loading/final', () => {
  test('los cinco skeletons mantienen posiciones, columnas y altura de los estados finales', async ({ page }) => {
    let release
    const pending = new Promise((resolve) => { release = resolve })
    await responseRoutes(page)
    await page.route('**/api/dashboard/kpis**', async (route) => { await pending; await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_KPI_RESPONSE) }) })
    await page.goto('/')
    const skeletons = page.locator('.kpi-row > .kpi-metric[data-state="LOADING"]')
    await expect(skeletons).toHaveCount(5)
    const before = await settledGeometry(page, skeletons)
    before.forEach((rect) => expect(rect.height).toBe(128))
    const loadingColumns = await skeletons.first().evaluate((element) => getComputedStyle(element.parentElement).gridTemplateColumns)
    release()
    await expect(cards(page)).toHaveCount(5)
    const after = await settledGeometry(page, cards(page))
    after.forEach((rect, index) => {
      for (const dimension of ['x', 'y', 'width', 'height']) expect(Math.abs(rect[dimension] - before[index][dimension])).toBeLessThanOrEqual(1)
    })
    expect(await cards(page).first().evaluate((element) => getComputedStyle(element.parentElement).gridTemplateColumns)).toBe(loadingColumns)
  })
})
