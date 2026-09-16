import { useCallback, useEffect, useState } from 'react'

/**
 * Estructura mínima de arranque del frontend.
 *
 * Esta vista no implementa funcionalidad de negocio. Su única finalidad es
 * comprobar que el contenedor construye, se sirve correctamente y puede alcanzar
 * al backend a través de la red de contenedores.
 *
 * La ruta de salud del backend es una decisión de diseño de este servicio; su
 * URL se inyecta en tiempo de build mediante `VITE_BACKEND_HEALTH_URL`.
 */

const BACKEND_HEALTH_URL =
  import.meta.env.VITE_BACKEND_HEALTH_URL || 'http://localhost:8000/health'

export default function App() {
  const [backend, setBackend] = useState({ state: 'unknown', detail: '' })

  const probe = useCallback(async () => {
    setBackend({ state: 'checking', detail: '' })
    try {
      const response = await fetch(BACKEND_HEALTH_URL, { mode: 'cors' })
      if (!response.ok) {
        setBackend({ state: 'error', detail: `HTTP ${response.status}` })
        return
      }
      const body = await response.json()
      setBackend({ state: 'ok', detail: body.status || 'ok' })
    } catch (error) {
      setBackend({ state: 'unavailable', detail: error.name || 'sin conexión' })
    }
  }, [])

  useEffect(() => {
    probe()
  }, [probe])

  return (
    <main className="shell">
      <header className="shell__header">
        <h1>Dashboard de Indicadores de Riesgo Legal</h1>
        <p className="badge">Estado del entorno de desarrollo</p>
      </header>

      <section className="card">
        <h2>Servicios</h2>
        <table className="status">
          <caption className="visually-hidden">Estado de los servicios</caption>
          <thead>
            <tr>
              <th scope="col">Servicio</th>
              <th scope="col">Estado</th>
              <th scope="col">Detalle</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">frontend</th>
              <td>
                <span className="pill pill--ok">servido</span>
              </td>
              <td>Esta página fue construida y servida por el contenedor.</td>
            </tr>
            <tr>
              <th scope="row">backend</th>
              <td>
                <span className={`pill pill--${backend.state}`}>{backend.state}</span>
              </td>
              <td data-testid="backend-detail">{backend.detail || '—'}</td>
            </tr>
          </tbody>
        </table>
        <button type="button" className="action" onClick={probe}>
          Reintentar comprobación
        </button>
      </section>

      <section className="card card--note">
        <h2>Alcance</h2>
        <p>
          Esta aplicación proporciona únicamente la estructura mínima de arranque.
          No incluye ingesta de datos, procesamiento documental, cálculo de
          indicadores, análisis de tendencias, búsqueda semántica, control de
          acceso ni auditoría.
        </p>
      </section>
    </main>
  )
}
