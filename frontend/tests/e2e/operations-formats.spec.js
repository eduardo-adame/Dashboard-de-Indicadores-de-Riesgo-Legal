/** BROWSER_CONTRACT_E2E: selector sintético, sin enviar archivos ni mutar DB. */
import { test, expect } from '@playwright/test'
import { mockSessionTI, mockDashboardEmpty } from './helpers.js'

test('SRS_REQUIRED: carga ofrece cuatro formatos canónicos y no ZIP', async ({ page }) => {
  await mockSessionTI(page); await mockDashboardEmpty(page)
  await page.route('**/api/documents/ocr**', (route) => route.fulfill({ json: { items: [], next_cursor: null } }))
  let uploads = 0
  await page.route('**/api/ingestion/uploads', (route) => { uploads++; return route.fulfill({ status: 503, json: { detail: 'No debe enviarse en esta prueba de selector' } }) })
  await page.goto('/ingesta')
  const input = page.getByLabel('Archivo (máximo 50 MiB)', { exact: true })
  await expect(input).toHaveAttribute('accept', '.csv,.xlsx,.pdf,.docx')
  await expect(page.getByText(/\bZIP\b/i)).toHaveCount(0)
  await expect(page.getByRole('option', { name: /ZIP/i })).toHaveCount(0)
  for (const [extension, mimeType] of [
    ['csv', 'text/csv'],
    ['xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['pdf', 'application/pdf'],
    ['docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ]) {
    await input.setInputFiles({ name: `sintetico.${extension}`, mimeType, buffer: Buffer.from('Contenido sintético no enviado') })
    expect(await input.evaluate((element) => element.files[0].name)).toBe(`sintetico.${extension}`)
    await expect(page.getByRole('button', { name: 'Recibir y procesar', exact: true })).toBeEnabled()
  }
  expect(uploads).toBe(0)
})
