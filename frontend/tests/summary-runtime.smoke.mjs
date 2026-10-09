import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { mockSessionTI, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE, ME_RESPONSE_TI } from './e2e/helpers.js'

// Evidencia suplementaria: separar lecturas reales de escenarios HTTP controlados.
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.resolve(frontend, '../../integration-evidence/summary-kpi-motion-post-fix-20261009')
fs.mkdirSync(out, { recursive: true })
const browser = await chromium.launch({ headless: true })
const report = { real_groq_requests: 0, real: [], controlled: [], motion: {} }
const viewports = [[1440, 900], [1024, 768], [390, 844]]
const codes = Object.keys(KPI_CATALOG).filter((code) => KPI_CATALOG[code].core)
const selector = 'section[aria-label="Indicadores principales"] .kpi-row > div'
const observation = (code, dimensions = {}, value = '0', availability = 'DISPONIBLE') => ({
  kpi_code: code, ...KPI_CATALOG[code], classification: 'MVP-NÚCLEO', dimensions,
  value, availability, period_start: '2026-10-01', period_end: '2026-10-31',
  as_of_date: '2026-10-09', calculated_at: '2026-10-09T00:00:00Z', entity_filter_applicable: false,
})
const full = [observation(codes[0], {}, '3'),
  ...['alto', 'medio', 'bajo'].map((severity, index) => observation(codes[1], { nivel_severidad: severity }, String([200000, 90000, 25000][index]))),
  observation(codes[2], {}, '1'), observation(codes[3], {}, '3'),
  observation(codes[4], { area: 'Área sintética', nivel_severidad: 'alto' }, '1'),
  observation(codes[4], { area: 'Área sintética', nivel_severidad: 'bajo' }, '0')]

async function measure(page) {
  return page.evaluate((selector) => {
    const rect = (element) => {
      const box = element.getBoundingClientRect()
      return { x: box.x, y: box.y, width: box.width, height: box.height }
    }
    const cards = [...document.querySelectorAll(selector)].map((element) => {
      const value = element.children[1], delta = element.children[2]
      const style = getComputedStyle(element)
      const valueText = value?.querySelector('span')
      return { label: element.children[0]?.innerText, value: valueText?.innerText,
        state: element.dataset.state, rect: rect(element),
        value_rect: value ? rect(value) : null, delta_rect: delta ? rect(delta) : null,
        value_text_rect: valueText ? rect(valueText) : null,
        font_size: valueText ? getComputedStyle(valueText).fontSize : null,
        opacity: style.opacity, transform: style.transform, animation: style.animationName,
        delay: style.animationDelay, duration: style.animationDuration,
        animations: element.getAnimations().map((animation) => ({
          state: animation.playState, timing: animation.effect.getTiming(),
          progress: animation.effect.getComputedTiming().progress,
          keyframes: animation.effect.getKeyframes().map(({ opacity, transform, offset }) => ({ opacity, transform, offset })),
        })) }
    })
    const loading = document.querySelector('[role="status"] .kpi-row')
    return { url: location.href, cards,
      skeletons: loading ? [...loading.children].map(rect) : [],
      grid: cards.length ? getComputedStyle(document.querySelector(selector).parentElement).gridTemplateColumns : null,
      loading_grid: loading ? getComputedStyle(loading).gridTemplateColumns : null,
      page_overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      internal_ids_visible: /KPI-[A-Z]{2}-\d{2}|RF-\d{3}/.test(document.querySelector('main')?.innerText || ''),
      errors: [...document.querySelectorAll('[role="alert"]')].map((element) => element.innerText),
      reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      svg_geometry: [...document.querySelectorAll('.recharts-bar-rectangle path,.recharts-pie-sector path')].map((element) => element.getAttribute('d')) }
  }, selector)
}

function checkCards(result, count = 5) {
  assert.equal(result.cards.length, count)
  assert.equal(result.page_overflow, false)
  assert.equal(result.internal_ids_visible, false)
  for (const card of result.cards) {
    assert.equal(card.rect.height, 128)
    if (card.value_text_rect) assert.ok(card.value_text_rect.y + card.value_text_rect.height <= card.delta_rect.y + 1, `Solapamiento: ${card.label}`)
  }
}

async function waitCards(page) {
  await page.waitForFunction((selector) => {
    const cards = [...document.querySelectorAll(selector)]
    return cards.length > 0 && cards.every((card) => card.dataset.state !== 'LOADING')
  }, selector)
}

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  page.on('request', (request) => { if (/groq|\/rag\/query/i.test(request.url())) report.real_groq_requests++ })
  const line = fs.readFileSync(path.join(frontend, '../.env'), 'utf8').split(/\r?\n/).find((value) => value.startsWith('TI_RUNTIME_PASSWORD='))
  const password = line?.slice(line.indexOf('=') + 1).trim()
  assert.ok(password, 'Falta la credencial local de pruebas.')
  await page.goto('http://localhost:3000/login')
  await page.getByLabel('Usuario').fill('ti-int-postfix')
  await page.getByLabel('Contraseña').fill(password)
  await Promise.all([page.waitForURL((url) => url.pathname !== '/login'), page.getByRole('button', { name: 'Iniciar sesión' }).click()])
  for (const [width, height] of viewports) {
    await page.setViewportSize({ width, height })
    const received = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard/kpis')
    await page.goto('http://localhost:3000/?period=current_month&risk_type=all')
    const response = await received
    assert.equal(response.status(), 200)
    const dto = await response.json()
    await waitCards(page); await page.waitForTimeout(400)
    const result = await measure(page); checkCards(result)
    assert.match(result.cards[1].value, /315,000/)
    assert.equal(result.cards[4].value, 'Desglose disponible')
    await page.screenshot({ path: path.join(out, `summary-final-current-${width}.png`) })
    report.real.push({ name: 'CURRENT_MONTH', width, classification: 'SYSTEM_E2E_REAL', api_interceptions: 0, observation_count: dto.items.length, ...result })
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  const received = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard/kpis')
  await page.goto('http://localhost:3000/')
  const dto = await (await received).json()
  await waitCards(page); await page.waitForTimeout(400)
  const completed = await measure(page); checkCards(completed)
  assert.equal(completed.cards[0].state, 'NO_OBSERVATION')
  assert.equal(completed.cards[1].state, 'NO_OBSERVATION')
  assert.ok(['VALUE', 'ZERO'].includes(completed.cards[2].state))
  assert.equal(completed.cards[3].state, 'NO_OBSERVATION')
  assert.equal(completed.cards[4].value, 'Desglose disponible')
  await page.screenshot({ path: path.join(out, 'summary-final-completed-1440.png') })
  report.real.push({ name: 'COMPLETED_MONTHS', classification: 'SYSTEM_E2E_REAL', api_interceptions: 0, observation_count: dto.items.length, ...completed })
  await context.close()

  const cases = [['all', full], ['partial', [observation(codes[2])]], ['no-data', []],
    ['zero', full.map((item) => ({ ...item, value: '0' }))],
    ['nd', full.map((item) => ({ ...item, value: null, availability: 'NO_DISPONIBLE' }))],
    ['incomplete', full.filter((item) => item.dimensions.nivel_severidad !== 'bajo')],
    ['error', full, 503], ['forbidden', full, 403], ['loading', full, 200, true]]
  for (const [width, height] of viewports) {
    for (const [name, items, status = 200, pending = false] of cases) {
      const context = await browser.newContext({ viewport: { width, height }, reducedMotion: 'reduce' })
      const page = await context.newPage(); await mockSessionTI(page)
      let release
      const gate = new Promise((resolve) => { release = resolve })
      await page.route('**/api/dashboard/kpis**', async (route) => {
        if (pending) await gate
        await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? { ...EMPTY_KPI_RESPONSE, items } : { detail: 'Error sintético' }) })
      })
      await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
      await page.goto('http://localhost:3000/?period=current_month&risk_type=all')
      await page.getByRole('heading', { name: 'Resumen ejecutivo', exact: true }).waitFor()
      if (pending) await page.locator('[role="status"] .kpi-row').waitFor()
      else if (status === 200) await waitCards(page)
      else await page.getByRole('alert').first().waitFor()
      const result = await measure(page)
      if (pending) {
        assert.equal(result.skeletons.length, 5)
        assert.ok(result.skeletons.every((rect) => rect.height === 128))
      } else if (status === 200) {
        checkCards(result)
        if (name === 'all') assert.deepEqual(result.cards.map((card) => card.value), ['3', '$315,000 MXN', '1', '3', 'Desglose disponible'])
        if (name === 'partial') assert.deepEqual(result.cards.map((card) => card.state), ['NO_OBSERVATION', 'NO_OBSERVATION', 'ZERO', 'NO_OBSERVATION', 'NO_OBSERVATION'])
        if (name === 'no-data') assert.ok(result.cards.every((card) => card.state === 'NO_OBSERVATION' && card.value === 'Sin observación'))
        if (name === 'nd') assert.ok(result.cards.every((card) => card.state === 'NO_DISPONIBLE' && card.value === 'No disponible'))
        if (name === 'zero') assert.deepEqual(result.cards.map((card) => card.state), ['ZERO', 'ZERO', 'ZERO', 'ZERO', 'VALUE'])
        if (name === 'incomplete') assert.equal(result.cards[1].value, 'Desglose disponible')
        result.accessible_snapshot = await page.locator('section[aria-label="Indicadores principales"]').ariaSnapshot()
        if (name === 'no-data' || name === 'partial') assert.match(result.accessible_snapshot, /Sin observación para la selección/)
      } else {
        assert.equal(result.cards.length, 0)
        assert.ok(result.errors.some((text) => text.includes(status === 403 ? 'Sin autorización' : 'No se pudo completar')))
      }
      if (['partial', 'no-data', 'nd', 'loading'].includes(name)) await page.screenshot({ path: path.join(out, `summary-final-${name}-${width}.png`) })
      if (pending) {
        release(); await waitCards(page)
        const final = await measure(page)
        assert.deepEqual(result.skeletons, final.cards.map((card) => card.rect))
        result.loading_matches_final = true
      }
      report.controlled.push({ name, width, classification: 'BROWSER_CONTRACT_E2E', api_fixture: true, ...result })
      await context.close()
    }
  }
  for (const motion of ['no-preference', 'reduce']) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: motion })
    const page = await context.newPage(); await mockSessionTI(page)
    let username = ME_RESPONSE_TI.username
    await page.route('**/api/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...ME_RESPONSE_TI, username }) }))
    await page.route('**/api/dashboard/analysis**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
    let calls = 0
    await page.route('**/api/dashboard/kpis**', (route) => {
      calls++
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...EMPTY_KPI_RESPONSE, items: full }) })
    })
    await page.addInitScript((selector) => {
      window.__summaryMotion = []
      new MutationObserver(() => {
        const cards = [...document.querySelectorAll(selector)]
        if (cards.length !== 5 || cards.some((card) => card.dataset.state === 'LOADING')) return
        const signature = cards[0]
        if (window.__summaryMotion.some((sample) => sample.node === signature)) return
        const values = cards.map((card) => {
          const style = getComputedStyle(card)
          return { opacity: style.opacity, transform: style.transform, delay: style.animationDelay,
            duration: style.animationDuration, easing: style.animationTimingFunction,
            animation: style.animationName, animations: card.getAnimations().length }
        })
        window.__summaryMotion.push({ node: signature, at: performance.now(), values })
      }).observe(document, { childList: true, subtree: true })
    }, selector)
    await page.goto('http://localhost:3000/?period=current_month&risk_type=all')
    await waitCards(page)
    const initial = await page.evaluate(() => window.__summaryMotion.map(({ at, values }) => ({ at, values })))
    assert.equal(initial.length, 1)
    if (motion === 'no-preference') {
      assert.ok(initial[0].values.every((value) => Number(value.opacity) < 1))
      assert.deepEqual(initial[0].values.map((value) => value.delay), ['0s', '0.04s', '0.08s', '0.12s', '0.16s'])
      assert.ok(initial[0].values.every((value) => value.duration === '0.18s' && value.easing === 'ease-out'))
      const initialOffset = Number.parseFloat(initial[0].values[0].transform.split(',').at(-1))
      assert.ok(Math.abs(initialOffset - 6) < 1.5)
    } else {
      assert.ok(initial[0].values.every((value) => value.opacity === '1' && value.transform === 'none' && value.animations === 0 && value.delay === '0s'))
    }
    await page.waitForTimeout(100)
    const during = await measure(page)
    await page.waitForTimeout(350)
    const final = await measure(page); checkCards(final)
    assert.ok(final.cards.every((card) => card.opacity === '1' && ['none', 'matrix(1, 0, 0, 1, 0, 0)'].includes(card.transform)))
    assert.ok(final.cards.every((card) => card.animations.every((animation) => animation.state === 'finished')))
    const settledRects = final.cards.map((card) => card.rect)
    username = 'usuario-refetch-sintetico'
    const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard/kpis')
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))
    // Revalidar la identidad con permisos iguales cambia generation y fuerza la lectura.
    await refreshed
    await waitCards(page)
    const refetch = await measure(page); checkCards(refetch)
    assert.equal(calls, 2)
    assert.ok(refetch.cards.every((card) => card.animations.length === 0 && card.opacity === '1' && card.transform === 'none'))
    assert.deepEqual(refetch.cards.map((card) => card.rect), settledRects)
    assert.deepEqual(refetch.svg_geometry, final.svg_geometry)
    report.motion[motion] = { classification: 'BROWSER_CONTRACT_E2E', initial, during, final, refetch, kpi_requests: calls, replay: false }
    await context.close()
  }
  assert.equal(report.real_groq_requests, 0)
  fs.writeFileSync(path.join(out, 'runtime-measurements.json'), JSON.stringify(report, null, 2))
  console.log(`SUMMARY_RUNTIME: ${report.real.length} lecturas reales; ${report.controlled.length} escenarios controlados; sin Groq.`)
} finally { await browser.close() }
