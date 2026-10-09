import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Smoke del contenedor poblado: escucha respuestas reales y nunca intercepta API.
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const workspace = path.resolve(frontend, '..', '..')
const evidence = path.join(workspace, 'integration-evidence', 'design-final-certification-20261008')
const credentialLine = fs.readFileSync(path.join(frontend, '..', '.env'), 'utf8')
  .split(/\r?\n/).find((line) => line.startsWith('TI_RUNTIME_PASSWORD='))
const password = credentialLine?.slice(credentialLine.indexOf('=') + 1).trim()
assert.ok(password, 'Falta la credencial local de pruebas.')
fs.mkdirSync(evidence, { recursive: true })

const browser = await chromium.launch({ headless: true })
const observations = []
let realGroqRequests = 0
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await context.newPage()
  const consoleErrors = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('request', (request) => {
    if (/groq|\/rag\/query/i.test(request.url())) realGroqRequests++
  })
  await page.goto('http://localhost:3000/login')
  await page.getByLabel('Usuario').fill('ti-int-postfix')
  await page.getByLabel('Contraseña').fill(password)
  await Promise.all([
    page.waitForURL((url) => url.pathname !== '/login'),
    page.getByRole('button', { name: 'Iniciar sesión' }).click(),
  ])
  consoleErrors.length = 0

  const cases = [
    ['summary', '/?period=current_month&risk_type=all', 'Resumen ejecutivo', 2],
    ['contracts', '/contratos?period=last_6_months&risk_type=contractual', 'Riesgo contractual', 1],
    ['litigation', '/litigios?period=current_month&risk_type=litigation', 'Gestión de litigios', 2],
    ['trends', '/tendencias?period=current_month&risk_type=all', 'Tendencias y riesgos', 0],
  ]
  for (const [name, route, heading, figures] of cases) {
    const kpiResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard/kpis' && response.request().method() === 'GET')
    const analysisResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/dashboard/analysis' && response.request().method() === 'GET')
    await page.goto(`http://localhost:3000${route}`)
    const [kpiHttp, analysisHttp] = await Promise.all([kpiResponse, analysisResponse])
    assert.equal(kpiHttp.status(), 200)
    assert.equal(analysisHttp.status(), 200)
    const [kpis, analysis] = await Promise.all([kpiHttp.json(), analysisHttp.json()])
    assert.ok(kpis.items.length > 0, `${name}: se requieren observaciones reales.`)
    await page.getByRole('heading', { name: heading, exact: true }).waitFor()
    await page.getByText('Cargando…', { exact: true }).first().waitFor({ state: 'hidden' })
    assert.equal(await page.locator('figure').count(), figures)
    assert.equal(await page.getByRole('alert').count(), 0)

    if (name === 'summary' || name === 'litigation') {
      const exposure = kpis.items.filter((item) => item.kpi_code === 'KPI-LI-01')
      assert.deepEqual(exposure.map((item) => item.dimensions.nivel_severidad).sort(), ['alto', 'bajo', 'medio'])
      assert.deepEqual(Object.fromEntries(exposure.map((item) => [item.dimensions.nivel_severidad, Number(item.value)])), { alto: 200000, bajo: 25000, medio: 90000 })
      const table = page.getByRole('table', { name: name === 'summary' ? 'Datos de Composición por severidad' : 'Datos de Evolución de exposición litigiosa', exact: true })
      await table.waitFor()
      assert.match(await table.innerText(), /200,000/)
      if (name === 'summary') {
        assert.match(await table.innerText(), /63\.49/)
        assert.match(await page.locator('figure').nth(1).innerText(), /\$315,000 MXN/)
      } else {
        assert.match(await table.innerText(), /\$315,000 MXN/)
      }
    }
    if (name === 'contracts') {
      assert.ok(kpis.items.some((item) => item.kpi_code === 'KPI-RC-01'))
      await page.getByRole('table', { name: 'Datos de Tiempo de ciclo del contrato', exact: true }).waitFor()
    }
    if (name === 'trends') {
      assert.equal(analysis.alert_count, 0)
      assert.ok(analysis.items.length > 0)
      await page.getByText('Análisis completado sin hallazgos', { exact: true }).waitFor()
      assert.equal(await page.locator('main .recharts-surface').count(), 0)
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
    assert.equal(overflow, false)
    await page.screenshot({ path: path.join(evidence, `real-${name}-1440.png`) })
    observations.push({ route, status: 'PASS', kpi_http: kpiHttp.status(), analysis_http: analysisHttp.status(), kpi_count: kpis.items.length, analysis_count: analysis.items.length, finding_count: analysis.alert_count, figures, overflow })
  }
  assert.equal(realGroqRequests, 0)
  assert.deepEqual(consoleErrors, [])
  fs.writeFileSync(path.join(evidence, 'real-runtime-smoke.json'), JSON.stringify({ classification: 'SYSTEM_E2E_REAL', base_url: 'http://localhost:3000', api_interceptions: 0, real_groq_requests: realGroqRequests, console_errors: consoleErrors, cases: observations }, null, 2))
  console.log(`REAL_RUNTIME_SMOKE: ${observations.length}/4 PASS; API_INTERCEPTIONS=0; REAL_GROQ_CALLS=0`)
  await context.close()
} finally {
  await browser.close()
}
