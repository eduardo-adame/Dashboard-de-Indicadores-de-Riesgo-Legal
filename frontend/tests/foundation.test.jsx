import React, { StrictMode, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter, Link, Navigate, Outlet, Route, Routes, useSearchParams } from 'react-router-dom'
import { buildRegistry } from '../src/routing/registry.jsx'
import App, { AppRoutes } from '../src/App.jsx'
import { SessionProvider, createSessionManager } from '../src/auth/SessionProvider.jsx'
import { createApiClient } from '../src/api/client.js'
import { useResource } from '../src/api/useResource.js'
import { Modal } from '../src/components/layout.jsx'
import { Button, Input, Select, Tabs } from '../src/components/controls.jsx'
import { Badge, DataTable, Delta, KpiMetric, KpiRow } from '../src/components/data.jsx'
import { Icon } from '../src/components/Icon.jsx'
import { ErrorState, ResourceState } from '../src/components/feedback.jsx'
import { ApiError } from '../src/api/errors.js'

function ConsultaDeCompatibilidad() {
  const [params] = useSearchParams()
  return <h1>Consulta {params.get('vista')}</h1>
}

describe('ROBUSTNESS: compatibilidad del entorno Foundation', () => {
  it('ejecuta JSX de React con Testing Library y jsdom', () => {
    render(<button type="button">Comprobar entorno</button>)
    expect(screen.getByRole('button', { name: 'Comprobar entorno' })).toBeVisible()
    expect(document.defaultView).toBe(window)
  })

  it('navega con el router declarativo sin loaders ni actions', async () => {
    window.history.replaceState(null, '', '/')
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Link to="/consulta?vista=actual">Abrir consulta</Link>} />
          <Route path="/consulta" element={<ConsultaDeCompatibilidad />} />
        </Routes>
      </BrowserRouter>,
    )
    await userEvent.click(screen.getByRole('link', { name: 'Abrir consulta' }))
    expect(screen.getByRole('heading', { name: 'Consulta actual' })).toBeVisible()
    expect(window.location.pathname).toBe('/consulta')
  })

  it('conserva Navigate y Outlet en rutas declarativas anidadas', async () => {
    window.history.replaceState(null, '', '/inicio')
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/inicio" element={<Navigate to="/base" replace />} />
          <Route path="/base" element={<main><Outlet /></main>}>
            <Route index element={<h1>Base de navegación</h1>} />
          </Route>
        </Routes>
      </BrowserRouter>,
    )
    expect(await screen.findByRole('heading', { name: 'Base de navegación' })).toBeVisible()
    expect(window.location.pathname).toBe('/base')
  })
})

const identity = { id: '11111111-1111-4111-8111-111111111111', username: 'cuenta-sintetica', roles: ['JURIDICO'] }
const ok = (data) => ({ ok: true, status: 200, json: async () => data })
function managerFixture(status = 'authenticated', roles = identity.roles, client = {}) {
  const snapshot = { status, principal: status === 'authenticated' ? { ...identity, roles } : null, generation: 1 }
  return { getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn(), logout: vi.fn(), client }
}
const feature = (id, path, capability = 'dashboard.read', order = 1) => ({ routes: [{ path, element: <h1>{id}</h1> }], navigation: [{ id, path, label: id, capability, order }] })
describe('SRS_REQUIRED: sesión y rutas protegidas', () => {
  it('no renderiza contenido protegido antes de restaurar sesión', () => {
    window.history.replaceState(null, '', '/reservado')
    render(<App manager={managerFixture('restoring')} manifest={buildRegistry({ feature: feature('Contenido protegido', '/reservado') })} />)
    expect(screen.getByText('Comprobando sesión…')).toBeVisible(); expect(screen.queryByText('Contenido protegido')).not.toBeInTheDocument()
  })
  it('acceso directo anónimo solicita login sin mostrar datos protegidos', async () => {
    window.history.replaceState(null, '', '/reservado')
    render(<App manager={managerFixture('anonymous')} manifest={buildRegistry({ feature: feature('Contenido protegido', '/reservado') })} />)
    expect(await screen.findByRole('heading', { name: 'Iniciar sesión' })).toBeVisible(); expect(screen.queryByText('Contenido protegido')).not.toBeInTheDocument()
  })
  it('oculta navegación y bloquea ruta para capability ausente', () => {
    window.history.replaceState(null, '', '/administracion')
    render(<App manager={managerFixture()} manifest={buildRegistry({ feature: feature('Administrar', '/administracion', 'role.assign') })} />)
    expect(screen.getByRole('heading', { name: 'Sin autorización' })).toBeVisible(); expect(screen.queryByRole('link', { name: 'Administrar' })).not.toBeInTheDocument(); expect(screen.queryByRole('heading', { name: 'Administrar' })).not.toBeInTheDocument()
  })
  it('la unión de roles permite una ruta registrada', () => {
    window.history.replaceState(null, '', '/cuarentena')
    render(<App manager={managerFixture('authenticated', ['JURIDICO', 'ANALISTA'])} manifest={buildRegistry({ feature: feature('Revisar', '/cuarentena', 'quarantine.read') })} />)
    expect(screen.getByRole('heading', { name: 'Revisar' })).toBeVisible(); expect(screen.getByRole('link', { name: 'Revisar' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Revisar' }).querySelector('ion-icon')).toHaveStyle({ fontSize: '16px' })
  })
  it('login borra la contraseña y no muestra detalles internos ante rechazo', async () => {
    window.history.replaceState(null, '', '/login')
    const manager = createSessionManager(async () => ({ ok: false, status: 401, json: async () => ({ detail: 'Cuenta interna reservada' }) }))
    render(<App manager={manager} manifest={buildRegistry({})} />)
    const password = await screen.findByLabelText('Contraseña')
    await userEvent.type(screen.getByLabelText('Usuario'), 'cuenta-sintetica'); await userEvent.type(password, 'clave-sintetica'); await userEvent.click(screen.getByRole('button', { name: 'Iniciar sesión' }))
    expect(await screen.findByRole('alert')).not.toHaveTextContent('Cuenta interna reservada'); expect(password).toHaveValue('')
  })
  it('StrictMode restaura solo una vez y no ofrece pantallas de negocio ausentes', async () => {
    window.history.replaceState(null, '', '/')
    const fetchImpl = vi.fn(async (path) => ok(path.endsWith('/refresh') ? { access_token: 'sintetico', token_type: 'bearer' } : identity))
    render(<StrictMode><App manager={createSessionManager(fetchImpl)} manifest={buildRegistry({})} /></StrictMode>)
    expect(await screen.findByRole('heading', { name: 'Sin funciones disponibles' })).toBeVisible(); expect(fetchImpl).toHaveBeenCalledTimes(2); expect(screen.queryByRole('link', { name: 'Consulta documental' })).not.toBeInTheDocument()
  })
})
describe('ROBUSTNESS: registro modular determinista', () => {
  it('cero módulos no crea rutas ni navegación', () => { expect(buildRegistry({})).toEqual({ routes: [], navigation: [] }) })
  it('ordena módulos presentes por order e id, no por orden de descubrimiento', () => {
    const registry = buildRegistry({ w3: feature('operaciones', '/ingesta', 'ingest.execute', 2), w1: feature('analitica', '/', 'dashboard.read', 1), w2: feature('consulta', '/consulta', 'document.query', 2) })
    expect(registry.navigation.map((item) => item.id)).toEqual(['analitica', 'consulta', 'operaciones']); expect(registry.routes).toHaveLength(3)
  })
  it('un layout sin path comparte raíz con el resumen sin colisión ni perder guards', () => {
    const registry = buildRegistry({ w1: feature('resumen', '/'), w2: { routes: [{ element: <Outlet />, children: [{ path: '/consulta', element: <h1>Consulta</h1> }] }], navigation: [{ id: 'consulta', path: '/consulta', label: 'Consulta', capability: 'document.query', order: 2 }] } })
    expect(registry.routes).toHaveLength(2); expect(registry.routes[1].path).toBeUndefined(); expect(registry.routes[1].children[0].capability).toBe('document.query')
  })
  it('rechaza índices duplicados bajo layouts sin path', () => {
    const module = { routes: [{ element: <Outlet />, children: [{ index: true, capability: 'dashboard.read', element: <h1>Resumen</h1> }] }], navigation: [] }
    expect(() => buildRegistry({ a: module, b: module })).toThrow('registro')
    expect(() => buildRegistry({ a: feature('resumen', '/'), b: module })).toThrow('registro')
  })
  it.each([false, true])('una raíz anidada no queda oculta por el fallback, index=%s', (index) => {
    window.history.replaceState(null, '', '/')
    const manifest = buildRegistry({ a: { routes: [{ element: <Outlet />, children: [{ ...(index ? { index: true } : { path: '/' }), capability: 'dashboard.read', element: <h1>Resumen registrado</h1> }] }], navigation: [] } })
    render(<App manager={managerFixture()} manifest={manifest} />)
    expect(screen.getByRole('heading', { name: 'Resumen registrado' })).toBeVisible(); expect(screen.queryByText('Sin funciones disponibles')).not.toBeInTheDocument()
  })
  it('rechaza colisiones de rutas e IDs', () => {
    expect(() => buildRegistry({ a: feature('a', '/a'), b: feature('a', '/b') })).toThrow('registro')
    expect(() => buildRegistry({ a: feature('a', '/a'), b: feature('b', '/a') })).toThrow('registro')
  })
  it('rechaza destinos externos, capabilities desconocidas y navegación sin ruta', () => {
    expect(() => buildRegistry({ a: feature('a', '//externo.invalid') })).toThrow()
    expect(() => buildRegistry({ a: feature('a', '/a', 'security.bypass') })).toThrow()
    expect(() => buildRegistry({ a: { ...feature('a', '/a'), routes: [{ path: '/b', capability: 'dashboard.read', element: <div /> }] } })).toThrow()
  })
  it('no acepta rutas hoja sin permiso ni colisión con login', () => {
    expect(() => buildRegistry({ a: { routes: [{ path: '/a', element: <div /> }], navigation: [] } })).toThrow()
    expect(() => buildRegistry({ a: feature('login', '/login') })).toThrow()
  })
})
describe('SRS_REQUIRED: componentes accesibles y datos fieles', () => {
  it('mantiene cero, ausencia y No disponible separados en la tabla', () => {
    render(<DataTable caption="Datos autorizados" columns={[{ key: 'value', label: 'Valor' }]} rows={[{ id: 'a', value: 0 }, { id: 'b', value: null }, { id: 'c', value: 'No disponible' }]} />)
    expect(screen.getByRole('cell', { name: '0' })).toBeVisible(); expect(screen.getByRole('cell', { name: '—' })).toBeVisible(); expect(screen.getByRole('cell', { name: 'No disponible' })).toBeVisible()
  })
  it('renderiza texto no confiable sin interpretarlo como HTML', () => { render(<DataTable caption="Datos" columns={[{ key: 'text', label: 'Texto' }]} rows={[{ id: 'a', text: '<script>no ejecutar</script>' }]} />); expect(screen.getByText('<script>no ejecutar</script>')).toBeVisible(); expect(document.querySelector('script')).toBeNull() })
  it('el valor KPI es neutral y el sentimiento no depende del signo', () => { render(<KpiRow><KpiMetric label="Valor autorizado" displayValue="0" delta={<Delta text="+10" sentiment="negative" />} /></KpiRow>); expect(screen.getByText('0')).toHaveClass('text-primary'); expect(screen.getByText('+10')).toHaveStyle({ color: 'var(--critical)' }) })
  it('vincula etiquetas/errores y deshabilita controles ocupados', () => { render(<><Input label="Dato" error="Revisa el dato" /><Select label="Periodo" options={[{ value: 'a', label: 'Actual' }]} /><Button busy>Guardar</Button></>); expect(screen.getByLabelText('Dato')).toHaveAttribute('aria-invalid', 'true'); expect(screen.getByLabelText('Periodo')).toBeVisible(); expect(screen.getByRole('button', { name: 'Guardar' })).toBeDisabled() })
  it('tabs siguen flechas, foco y selección', async () => {
    function Example() { const [active, setActive] = useState('a'); return <Tabs label="Conjuntos" items={[{ id: 'a', label: 'Primero' }, { id: 'b', label: 'Segundo' }]} active={active} onChange={setActive} /> }
    render(<Example />); screen.getByRole('tab', { name: 'Primero' }).focus(); await userEvent.keyboard('{ArrowRight}'); expect(screen.getByRole('tab', { name: 'Segundo' })).toHaveFocus(); expect(screen.getByRole('tab', { name: 'Segundo' })).toHaveAttribute('aria-selected', 'true')
  })
  it('modal y drawer atrapan foco, cierran por Escape y restauran foco/scroll', async () => {
    function Example() { const [open, setOpen] = useState(false); return <><Button onClick={() => setOpen(true)}>Abrir</Button><Modal open={open} title="Confirmar" drawer onClose={() => setOpen(false)}><Button>Último control</Button></Modal></> }
    render(<Example />); const trigger = screen.getByRole('button', { name: 'Abrir' }); await userEvent.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Confirmar' }); expect(document.body.style.overflow).toBe('hidden'); expect(within(dialog).getByRole('button', { name: 'Cerrar' })).toHaveFocus()
    await userEvent.keyboard('{Shift>}{Tab}{/Shift}'); expect(within(dialog).getByRole('button', { name: 'Último control' })).toHaveFocus(); await userEvent.tab(); expect(within(dialog).getByRole('button', { name: 'Cerrar' })).toHaveFocus()
    await userEvent.keyboard('{Escape}'); expect(screen.queryByRole('dialog')).not.toBeInTheDocument(); expect(trigger).toHaveFocus(); expect(document.body.style.overflow).toBe('')
  })
  it('los errores arbitrarios no exponen mensajes ni ofrecen replay de mutación incierta', () => { render(<ErrorState error={new ApiError(0, { uncertain: true })} onRetry={() => {}} />); expect(screen.queryByRole('button', { name: 'Reintentar' })).not.toBeInTheDocument(); expect(screen.getByRole('alert')).toHaveTextContent('Consulta el estado') })
  it('el wrapper de iconos no acepta URL o SVG suministrado por datos', () => { render(<Icon name="https://externo.invalid/icon.svg" label="No permitido" />); expect(screen.queryByRole('img')).not.toBeInTheDocument() })
  it('los estados loading, empty y failure no se presentan como cero', () => { const { rerender } = render(<ResourceState resource={{ state: 'loading' }}>{() => '0'}</ResourceState>); expect(screen.getByRole('status')).toBeVisible(); rerender(<ResourceState resource={{ state: 'empty' }}>{() => '0'}</ResourceState>); expect(screen.getByText('Sin resultados')).toBeVisible(); rerender(<ResourceState resource={{ state: 'unrecoverable_error', error: new ApiError(403) }}>{() => '0'}</ResourceState>); expect(screen.getByRole('alert')).toHaveTextContent('Sin autorización') })
})
describe('ROBUSTNESS: lecturas protegidas y ciclo React', () => {
  it('un403 documental con identidad vigente queda como error sin bucle de recuperación', async () => {
    window.history.replaceState(null, '', '/reservado')
    const fetchImpl = vi.fn(async (path) => path === '/api/reservado' ? { status: 403, ok: false } : ok(path.endsWith('/refresh') ? { access_token: 'sintetico', token_type: 'bearer' } : identity))
    const manager = createSessionManager(fetchImpl)
    function Probe() { const result = useResource('/reservado'); return <ResourceState resource={result}>{() => 'Contenido restringido'}</ResourceState> }
    render(<App manager={manager} manifest={buildRegistry({ a: { ...feature('Reservado', '/reservado'), routes: [{ path: '/reservado', element: <Probe /> }] } })} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Sin autorización')
    await new Promise((done) => setTimeout(done, 30))
    expect(fetchImpl.mock.calls.filter(([path]) => path === '/api/reservado')).toHaveLength(1); expect(manager.getSnapshot().status).toBe('authenticated')
  })
  it('el fallo de logout sigue visible en login después de desmontar el shell', async () => {
    window.history.replaceState(null, '', '/')
    const manager = createSessionManager(async (path) => { if (path.endsWith('/logout')) throw new Error('Detalle interno'); return ok(path.endsWith('/refresh') ? { access_token: 'sintetico', token_type: 'bearer' } : identity) })
    render(<App manager={manager} manifest={buildRegistry({})} />)
    await screen.findByRole('heading', { name: 'Sin funciones disponibles' }); await userEvent.click(screen.getAllByRole('button', { name: 'Cerrar sesión' })[0])
    expect(await screen.findByRole('heading', { name: 'Iniciar sesión' })).toBeVisible(); expect(await screen.findByRole('alert')).toHaveTextContent('Consulta el estado'); expect(screen.getByRole('alert')).not.toHaveTextContent('Detalle interno')
  })
  it('StrictMode comparte lectura y descarta datos al cambiar query', async () => {
    const resolvers = []
    const fetchImpl = vi.fn(() => new Promise((resolve) => resolvers.push(resolve)))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 1 }) })
    const manager = managerFixture('authenticated', identity.roles, client)
    function Probe({ path }) { const result = useResource(path); return <ResourceState resource={result}>{(data) => <p>{data.value}</p>}</ResourceState> }
    const view = (path) => <StrictMode><SessionProvider manager={manager}><Probe path={path} /></SessionProvider></StrictMode>
    const { rerender } = render(view('/a'))
    expect(fetchImpl).toHaveBeenCalledTimes(1); resolvers[0](ok({ value: 'Dato anterior' })); await screen.findByText('Dato anterior')
    rerender(view('/b')); expect(screen.queryByText('Dato anterior')).not.toBeInTheDocument(); expect(screen.getByRole('status')).toBeVisible()
    resolvers[1](ok({ value: 'Dato vigente' })); expect(await screen.findByText('Dato vigente')).toBeVisible()
  })
})
