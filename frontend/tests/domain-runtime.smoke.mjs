import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { chromium } from 'playwright'
import { adaptKpis, KPI_CATALOG } from '../src/features/analytics/adapters.js'
import { temporalSeriesViewModel, litigationExposureViewModel } from '../src/features/analytics/viewModels.js'
import { mockSessionTI, EMPTY_KPI_RESPONSE, EMPTY_ANALYSIS_RESPONSE } from './e2e/helpers.js'

// Certificación suplementaria: separar conexión real y contrato HTTP sintético.
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const out = path.resolve(frontend, '../../integration-evidence/contracts-litigation-post-fix-20261009')
fs.mkdirSync(out, { recursive: true })
const head = execFileSync('git', ['-C', path.resolve(frontend, '..'), 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim()
const report = { head, real_groq_requests: 0, real: [], controlled: [] }
const apiValues = { head, classification: 'SYSTEM_E2E_REAL', api_interceptions: 0, reads: [] }
const selector = 'section[aria-label="Indicadores principales"] .kpi-row > div'
const sizes = [[1440, 900], [1024, 768], [390, 844]]
const domains = [['contracts', '/contratos', 1], ['litigation', '/litigios', 2]]
const observation = (code, value = '1', month = '2026-10', dimensions = {}, availability = 'DISPONIBLE') => ({
  kpi_code: code, name: KPI_CATALOG[code].name, unit: KPI_CATALOG[code].unit, classification: KPI_CATALOG[code].core ? 'MVP-NÚCLEO' : 'MVP-COMPLEMENTARIO',
  dimensions, value, availability, period_start: `${month}-01`,
  period_end: `${month}-${new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)), 0)).getUTCDate()}`,
  as_of_date: `${month}-01`, calculated_at: '2026-10-09T00:00:00Z', entity_filter_applicable: false,
})
const partition = ['alto', 'medio', 'bajo'].map((severity, i) => observation('KPI-LI-01', String([200000, 90000, 25000][i]), '2026-10', { nivel_severidad: severity }))
const historic = ['2026-05', '2026-06', '2026-08', '2026-09']
const full = [observation('KPI-RC-03', '3'), ...partition,
  ...historic.map((month, i) => observation('KPI-RC-01', String([10.5, 20, 30, 15][i]), month)),
  ...historic.map((month) => observation('KPI-LI-05', '1', month))]
const fixture = (items) => ({ ...EMPTY_KPI_RESPONSE, period_start: '2026-05-01', period_end: '2026-10-31', period_reference: '2026-10-01',
  filters: { period: 'custom', period_start: '2026-05-01', period_end: '2026-10-31', risk_type: 'all', entity: null }, items })

async function measure(page) {
  return page.evaluate((selector) => {
    const rect = (el) => { const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height } }
    const cards = [...document.querySelectorAll(selector)].map((el) => ({
      label: el.children[0]?.innerText, value: el.children[1]?.querySelector('span')?.innerText,
      state: el.dataset.state, rect: rect(el), animation: getComputedStyle(el).animationName,
    }))
    const loading = document.querySelector('[role="status"] .kpi-row')
    return { url: location.href, viewport: { width: innerWidth, height: innerHeight },
      served_assets: { js: document.querySelector('script[type="module"][src]')?.getAttribute('src'), css: document.querySelector('link[rel="stylesheet"]')?.getAttribute('href') },
      cards, skeletons: loading ? [...loading.children].map(rect) : [],
      overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      internal_ids_visible: /KPI-[A-Z]{2}-\d{2}|RF-\d{3}/.test(document.querySelector('main')?.innerText || ''),
      alerts: [...document.querySelectorAll('[role="alert"]')].map((el) => el.innerText),
      figures: [...document.querySelectorAll('figure')].map((el) => ({
        title: el.querySelector('figcaption')?.innerText, text: el.innerText,
        x_ticks: [...el.querySelectorAll('.recharts-xAxis-tick-labels .recharts-cartesian-axis-tick-value')].map((e) => e.textContent),
        y_ticks: [...el.querySelectorAll('.recharts-yAxis-tick-labels .recharts-cartesian-axis-tick-value')].map((e) => e.textContent),
        y_tick_rects: [...el.querySelectorAll('.recharts-yAxis-tick-labels .recharts-cartesian-axis-tick-value')].map((e) => ({ text: e.textContent, rect: rect(e) })),
        table: [...el.querySelectorAll('tbody tr')].map((row) => [...row.querySelectorAll('td')].map((e) => e.innerText)),
        lines: [...el.querySelectorAll('.recharts-line-curve')].map((e) => e.getAttribute('d')),
        dots: [...el.querySelectorAll('.recharts-line-dot')].map((e) => ({ x: Number(e.getAttribute('cx')), y: Number(e.getAttribute('cy')), rect: rect(e) })),
        bars: [...el.querySelectorAll('.recharts-bar-rectangle path')].map((e) => ({ path: e.getAttribute('d'), rect: rect(e) })),
      })) }
  }, selector)
}
async function waitCards(page) {
  await page.waitForFunction((selector) => { const cards = [...document.querySelectorAll(selector)]; return cards.length > 0 && cards.every((c) => c.dataset.state !== 'LOADING') }, selector)
}
function check(result, count) {
  assert.equal(result.cards.length, count)
  assert.equal(result.overflow, false)
  assert.equal(result.internal_ids_visible, false)
  assert.ok(result.cards.every((card) => card.rect.height === 128 && card.animation === 'none'))
}
function checkCountAxis(result) {
  const figure = result.figures.find((f) => f.title?.includes('Nuevos litigios'))
  if (!figure?.y_ticks.length) return
  const ticks = figure.y_ticks.map((text) => Number(text.replaceAll(',', '')))
  assert.ok(ticks.every(Number.isInteger), 'COUNT no admite ticks fraccionarios.')
  assert.equal(Math.min(...ticks), 0)
  if (figure.bars.length) {
    const top = Math.min(...figure.y_tick_rects.map(({ rect }) => rect.y + rect.height / 2))
    assert.ok(figure.bars.filter(({ rect }) => rect.height > 0).every(({ rect }) => rect.y > top), 'La barra requiere margen superior sin clipping.')
  }
}
function checkTemporalTable(result, title, rows) {
  const figure = result.figures.find((f) => f.title?.includes(title))
  assert.ok(figure, `Falta la figura ${title}.`)
  assert.equal(figure.table.length, rows.length)
  rows.forEach((row, index) => {
    assert.ok(figure.table[index].includes(row.periodText), 'Tabla y VM deben compartir periodo y orden.')
    assert.ok(figure.table[index].some((cell) => cell === row.valueText || cell.startsWith(`${row.valueText} `)), 'Tabla y VM deben conservar el valor exacto.')
  })
}
function checkHistoricalGeometry(result, name, rc, li5) {
  if (name === 'contracts') {
    checkTemporalTable(result, 'Tiempo de ciclo', rc)
    const figure = result.figures.find((f) => f.title?.includes('Tiempo de ciclo'))
    const points = [...figure.dots].sort((a, b) => a.x - b.x)
    assert.equal(points.length, 4, 'Solo deben dibujarse las cuatro observaciones reales.')
    const monthly = points[1].x - points[0].x
    assert.ok(monthly > 0)
    assert.ok(Math.abs((points[2].x - points[1].x) / monthly - 2) < 0.02, 'Junio→agosto debe ocupar dos meses.')
    assert.ok(figure.lines.reduce((sum, curve) => sum + (curve?.match(/M/g)?.length || 0), 0) >= 2, 'El hueco de julio no debe unirse con una línea.')
    assert.ok(figure.table.some((row) => row.includes('Sin observación')))
  } else {
    checkTemporalTable(result, 'Nuevos litigios', li5)
    const figure = result.figures.find((f) => f.title?.includes('Nuevos litigios'))
    assert.equal(figure.bars.filter(({ rect }) => rect.height > 0).length, 5)
    const heights = figure.bars.map(({ rect }) => rect.height)
    assert.ok(Math.max(...heights) - Math.min(...heights) < 0.1, 'Los cinco conteos1 deben tener altura igual.')
    assert.ok(Math.max(...figure.y_ticks.map(Number)) > 1, 'El máximo1 requiere headroom.')
  }
}
async function tooltips(page) {
  const collected = []
  for (const figure of await page.locator('figure').all()) {
    const targets = figure.locator('.recharts-line-dot,.recharts-bar-rectangle')
    if (await targets.count()) {
      await targets.first().scrollIntoViewIfNeeded()
      const box = await targets.first().boundingBox()
      if (box && box.width > 0 && box.height > 0) {
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
        await page.waitForTimeout(80)
        const text = await figure.locator('.recharts-tooltip-wrapper').innerText()
        assert.ok(text.trim(), 'La lectura real debe capturar un tooltip visible, no solo su contenedor.')
        collected.push({ title: await figure.locator('figcaption').innerText(), text })
      }
    }
  }
  await page.mouse.move(0, 0)
  return collected
}
async function capture(page, name, width, charts = false) {
  // El shell desplaza main, no el documento: capturar el viewport evita falsos blancos.
  await page.locator('main').evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: path.join(out, `${name}-${width}.png`) })
  if (charts) {
    let index = 0
    for (const plot of await page.locator('figure .recharts-wrapper').all()) {
      await plot.scrollIntoViewIfNeeded()
      await page.screenshot({ path: path.join(out, `${name}-chart-${++index}-${width}.png`) })
    }
  }
}
const browser = await chromium.launch({ headless: true })
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  page.on('request', (r) => { if (/groq|\/rag\/query/i.test(r.url())) report.real_groq_requests++ })
  // La credencial se utiliza solo en memoria: no capturar login, tokens ni cookies.
  const line = fs.readFileSync(path.join(frontend, '../.env'), 'utf8').split(/\r?\n/).find((s) => s.startsWith('TI_RUNTIME_PASSWORD='))
  const password = line?.slice(line.indexOf('=') + 1).trim()
  assert.ok(password, 'Falta la credencial local de pruebas.')
  await page.goto('http://localhost:3000/login')
  await page.getByLabel('Usuario').fill('ti-int-postfix')
  await page.getByLabel('Contraseña').fill(password)
  await Promise.all([page.waitForURL((url) => url.pathname !== '/login'), page.getByRole('button', { name: 'Iniciar sesión' }).click()])
  for (const [name, route, count] of domains) {
    for (const [width, height] of sizes) {
      await page.setViewportSize({ width, height })
      const kpi = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboard/kpis')
      const analysis = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboard/analysis')
      await page.goto(`http://localhost:3000${route}`)
      const kr = await kpi, ar = await analysis
      assert.equal(kr.status(), 200); assert.equal(ar.status(), 200)
      const dto = await kr.json()
      await waitCards(page); await page.waitForTimeout(150)
      const result = await measure(page); check(result, count)
      checkCountAxis(result)
      if (name === 'contracts') assert.equal(result.cards[0].state, 'NO_OBSERVATION')
      else assert.deepEqual(result.cards.map((c) => c.state), ['NO_OBSERVATION', 'VALUE'])
      const model = adaptKpis(dto)
      const rc = temporalSeriesViewModel(model.items, 'KPI-RC-01')
      const li5 = temporalSeriesViewModel(model.items, 'KPI-LI-05')
      const relevant = dto.items.filter((i) => ['KPI-RC-01', 'KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05'].includes(i.kpi_code))
      assert.deepEqual(relevant.filter((i) => i.kpi_code === 'KPI-RC-01').map((i) => i.value), ['10', '20', '30', '15'])
      assert.equal(rc.find((i) => i.month === '2026-07')?.value, null)
      assert.ok(li5.filter((i) => i.value !== null).every((i) => i.value === 1))
      checkHistoricalGeometry(result, name, rc, li5)
      result.api_vm_table_geometry_consistent = true
      result.tooltips = await tooltips(page)
      result.accessible_snapshot = await page.locator('main').ariaSnapshot()
      await capture(page, name, width, true)
      report.real.push({ name, period: 'last_6_months', width, classification: 'SYSTEM_E2E_REAL', api_interceptions: 0, ...result })
      apiValues.reads.push({ name, width, endpoint: kr.url(), analysis_endpoint: ar.url(), analysis_status: ar.status(),
        period_start: dto.period_start, period_end: dto.period_end, items: relevant, rc01_viewmodel: rc,
        li05_viewmodel: li5, li01_viewmodel: litigationExposureViewModel(model.items) })
    }
    await page.setViewportSize({ width: 1440, height: 900 })
    const received = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboard/kpis')
    const analysisReceived = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/dashboard/analysis')
    await page.goto(`http://localhost:3000${route}?period=current_month&risk_type=all`)
    const kr = await received, ar = await analysisReceived
    assert.equal(kr.status(), 200); assert.equal(ar.status(), 200)
    const dto = await kr.json()
    await waitCards(page); await page.waitForTimeout(150)
    const result = await measure(page); check(result, count)
    checkCountAxis(result)
    if (name === 'contracts') assert.equal(result.cards[0].value, '3')
    else { assert.match(result.cards[0].value, /315,000/); assert.equal(result.cards[1].value, '1') }
    const model = adaptKpis(dto)
    if (name === 'contracts') {
      checkTemporalTable(result, 'Tiempo de ciclo', temporalSeriesViewModel(model.items, 'KPI-RC-01'))
      assert.equal(result.figures.find((f) => f.title?.includes('Tiempo de ciclo'))?.dots.length, 1)
    } else {
      checkTemporalTable(result, 'Nuevos litigios', temporalSeriesViewModel(model.items, 'KPI-LI-05'))
      const exposure = litigationExposureViewModel(model.items)
      assert.equal(exposure.rows.length, 1)
      assert.equal(exposure.rows[0].total, 315000)
      assert.equal(exposure.rows[0].high, 200000)
      const figure = result.figures.find((f) => f.title?.toLowerCase().includes('exposición'))
      assert.ok(figure?.table.flat().includes('$315,000 MXN'))
      assert.ok(figure?.table.flat().includes('$200,000 MXN'))
    }
    result.api_vm_table_geometry_consistent = true
    result.tooltips = await tooltips(page)
    await capture(page, `${name}-current`, 1440, true)
    report.real.push({ name, period: 'current_month', width: 1440, classification: 'SYSTEM_E2E_REAL', api_interceptions: 0, ...result })
    apiValues.reads.push({ name, period: 'current_month', endpoint: kr.url(), analysis_endpoint: ar.url(), analysis_status: ar.status(),
      period_start: dto.period_start, period_end: dto.period_end, items: dto.items.filter((i) => ['KPI-RC-01', 'KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05'].includes(i.kpi_code)),
      rc01_viewmodel: temporalSeriesViewModel(model.items, 'KPI-RC-01'), li05_viewmodel: temporalSeriesViewModel(model.items, 'KPI-LI-05'), li01_viewmodel: litigationExposureViewModel(model.items) })
  }
  await context.close()
  const cases = [['all', full], ['partial', [observation('KPI-LI-05')]], ['no-data', []],
    ['zero', full.map((i) => ({ ...i, value: '0' }))], ['nd', full.map((i) => ({ ...i, value: null, availability: 'NO_DISPONIBLE' }))],
    ['error', full, 503], ['forbidden', full, 403], ['loading', full, 200, true]]
  for (const [name, route, count] of domains) for (const [width, height] of sizes) {
    for (const [caseName, items, status = 200, pending = false] of cases) {
      const context = await browser.newContext({ viewport: { width, height }, reducedMotion: 'reduce' })
      const page = await context.newPage(); await mockSessionTI(page)
      let release
      const gate = new Promise((resolve) => { release = resolve })
      await page.route('**/api/dashboard/kpis**', async (r) => { if (pending) await gate; await r.fulfill({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? fixture(items) : { detail: 'Error sintético' }) }) })
      await page.route('**/api/dashboard/analysis**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(EMPTY_ANALYSIS_RESPONSE) }))
      await page.goto(`http://localhost:3000${route}?period=custom&period_start=2026-05-01&period_end=2026-10-31&risk_type=all`)
      if (pending) await page.locator('[role="status"] .kpi-row').waitFor()
      else if (status === 200) { await waitCards(page); await page.waitForTimeout(80) }
      else await page.getByRole('alert').first().waitFor()
      const result = await measure(page)
      assert.equal(result.overflow, false)
      if (pending) { assert.equal(result.skeletons.length, count); assert.ok(result.skeletons.every((r) => r.height === 128)) }
      else if (status === 200) {
        check(result, count)
        checkCountAxis(result)
        if (caseName === 'no-data') assert.ok(result.cards.every((c) => c.state === 'NO_OBSERVATION'))
        if (caseName === 'nd') assert.ok(result.cards.every((c) => c.state === 'NO_DISPONIBLE'))
        if (caseName === 'zero') assert.ok(result.cards.every((c) => c.state === 'ZERO'))
        if (name === 'litigation' && caseName === 'partial') assert.deepEqual(result.cards.map((c) => c.state), ['NO_OBSERVATION', 'VALUE'])
        result.accessible_snapshot = await page.locator('main').ariaSnapshot()
        result.tooltips = await tooltips(page)
      } else { assert.equal(result.cards.length, 0); assert.ok(result.alerts.some((t) => t.includes(status === 403 ? 'Sin autorización' : 'No se pudo completar'))) }
      if (['all', 'no-data', 'nd', 'loading', 'partial'].includes(caseName)) await capture(page, `${name}-${caseName}`, width)
      if (pending) { release(); await waitCards(page); const final = await measure(page); assert.deepEqual(result.skeletons, final.cards.map((c) => c.rect)); result.loading_matches_final = true }
      report.controlled.push({ name, case: caseName, width, classification: 'BROWSER_CONTRACT_E2E', api_fixture: true, ...result })
      await context.close()
    }
  }
  assert.equal(report.real_groq_requests, 0)
  fs.writeFileSync(path.join(out, 'api-values.json'), JSON.stringify(apiValues, null, 2))
  fs.writeFileSync(path.join(out, 'runtime-measurements.json'), JSON.stringify(report, null, 2))
  console.log(`DOMAIN_RUNTIME: ${report.real.length} lecturas reales; ${report.controlled.length} escenarios controlados; sin Groq.`)
} finally { await browser.close() }
