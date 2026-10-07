/**
 * Spec: Sesión y autenticación — pruebas de interfaz/contrato con fixtures sintéticos.
 * Clasificación: BROWSER_CONTRACT_E2E. No usa credenciales reales.
 */
import { test, expect } from '@playwright/test'
import {
  mockSessionTI, mockSessionJuridico, mockMeTI, mockMeJuridico, mockLogout, mockRefresh,
  SYNTHETIC_TOKEN, PRINCIPAL_TI, PRINCIPAL_JURIDICO,
  ME_RESPONSE_TI, ME_RESPONSE_JURIDICO,
} from './helpers.js'

test.describe('Sesión — login exitoso', () => {
  test('muestra el formulario de login al inicio', async ({ page }) => {
    await page.route('**/api/auth/me', (r) => r.fulfill({ status: 401, body: '{}', contentType: 'application/json' }))
    await page.goto('/')
    await expect(page.getByRole('textbox', { name: /usuario/i })).toBeVisible()
  })

  test('login con credenciales sintéticas navega al dashboard', async ({ page }) => {
    await mockSessionTI(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/')
    // Si ya autenticado, la app muestra el shell de navegación
    await expect(page.getByText(PRINCIPAL_TI.username)).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Sesión — credenciales inválidas', () => {
  test('muestra mensaje de error con credenciales incorrectas', async ({ page }) => {
    await page.route('**/api/auth/me', (r) => r.fulfill({ status: 401, body: '{}', contentType: 'application/json' }))
    await page.route('**/api/auth/login', (r) => r.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'Credenciales inválidas' }) }))
    await page.goto('/')
    await page.getByRole('textbox', { name: /usuario/i }).fill('usuario-invalido')
    const passInput = page.getByLabel(/contraseña/i)
    await passInput.fill('claveInvalida123')
    await page.getByRole('button', { name: /iniciar sesión/i }).click()
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 5000 })
  })
})

test.describe('Sesión — refresh', () => {
  test('intercepta refresh correctamente sin exponer token', async ({ page }) => {
    await mockRefresh(page)
    await mockSessionTI(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/')
    // Verificar que la sesión persiste visible
    await expect(page.getByText(PRINCIPAL_TI.username)).toBeVisible({ timeout: 8000 })
    // Sin storageState ni token en localStorage
    const stored = await page.evaluate(() => localStorage.getItem('access_token'))
    expect(stored).toBeNull()
  })
})

test.describe('Sesión — logout', () => {
  test('el logout regresa al formulario de login', async ({ page }) => {
    await mockSessionTI(page)
    await mockLogout(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/')
    await expect(page.getByText(PRINCIPAL_TI.username)).toBeVisible({ timeout: 8000 })
    // Click en cerrar sesión — puede estar en sidebar o topbar
    const logoutBtn = page.getByRole('button', { name: /cerrar sesión/i }).first()
    await logoutBtn.click()
    await expect(page.getByRole('textbox', { name: /usuario/i })).toBeVisible({ timeout: 8000 })
  })
})

test.describe('Sesión — sesión inválida / protección de rutas', () => {
  test('una ruta protegida sin sesión regresa al login', async ({ page }) => {
    await page.route('**/api/auth/me', (r) => r.fulfill({ status: 401, body: '{}', contentType: 'application/json' }))
    await page.goto('/contratos')
    await expect(page.getByRole('textbox', { name: /usuario/i })).toBeVisible({ timeout: 8000 })
  })

  test('navegación por rol: Jurídico no ve rutas TI', async ({ page }) => {
    await mockSessionJuridico(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/')
    await expect(page.getByText(PRINCIPAL_JURIDICO.username)).toBeVisible({ timeout: 8000 })
    // Administración no debe aparecer en nav para Jurídico
    const adminLink = page.getByRole('link', { name: /administración/i })
    await expect(adminLink).toBeHidden()
  })

  test('default deny: Jurídico en ruta de administración recibe 403', async ({ page }) => {
    await mockSessionJuridico(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/administracion')
    // La app debe mostrar acceso denegado (403 o redirigir)
    await expect(page.getByText(/sin autorización|no autorizado|403/i).first()).toBeVisible({ timeout: 8000 })
  })

  test('pérdida de autorización limpia información protegida', async ({ page }) => {
    await mockSessionTI(page)
    await page.route('**/api/analytics/**', (r) => r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }))
    await page.goto('/')
    await expect(page.getByText(PRINCIPAL_TI.username)).toBeVisible({ timeout: 8000 })
    // Simular pérdida de sesión en siguiente petición me
    await page.route('**/api/auth/me', (r) => r.fulfill({ status: 401, body: '{}', contentType: 'application/json' }), { times: 1 })
    // Al recargar la app detecta la sesión inválida
    await page.reload()
    await expect(page.getByRole('textbox', { name: /usuario/i })).toBeVisible({ timeout: 10000 })
    // No debe quedar username visible del usuario anterior
    await expect(page.getByText(PRINCIPAL_TI.username)).toBeHidden()
  })
})
