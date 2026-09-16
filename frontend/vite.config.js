import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Configuración de build del frontend.
//
// No se configuran proxies de API: las rutas del backend todavía no forman un
// contrato estable, y un proxy apuntando a rutas provisionales se convertiría en
// deuda técnica silenciosa.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 3000,
    strictPort: false,
  },
  preview: {
    host: true,
    port: 3000,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // El bundle inicial no incluye librerías de visualización de datos.
    chunkSizeWarningLimit: 600,
  },
})
