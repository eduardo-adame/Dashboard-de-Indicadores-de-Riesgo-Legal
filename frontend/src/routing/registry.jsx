import React from 'react'
import { KNOWN_CAPABILITIES } from '../auth/permissions.js'

const discovered = import.meta.glob('../features/*/routes.jsx', { eager: true })
export function isInternalPath(path) { return typeof path === 'string' && /^\/(?!\/)[A-Za-z0-9/_:-]*$/.test(path) && !path.split('/').some((part) => part === '.' || part === '..') }
const known = (capability) => KNOWN_CAPABILITIES.includes(capability)
const fail = () => { throw new Error('El registro de navegación no es válido.') }
export function buildRegistry(modules = discovered) {
  const navigation = []; const routes = []; const ids = new Set(); const paths = new Set(); const routePaths = new Set(); const endpointPaths = new Set(); const routeIds = new Set()
  for (const [, module] of Object.entries(modules).sort(([a], [b]) => a.localeCompare(b))) {
    if (!Array.isArray(module.routes) || !Array.isArray(module.navigation)) fail()
    for (const item of module.navigation) {
      if (!item || typeof item.id !== 'string' || !item.id || !isInternalPath(item.path) || ['/login', '/sin-autorizacion'].includes(item.path) || /:/.test(item.path) || !known(item.capability) || typeof item.label !== 'string' || !item.label || !Number.isFinite(item.order) || ids.has(item.id) || paths.has(item.path)) fail()
      ids.add(item.id); paths.add(item.path); navigation.push(Object.freeze({ id: item.id, path: item.path, label: item.label, capability: item.capability, order: item.order }))
    }
    function normalize(route, parent = '', inherited) {
      if (!route || !React.isValidElement(route.element) || (route.index && (route.path !== undefined || route.children))) fail()
      const path = route.index ? parent || '/' : route.path?.startsWith('/') ? route.path : `${parent}/${route.path || ''}`.replace(/\/+/g, '/')
      const concrete = !route.index && route.path !== undefined
      const endpoint = (concrete || route.index) && !route.children?.length
      if (!isInternalPath(path) || ['/login', '/sin-autorizacion'].includes(path) || (concrete && routePaths.has(path)) || (endpoint && endpointPaths.has(path)) || (route.id && routeIds.has(route.id))) fail()
      if (concrete || route.index) routePaths.add(path)
      if (endpoint) endpointPaths.add(path)
      if (route.id) routeIds.add(route.id)
      const nav = (concrete || route.index) && module.navigation.find((item) => item.path === path)
      const capability = route.capability || nav?.capability || inherited
      if ((route.capability && !known(route.capability)) || (nav && capability !== nav.capability) || (!capability && !route.children?.length)) fail()
      return { id: route.id, path: route.path, index: route.index, element: route.element, capability, children: route.children?.map((child) => normalize(child, path, capability)) }
    }
    routes.push(...module.routes.map((route) => normalize(route)))
  }
  if (navigation.some((item) => !routePaths.has(item.path))) fail()
  navigation.sort((a, b) => a.order - b.order || a.id.localeCompare(b.id))
  return Object.freeze({ routes: Object.freeze(routes), navigation: Object.freeze(navigation) })
}
