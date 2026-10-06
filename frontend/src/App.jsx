import React from 'react'
import { BrowserRouter, Navigate, useRoutes } from 'react-router-dom'
import { SessionProvider, useSession } from './auth/SessionProvider.jsx'
import { can } from './auth/permissions.js'
import { LoginPage } from './auth/LoginPage.jsx'
import { buildRegistry } from './routing/registry.jsx'
import { AppShell } from './components/layout.jsx'
import { EmptyState, ErrorState, Loading } from './components/feedback.jsx'
import { ApiError } from './api/errors.js'

export function Guard({ capability, children }) {
  const { status, principal } = useSession()
  if (status === 'restoring') return <Loading label="Comprobando sesión…" />
  if (status !== 'authenticated') return <Navigate to="/login" replace />
  if (capability && !can(principal, capability)) return <ErrorState error={new ApiError(403)} />
  return children
}
const registry = buildRegistry()
function protectedRoutes(routes) { return routes.map(({ capability, children, ...route }) => ({ ...route, element: <Guard capability={capability}>{route.element}</Guard>, children: children && protectedRoutes(children) })) }
function hasRootRoute(routes, parent = '') {
  return routes.some((route) => {
    const path = route.index ? parent || '/' : route.path?.startsWith('/') ? route.path : `${parent}/${route.path || ''}`.replace(/\/+/g, '/')
    return (path === '/' && (route.index || route.path !== undefined)) || (route.children && hasRootRoute(route.children, path))
  })
}
export function AppRoutes({ manifest = registry }) {
  const { principal } = useSession()
  const destination = manifest.navigation.find((item) => can(principal, item.capability))?.path || '/'
  const children = protectedRoutes(manifest.routes)
  if (!hasRootRoute(manifest.routes)) children.push({ index: true, element: <EmptyState title="Sin funciones disponibles" message="No hay funciones disponibles para tu sesión en este momento." /> })
  children.push({ path: '*', element: <EmptyState title="Página no disponible" message="Selecciona una opción disponible en la navegación." /> })
  return useRoutes([
    { path: '/login', element: <LoginPage destination={destination} /> },
    { path: '/', element: <Guard><AppShell navigation={manifest.navigation} /></Guard>, children },
  ])
}
export default function App({ manager, manifest }) { return <SessionProvider manager={manager}><BrowserRouter><AppRoutes manifest={manifest} /></BrowserRouter></SessionProvider> }
