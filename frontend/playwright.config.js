// Configuración de Playwright para pruebas de interfaz/contrato con fixtures HTTP sintéticos.
// Clasificación: BROWSER_CONTRACT_E2E (no SYSTEM_E2E_REAL).
// Smoke real diferido: REAL_BACKEND_SMOKE = DEFERRED_TO_INT_001.
import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  // Directorio de specs E2E separado de las unitarias Vitest.
  testDir: './tests/e2e',
  // Sin reintentos: los fallos reales deben reportarse, no ocultarse.
  retries: 0,
  workers: 1,
  // Reportes en terminal y en HTML para inspección post-ejecución.
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    // Servidor de desarrollo local; no exponer en interfaz pública.
    baseURL: 'http://127.0.0.1:5173',
    // Sin credenciales persistidas ni storageState real.
    storageState: undefined,
    // No guardar trazas automáticamente para evitar artefactos sensibles.
    trace: 'off',
    screenshot: 'only-on-failure',
    video: 'off',
    // Navegar al baseURL antes de cada test para estado limpio.
    navigationTimeout: 15000,
    actionTimeout: 8000,
  },
  projects: [
    // Escritorio: 1440×900
    {
      name: 'desktop',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
      },
    },
    // Viewport intermedio: 1024×768
    {
      name: 'intermediate',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1024, height: 768 },
      },
    },
    // Viewport estrecho: 390×844 (móvil)
    {
      name: 'narrow',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 390, height: 844 },
        isMobile: true,
      },
    },
  ],
  // Servidor de desarrollo local para los tests; no usar --host.
  webServer: {
    command: 'npm run dev -- --port 5173',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: true,
    timeout: 60000,
  },
})
