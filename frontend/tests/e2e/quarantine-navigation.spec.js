// Contrato HTTP sintético de navegación; no accede a bases ni usa credenciales reales.
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty } from './helpers.js'
import { quarantineCauseItem } from '../fixtures/quarantineCauses.js'
const fileA = '11111111-1111-4111-8111-111111111111'
const fileB = '22222222-2222-4222-8222-222222222222'

test('SRS_REQUIRED: A/B/Atrás/Adelante sincroniza URL, UUID visible y GET sin recarga', async ({ page }) => {
  await mockSessionTI(page); await mockDashboardEmpty(page)
  const queries = []
  await page.route('**/api/validation/quarantine**', route => {
    const query = new URL(route.request().url()).searchParams
    queries.push(query.get('ingest_file_id'))
    return route.fulfill({ json: { items: [{ ...quarantineCauseItem('FORMAT_MISMATCH', 'Formato incompatible'), ingest_file_id: query.get('ingest_file_id') || fileA }], next_cursor: null } })
  })
  await page.goto('/cuarentena'); await expect(page.getByText('Formato incompatible')).toBeVisible()
  const input = page.getByLabel('Archivo de origen (UUID)', { exact: true })
  async function transition(action, id) {
    const read = page.waitForResponse(r => new URL(r.url()).pathname === '/api/validation/quarantine')
    await action(); const response = await read
    expect(new URL(response.url()).searchParams.get('ingest_file_id')).toBe(id)
    await expect(input).toHaveValue(id)
    expect(new URL(page.url()).searchParams.get('ingest_file_id')).toBe(id)
    await expect(page.getByRole('alert')).toHaveCount(0)
  }
  await transition(async () => { await input.fill(fileA); await input.blur() }, fileA)
  await transition(async () => { await input.fill(fileB); await input.blur() }, fileB)
  await transition(() => page.goBack(), fileA)
  await transition(() => page.goForward(), fileB)
  expect(queries).toEqual([null, fileA, fileB, fileA, fileB])
})
