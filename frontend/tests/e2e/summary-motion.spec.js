/** Motion de tarjetas ejecutivas con contratos HTTP sintéticos, sin persistencia real. */
import { test, expect } from '@playwright/test'
import { mockSessionTI, ME_RESPONSE_TI, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE, SYNTHETIC_KPI_ITEM } from './helpers.js'

const metrics = (page) => page.locator('section[aria-label="Indicadores principales"] .kpi-metric')

async function observeEntry(page) {
  await page.addInitScript(() => {
    window.__kpiMotionBatches = []
    let previousFirst = null
    const observer = new MutationObserver(() => {
      const cards = [...document.querySelectorAll('section[aria-label="Indicadores principales"] .kpi-metric')]
      if (cards.length !== 5 || cards[0] === previousFirst) return
      previousFirst = cards[0]
      window.__kpiMotionBatches.push(cards.map((card) => {
        const style = getComputedStyle(card); const rect = card.getBoundingClientRect()
        const animation = card.getAnimations()[0]
        return {
          opacity: Number(style.opacity), translateY: style.transform === 'none' ? 0 : new DOMMatrixReadOnly(style.transform).m42,
          name: style.animationName, duration: parseFloat(style.animationDuration) * 1000,
          delay: parseFloat(style.animationDelay) * 1000, easing: style.animationTimingFunction,
          timeline: animation?.effect?.getComputedTiming(), keyframes: animation?.effect?.getKeyframes(),
          width: rect.width, height: rect.height, layoutY: card.offsetTop,
        }
      }))
    })
    observer.observe(document, { childList: true, subtree: true })
  })
}

async function analyticsRoutes(page) {
  let kpiReads = 0
  await page.route('**/api/dashboard/kpis**', (route) => { kpiReads++; return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_KPI_RESPONSE) }) })
  await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
  return () => kpiReads
}

async function waitForFinal(page) {
  await expect(metrics(page)).toHaveCount(5)
  await expect.poll(() => metrics(page).evaluateAll((cards) => cards.every((card) => {
    const style = getComputedStyle(card)
    return Number(style.opacity) === 1 && (style.transform === 'none' || Math.abs(new DOMMatrixReadOnly(style.transform).m42) < 0.01)
  }))).toBe(true)
}

test.beforeEach(async ({ page }) => { await mockSessionTI(page); await observeEntry(page) })

test.describe('ROBUSTNESS: motion ejecutivo aprobado', () => {
  test('entrada normal usa opacidad, desplazamiento6px,180ms,ease-out y stagger ordenado sin cambiar geometría final', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await analyticsRoutes(page)
    await page.goto('/')
    await expect(metrics(page)).toHaveCount(5)
    const initial = await page.evaluate(() => window.__kpiMotionBatches[0])
    expect(initial).toHaveLength(5)
    expect(initial.map((card) => card.delay)).toEqual([0, 40, 80, 120, 160])
    initial.forEach((card) => {
      expect(card.opacity).toBeLessThan(1)
      expect(card.translateY).toBeGreaterThanOrEqual(4.5)
      expect(card.translateY).toBeLessThanOrEqual(6.1)
      expect(card.duration).toBe(180)
      expect(card.easing).toBe('ease-out')
      expect(Math.abs(card.timeline.endTime - (card.delay + 180))).toBeLessThan(2)
      expect(card.keyframes[0]).toMatchObject({ opacity: '0' })
      expect(card.keyframes[1]).toMatchObject({ opacity: '1' })
    })
    await waitForFinal(page)
    const final = await metrics(page).evaluateAll((cards) => cards.map((card) => ({ height: card.getBoundingClientRect().height, width: card.getBoundingClientRect().width, layoutY: card.offsetTop })))
    final.forEach((card, index) => { expect(card.height).toBe(128); expect(card.width).toBe(initial[index].width); expect(card.layoutY).toBe(initial[index].layoutY) })
    expect(Math.abs(initial[4].timeline.endTime - 340)).toBeLessThan(2)
    expect(await page.locator('figure').evaluateAll((figures) => figures.flatMap((figure) => figure.getAnimations({ subtree: true })).length)).toBe(0)
  })
  test('reduced motion muestra estado final inmediato sin animación ni stagger', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await analyticsRoutes(page)
    await page.goto('/')
    await expect(metrics(page)).toHaveCount(5)
    const initial = await page.evaluate(() => window.__kpiMotionBatches[0])
    initial.forEach((card) => { expect(card.opacity).toBe(1); expect(card.translateY).toBe(0); expect(card.name).toBe('none'); expect(card.delay).toBe(0); expect(card.timeline).toBeUndefined() })
    await expect(metrics(page).getByText('Sin observación', { exact: true })).toHaveCount(5)
    expect(await metrics(page).evaluateAll((cards) => cards.flatMap((card) => card.getAnimations()).length)).toBe(0)
  })
  test('refetch silencioso de la misma URL no reinicia las cinco entradas', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    const reads = await analyticsRoutes(page)
    await page.goto('/')
    await waitForFinal(page)
    const before = reads()
    await page.route('**/api/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...ME_RESPONSE_TI, username: 'usuario-prueba-actualizado' }) }))
    const nextRead = page.waitForResponse((response) => response.url().includes('/api/dashboard/kpis'))
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    await nextRead
    await expect.poll(reads).toBeGreaterThan(before)
    await expect(metrics(page)).toHaveCount(5)
    await expect.poll(() => page.evaluate(() => window.__kpiMotionBatches.length)).toBe(2)
    const refetched = await page.evaluate(() => window.__kpiMotionBatches[1])
    refetched.forEach((card) => { expect(card.name).toBe('none'); expect(card.opacity).toBe(1); expect(card.translateY).toBe(0); expect(card.delay).toBe(0) })
    expect(await metrics(page).evaluateAll((cards) => cards.flatMap((card) => card.getAnimations()).length)).toBe(0)
    await expect(metrics(page).getByText('Sin observación', { exact: true })).toHaveCount(5)
  })
  test('un cambio material del periodo permite una nueva entrada, mientras editar el borrador no la reinicia', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'no-preference' })
    await analyticsRoutes(page)
    await page.goto('/')
    await waitForFinal(page)
    await page.getByLabel('Análisis', { exact: true }).selectOption('historical')
    await page.getByLabel('Periodo', { exact: true }).selectOption('current_month')
    expect(await page.evaluate(() => window.__kpiMotionBatches.length)).toBe(1)
    const nextRead = page.waitForResponse((response) => response.url().includes('/api/dashboard/kpis') && response.url().includes('period=current_month'))
    await page.getByRole('button', { name: 'Aplicar filtros' }).click()
    await nextRead
    await expect.poll(() => page.evaluate(() => window.__kpiMotionBatches.length)).toBe(2)
    const selected = await page.evaluate(() => window.__kpiMotionBatches[1])
    selected.forEach((card) => expect(card.name).toBe('kpi-entry'))
    expect(selected.map((card) => card.delay)).toEqual([0, 40, 80, 120, 160])
    await waitForFinal(page)
  })
  test('las tarjetas de otras vistas analíticas y las gráficas no reciben motion global', async ({ page }) => {
    await analyticsRoutes(page)
    await page.route('**/api/dashboard/kpis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...EMPTY_KPI_RESPONSE, items: [SYNTHETIC_KPI_ITEM] }) }))
    await page.goto('/contratos')
    await expect(metrics(page)).toHaveCount(1)
    await expect(page.locator('.kpi-entry')).toHaveCount(0)
    expect(await metrics(page).first().evaluate((card) => getComputedStyle(card).animationName)).toBe('none')
    expect(await page.locator('figure').evaluateAll((figures) => figures.flatMap((figure) => figure.getAnimations({ subtree: true })).length)).toBe(0)
  })
})
