import React, { createContext, useContext, useEffect, useState } from 'react'
import { createApiClient } from '../api/client.js'
import { ApiError } from '../api/errors.js'
import { validatePrincipal } from './permissions.js'

export function createSessionManager(fetchImpl) {
  let token = null
  let generation = 0
  let snapshot = { status: 'restoring', principal: null, generation }
  let restoring = null
  let forbidden = null
  let closing = null
  const listeners = new Set()
  const emit = (status, principal = null, sessionError = null) => {
    snapshot = { status, principal, generation, sessionError }
    for (const listener of listeners) listener(snapshot)
  }
  const invalidate = () => { generation++; token = null; client.abortAll(); emit('anonymous') }
  function access(data) {
    if (!data || typeof data.access_token !== 'string' || !data.access_token || data.token_type?.toLowerCase() !== 'bearer') throw new ApiError(503)
    return data.access_token
  }
  async function identity(expected, recoverSession = false, reconcile = false) {
    const previousPrincipal = snapshot.principal
    // La propia revalidación no puede iniciar otra revalidación recursiva.
    const principal = validatePrincipal(await client.request('/auth/me', { recoverSession, revalidateOnForbidden: false }))
    if (expected !== generation) throw new ApiError(0, { cancelled: true })
    if (!principal) throw new ApiError(503)
    // Una denegación documental no debe reiniciar indefinidamente la misma lectura.
    if (reconcile && JSON.stringify(principal) !== JSON.stringify(previousPrincipal)) {
      generation++; client.abortAll()
    }
    emit('authenticated', principal)
    return true
  }
  async function renew() {
    const expected = generation
    try {
      const data = await client.request('/auth/refresh', { method: 'POST', anonymous: true })
      if (generation !== expected) return false
      token = access(data)
      return await identity(expected)
    } catch {
      if (generation === expected) invalidate()
      return false
    }
  }
  async function revalidate() {
    if (forbidden) return forbidden
    const expected = generation
    forbidden = identity(expected, true, true).catch(() => { if (generation === expected) invalidate(); return false }).finally(() => { forbidden = null })
    return forbidden
  }
  const client = createApiClient({ fetchImpl, getSession: () => ({ token, generation }), recover: renew, onUnauthorized: invalidate, onForbidden: revalidate })
  return {
    client, getSnapshot: () => snapshot,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    restore() { if (!restoring) restoring = renew(); return restoring }, revalidate, invalidate,
    async login(username, password) {
      if (closing) await closing.catch(() => {})
      invalidate(); emit('restoring')
      const expected = generation
      try {
        const data = await client.request('/auth/login', { method: 'POST', anonymous: true, body: { username, password } })
        if (expected !== generation) throw new ApiError(0, { cancelled: true })
        token = access(data)
        await identity(expected)
      } catch (error) { if (expected === generation) invalidate(); throw error }
    },
    async logout() {
      if (closing) return closing
      let accessToken = token
      invalidate()
      const expected = generation
      closing = (async () => {
        try {
          if (!accessToken) return
          try { await client.request('/auth/logout', { method: 'POST', anonymous: true, accessToken }) }
          catch (error) {
            if (error.status !== 401 || expected !== generation) throw error
            // Solo un rechazo401 confirmado permite renovar y repetir el cierre.
            const data = await client.request('/auth/refresh', { method: 'POST', anonymous: true })
            if (expected !== generation) throw new ApiError(0, { cancelled: true })
            accessToken = access(data)
            await client.request('/auth/logout', { method: 'POST', anonymous: true, accessToken })
          }
        } catch (error) {
          if (expected === generation) emit('anonymous', null, error)
          throw error
        } finally { accessToken = null }
      })().finally(() => { closing = null })
      return closing
    },
  }
}

const SessionContext = createContext(null)
export function SessionProvider({ children, manager: provided }) {
  const [manager] = useState(() => provided || createSessionManager())
  const [snapshot, setSnapshot] = useState(manager.getSnapshot)
  useEffect(() => {
    const unsubscribe = manager.subscribe(setSnapshot)
    setSnapshot(manager.getSnapshot()); manager.restore()
    return unsubscribe
  }, [manager])
  useEffect(() => {
    const verify = () => { if (document.visibilityState === 'visible' && manager.getSnapshot().status === 'authenticated') manager.revalidate() }
    window.addEventListener('focus', verify); document.addEventListener('visibilitychange', verify)
    return () => { window.removeEventListener('focus', verify); document.removeEventListener('visibilitychange', verify) }
  }, [manager])
  return <SessionContext.Provider value={{ ...snapshot, ...manager }}>{children}</SessionContext.Provider>
}
export function useSession() {
  const session = useContext(SessionContext)
  if (!session) throw new Error('La vista requiere un contexto de sesión.')
  return session
}
