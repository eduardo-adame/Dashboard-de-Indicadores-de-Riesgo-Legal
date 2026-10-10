/** Contrato HTTP sintético; no accede a DB ni utiliza credenciales reales. */
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty } from './helpers.js'
import { canonicalQuarantineCauses, quarantineCauseItem } from '../fixtures/quarantineCauses.js'

test.beforeEach(async ({ page }) => {
  await mockSessionTI(page)
  await mockDashboardEmpty(page)
})

test.describe('SRS_REQUIRED: motivos técnicos de cuarentena — BROWSER_CONTRACT_E2E', () => {
  test('consulta global y filtro soportan los 25 motivos con sus descripciones y sin falso 503', async ({ page }) => {
    const items = canonicalQuarantineCauses.map(([code, description], index) => quarantineCauseItem(code, description, index + 1))
    const queries = []
    await page.route('**/api/validation/quarantine**', (route) => {
      const query = new URL(route.request().url()).searchParams
      queries.push(query.get('cause_code'))
      const filtered = query.get('cause_code') ? items.filter((row) => row.cause_code === query.get('cause_code')) : items
      return route.fulfill({ json: { items: filtered, next_cursor: null } })
    })
    await page.goto('/cuarentena')
    const table = page.getByRole('table', { name: 'Elementos en cuarentena' })
    await expect(table.getByRole('row')).toHaveCount(26)
    await expect(page.getByRole('alert')).toHaveCount(0)
    const filter = page.getByRole('combobox', { name: 'Regla infringida', exact: true })
    await expect(filter.getByRole('option')).toHaveCount(26)
    for (const [code, description] of canonicalQuarantineCauses) {
      await expect(table.getByRole('cell', { name: description, exact: true })).toHaveCount(1)
      await expect(filter.getByRole('option', { name: code.replaceAll('_', ' '), exact: true })).toHaveAttribute('value', code)
    }
    for (const [code, description] of canonicalQuarantineCauses) {
      const response = page.waitForResponse((r) => r.url().includes('/api/validation/quarantine?') && new URL(r.url()).searchParams.get('cause_code') === code)
      await filter.selectOption(code)
      await response
      await expect(table.getByRole('row')).toHaveCount(2)
      await expect(table.getByRole('cell', { name: description, exact: true })).toHaveCount(1)
      await expect(filter).toHaveValue(code)
      expect(new URL(page.url()).searchParams.get('cause_code')).toBe(code)
      await expect(page.getByRole('alert')).toHaveCount(0)
    }
    expect(queries).toEqual([null, ...canonicalQuarantineCauses.map(([code]) => code)])
  })
})

test.describe('ROBUSTNESS: causas desconocidas — BROWSER_CONTRACT_E2E', () => {
  test('un código UNKNOWN falla cerrado y no ofrece acciones sobre una tabla parcial', async ({ page }) => {
    const [code, description] = canonicalQuarantineCauses[10]
    await page.route('**/api/validation/quarantine**', (r) => r.fulfill({ json: { items: [quarantineCauseItem(code, description), quarantineCauseItem('UNKNOWN', 'No contractual', 2)], next_cursor: null } }))
    await page.goto('/cuarentena')
    await expect(page.getByRole('alert')).toContainText('El servicio no está disponible temporalmente')
    await expect(page.getByRole('table', { name: 'Elementos en cuarentena' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Descartar', exact: true })).toHaveCount(0)
  })
})
