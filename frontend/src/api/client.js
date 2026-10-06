import { ApiError } from './errors.js'

/** Transporte único: no conserva cuerpos y nunca repite una mutación incierta. */
export function createApiClient({ fetchImpl = (...args) => fetch(...args), getSession = () => ({ token: null, generation: 0 }), recover = async () => false, onForbidden = async () => {}, onUnauthorized = () => {} } = {}) {
  const controllers = new Set()
  const reads = new Map()
  let recovery = null
  function abortAll() {
    for (const controller of controllers) controller.abort()
    reads.clear()
  }
  async function refreshOnce() {
    if (!recovery) recovery = Promise.resolve().then(recover).finally(() => { recovery = null })
    return recovery
  }
  async function request(path, { method = 'GET', body, signal, headers = {}, anonymous = false, recoverSession = true, revalidateOnForbidden = true, accessToken, timeout = 420000, acceptStatuses = [] } = {}) {
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || /[\r\n\\#]/.test(path) || !new URL(`/api${path}`, 'http://localhost').pathname.startsWith('/api/')) throw new ApiError(422)
    const initial = getSession()
    const controller = new AbortController()
    controllers.add(controller)
    const abort = () => controller.abort()
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) controller.abort()
    const timer = setTimeout(abort, timeout)
    const mutation = !['GET', 'HEAD'].includes(method.toUpperCase())
    try {
      for (let attempt = 0; attempt < 2; attempt++) {
        if (controller.signal.aborted || (!anonymous && initial.generation !== getSession().generation)) throw new ApiError(0, { cancelled: true })
        const token = accessToken ?? getSession().token
        if (!anonymous && !token) throw new ApiError(401)
        const outgoing = new Headers(headers)
        outgoing.delete('Authorization')
        if (token && (!anonymous || accessToken)) outgoing.set('Authorization', `Bearer ${token}`)
        const multipart = typeof FormData !== 'undefined' && body instanceof FormData
        if (body !== undefined && !multipart) outgoing.set('Content-Type', 'application/json')
        const response = await fetchImpl(`/api${path}`, {
          method, headers: outgoing, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          signal: controller.signal, body: body === undefined ? undefined : multipart ? body : JSON.stringify(body),
        })
        if (!anonymous && initial.generation !== getSession().generation) throw new ApiError(0, { cancelled: true })
        if (response.status === 401 && !anonymous && recoverSession && attempt === 0) {
          const recovered = await refreshOnce()
          if (controller.signal.aborted || initial.generation !== getSession().generation) throw new ApiError(0, { cancelled: true })
          if (recovered) continue
          onUnauthorized()
          throw new ApiError(401)
        }
        if (response.status === 401 && !anonymous) onUnauthorized()
        if (response.status === 403 && !anonymous) { if (revalidateOnForbidden) await onForbidden(); throw new ApiError(403) }
        if (!response.ok && !acceptStatuses.includes(response.status)) throw new ApiError(response.status)
        if (response.status === 204) return null
        let data
        try { data = await response.json() } catch { throw new ApiError(503) }
        if (controller.signal.aborted || (!anonymous && initial.generation !== getSession().generation)) throw new ApiError(0, { cancelled: true })
        return data
      }
      throw new ApiError(401)
    } catch (error) {
      if (error instanceof ApiError) throw error
      if (signal?.aborted || (!anonymous && initial.generation !== getSession().generation)) throw new ApiError(0, { cancelled: true })
      throw new ApiError(0, { uncertain: mutation })
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      controllers.delete(controller)
    }
  }
  // Solo se comparten lecturas en curso; al terminar se elimina toda referencia.
  function acquireRead(path) {
    const key = `${getSession().generation}:${path}`
    let entry = reads.get(key)
    if (!entry) {
      const controller = new AbortController()
      entry = { controller, subscribers: 0, abortTimer: null, promise: null }
      entry.promise = request(path, { signal: controller.signal }).finally(() => { if (reads.get(key) === entry) reads.delete(key) })
      reads.set(key, entry)
    }
    clearTimeout(entry.abortTimer)
    entry.subscribers++
    let released = false
    return { promise: entry.promise, release() {
      if (released) return
      released = true
      entry.subscribers--
      // StrictMode puede volver a adquirir la misma lectura antes de cancelar.
      if (!entry.subscribers) entry.abortTimer = setTimeout(() => { if (!entry.subscribers) entry.controller.abort() }, 0)
    } }
  }
  return { request, acquireRead, abortAll }
}
