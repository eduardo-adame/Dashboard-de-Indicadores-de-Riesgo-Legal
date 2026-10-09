/** Contratos HTTP sintéticos para estados por dominio; no mutan datos persistidos. */
import { test, expect } from '@playwright/test'
import { KPI_CATALOG } from '../../src/features/analytics/adapters.js'
import { mockSessionTI, SYNTHETIC_KPI_ITEM, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE } from './helpers.js'

const item = (code, overrides = {}) => ({ ...SYNTHETIC_KPI_ITEM, kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO', ...overrides })
const exposure = (levels = ['alto', 'medio', 'bajo'], overrides = {}) => levels.map((level) => item('KPI-LI-01', { dimensions: { nivel_severidad: level }, value: { alto: '200000', medio: '90000', bajo: '25000' }[level], ...overrides }))
const catalogs = { contratos: ['KPI-RC-03'], litigios: ['KPI-LI-01', 'KPI-LI-05'] }
const cards = (page) => page.locator('section[aria-label="Indicadores principales"] .kpi-row > .kpi-metric')
async function routes(page, items = [], status = 200) {
  await page.route('**/api/dashboard/kpis**', (route) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ ...EMPTY_KPI_RESPONSE, items }) }))
  await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
}
async function assertCatalog(page, domain) {
  await expect(cards(page)).toHaveCount(catalogs[domain].length)
  expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.getAttribute('aria-label')))).toEqual(catalogs[domain].map((code) => KPI_CATALOG[code].name))
  await expect(page.getByText('Sin indicadores principales')).toHaveCount(0)
  await expect(page.locator('.kpi-entry')).toHaveCount(0)
  expect(await cards(page).evaluateAll((elements) => elements.every((element) => getComputedStyle(element).animationName === 'none' && element.getBoundingClientRect().height === 128))).toBe(true)
  expect(await page.evaluate(() => document.body.scrollWidth <= document.body.clientWidth + 1)).toBe(true)
}

test.beforeEach(async ({ page }) => { await mockSessionTI(page) })

test.describe('SRS_REQUIRED: catálogo y estados de contratos/litigios', () => {
  for (const domain of Object.keys(catalogs)) {
    test(`${domain}: poblado mantiene catálogo propio y valores reales`, async ({ page }) => {
      await routes(page, [item('KPI-RC-03', { value: '3' }), ...exposure(), item('KPI-LI-05', { value: '1' }), item('KPI-RC-01', { value: '5.5' })])
      await page.goto(`/${domain}?period=current_month&risk_type=all`)
      await assertCatalog(page, domain)
      if (domain === 'contratos') await expect(cards(page).first().locator('.kpi-value-slot > span').first()).toHaveText('3')
      else {
        await expect(cards(page).first().locator('.kpi-value-slot > span').first()).toHaveText('$315,000 MXN')
        await expect(cards(page).nth(1).locator('.kpi-value-slot > span').first()).toHaveText('1')
      }
      expect(await cards(page).evaluateAll((elements) => elements.every((element) => element.dataset.state === 'VALUE'))).toBe(true)
    })
    test(`${domain}: vacío mantiene slots y estado completo en árbol accesible`, async ({ page }) => {
      await routes(page)
      await page.goto(`/${domain}`)
      await assertCatalog(page, domain)
      expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.dataset.state))).toEqual(catalogs[domain].map(() => 'NO_OBSERVATION'))
      const snapshot = await page.locator('section[aria-label="Indicadores principales"]').ariaSnapshot()
      expect(snapshot.match(/Sin observación para la selección/g)).toHaveLength(catalogs[domain].length)
      expect(snapshot).not.toMatch(/(?:text|generic): ["']?Sin observación["']?(?:\r?\n|$)/)
    })
    test(`${domain}: cero observado no equivale a ausencia`, async ({ page }) => {
      await routes(page, [item('KPI-RC-03', { value: '0' }), ...exposure(undefined, { value: '0' }), item('KPI-LI-05', { value: '0' })])
      await page.goto(`/${domain}`)
      await assertCatalog(page, domain)
      expect(await cards(page).evaluateAll((elements) => elements.every((element) => element.dataset.state === 'ZERO'))).toBe(true)
      await expect(cards(page).first().locator('.kpi-value-slot > span').first()).toHaveText(domain === 'contratos' ? '0' : '$0 MXN')
    })
    test(`${domain}: ND explícito conserva estado compacto sin falso cero`, async ({ page }) => {
      const nd = { availability: 'NO_DISPONIBLE', value: null }
      await routes(page, [item('KPI-RC-03', nd), ...exposure(undefined, nd), item('KPI-LI-05', nd)])
      await page.goto(`/${domain}`)
      await assertCatalog(page, domain)
      expect(await cards(page).evaluateAll((elements) => elements.every((element) => element.dataset.state === 'NO_DISPONIBLE'))).toBe(true)
      await expect(cards(page).getByText('No disponible', { exact: true })).toHaveCount(catalogs[domain].length)
      expect(await cards(page).evaluateAll((elements) => elements.every((element) => {
        const value = element.querySelector('.kpi-value-slot > span'); const delta = element.querySelector('.kpi-delta-slot')
        return getComputedStyle(value).fontSize === '14px' && value.getBoundingClientRect().bottom <= delta.getBoundingClientRect().top
      }))).toBe(true)
    })
    for (const status of [403, 503]) {
      test(`${domain}: error ${status} preserva frontera de autorización/lectura`, async ({ page }) => {
        await routes(page, [], status)
        await page.goto(`/${domain}`)
        await expect(page.getByRole('alert').first()).toContainText(status === 403 ? 'Sin autorización' : 'No se pudo completar')
        await expect(page.locator('.kpi-metric')).toHaveCount(0)
      })
    }
  }
  test('contratos parcial: RC01 contextual no reemplaza RC03 ni importa LI05', async ({ page }) => {
    await routes(page, [item('KPI-RC-01', { value: '5.5' }), item('KPI-LI-05', { value: '1' })])
    await page.goto('/contratos')
    await assertCatalog(page, 'contratos')
    await expect(cards(page).first()).toHaveAttribute('data-state', 'NO_OBSERVATION')
    await expect(page.getByRole('heading', { name: 'Contexto operativo' })).toBeVisible()
  })
  test('litigios parcial: LI05 cero conserva LI01 ausente', async ({ page }) => {
    await routes(page, [item('KPI-LI-05', { value: '0' })])
    await page.goto('/litigios')
    await assertCatalog(page, 'litigios')
    expect(await cards(page).evaluateAll((elements) => elements.map((element) => element.dataset.state))).toEqual(['NO_OBSERVATION', 'ZERO'])
  })
  test('litigios incompleto: LI01 ofrece desglose, nunca total parcial', async ({ page }) => {
    await routes(page, exposure(['alto', 'medio']))
    await page.goto('/litigios')
    await assertCatalog(page, 'litigios')
    await expect(cards(page).first()).toContainText('Desglose disponible')
    await expect(cards(page).first()).not.toContainText('290,000')
    expect(await page.locator('section[aria-label="Indicadores principales"]').ariaSnapshot()).toContain('Desglose disponible en gráfica y tabla')
  })
  test('filtro de dominio incompatible se distingue de ausencia', async ({ page }) => {
    await routes(page)
    await page.goto('/contratos?period=current_month&risk_type=litigation')
    await expect(page.locator('section[aria-label="Indicadores principales"]')).toBeVisible()
    await expect(cards(page)).toHaveCount(0)
    await expect(page.getByText('Sin observación para la selección')).toHaveCount(0)
  })
})

test.describe('ROBUSTNESS: geometría loading/final estable sin ampliar motion', () => {
  for (const domain of Object.keys(catalogs)) {
    test(`${domain}: loading y resultado conservan número y geometría de slots`, async ({ page }) => {
      let release
      const pending = new Promise((resolve) => { release = resolve })
      await routes(page)
      await page.route('**/api/dashboard/kpis**', async (route) => { await pending; await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_KPI_RESPONSE) }) })
      await page.goto(`/${domain}`)
      const skeletons = page.locator('.kpi-row > .kpi-metric[data-state="LOADING"]')
      await expect(skeletons).toHaveCount(catalogs[domain].length)
      const geometry = (locator) => locator.evaluateAll((elements) => elements.map((element) => {
        const { x, y, width, height } = element.getBoundingClientRect()
        return { x, y, width, height }
      }))
      const before = await geometry(skeletons)
      before.forEach(({ height }) => expect(height).toBe(128))
      release()
      await assertCatalog(page, domain)
      const after = await geometry(cards(page))
      after.forEach((rect, index) => { for (const field of ['x', 'y', 'width', 'height']) expect(Math.abs(rect[field] - before[index][field])).toBeLessThanOrEqual(1) })
    })
  }
})
