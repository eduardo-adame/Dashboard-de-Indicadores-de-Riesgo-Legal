import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApiClient } from '../src/api/client.js'
import { ApiError, safeError } from '../src/api/errors.js'
import { createSessionManager } from '../src/auth/SessionProvider.jsx'

const principal = { id: '11111111-1111-4111-8111-111111111111', username: 'cuenta-sintetica', roles: ['ANALISTA'] }
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data })
const token = { access_token: 'acceso-sintetico', token_type: 'bearer' }
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { resolve, promise } }
afterEach(() => vi.useRealTimers())

describe('SRS_REQUIRED: transporte y sesión', () => {
  it('conserva prefijo, bearer y cookie sin caché ni redirects', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(200, { items: [] }))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 1 }) })
    await client.request('/dashboard/kpis')
    const [path, options] = fetchImpl.mock.calls[0]
    expect(path).toBe('/api/dashboard/kpis'); expect(options.credentials).toBe('same-origin'); expect(options.cache).toBe('no-store'); expect(options.redirect).toBe('error'); expect(options.headers.get('Authorization')).toBe('Bearer sintetico')
  })
  it('preserva multipart e identidad sin inventar Content-Type', async () => {
    const body = new FormData(); body.append('file', new Blob(['sintetico']), 'fixture.txt')
    const fetchImpl = vi.fn().mockResolvedValue(response(200, {}))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await client.request('/ingestion/uploads', { method: 'POST', body, headers: { 'Idempotency-Key': '11111111-1111-4111-8111-111111111111' } })
    const options = fetchImpl.mock.calls[0][1]
    expect(options.body).toBe(body); expect(options.headers.has('Content-Type')).toBe(false); expect(options.headers.get('Idempotency-Key')).toBe('11111111-1111-4111-8111-111111111111')
  })
  it('comparte una renovación para dos respuestas401 concurrentes', async () => {
    let renewed = false
    const fetchImpl = vi.fn(async () => response(renewed ? 200 : 401, { ok: true }))
    const recover = vi.fn(async () => { await Promise.resolve(); renewed = true; return true })
    const client = createApiClient({ fetchImpl, recover, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await Promise.all([client.request('/a'), client.request('/b')])
    expect(recover).toHaveBeenCalledTimes(1); expect(fetchImpl).toHaveBeenCalledTimes(4)
  })
  it('limita401 a una renovación y limpia una sesión rechazada', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(401, {})); const recover = vi.fn().mockResolvedValue(true); const invalidate = vi.fn()
    const client = createApiClient({ fetchImpl, recover, onUnauthorized: invalidate, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await expect(client.request('/a')).rejects.toMatchObject({ status: 401 })
    expect(recover).toHaveBeenCalledTimes(1); expect(fetchImpl).toHaveBeenCalledTimes(2); expect(invalidate).toHaveBeenCalledTimes(1)
  })
  it('no repite una mutación ante pérdida de respuesta', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('Detalle interno que no se presenta'))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await expect(client.request('/operacion', { method: 'POST', body: {} })).rejects.toMatchObject({ uncertain: true, status: 0 })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('un timeout de mutación es incierto y no permite replay automático', async () => {
    vi.useFakeTimers()
    const fetchImpl = vi.fn((path, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('Timeout interno')), { once: true })))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    const assertion = expect(client.request('/operacion', { method: 'POST', timeout: 10 })).rejects.toMatchObject({ uncertain: true })
    await vi.advanceTimersByTimeAsync(11); await assertion; expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('403 revalida identidad sin reintentar ni mostrar cuerpo restringido', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response(403, { detail: 'Documento restringido interno' })); const onForbidden = vi.fn()
    const client = createApiClient({ fetchImpl, onForbidden, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await expect(client.request('/a')).rejects.toMatchObject({ status: 403, message: 'Acceso no autorizado.' })
    expect(onForbidden).toHaveBeenCalledTimes(1); expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('descarta una respuesta que llega después de invalidar generación', async () => {
    const delayed = deferred(); let generation = 0
    const client = createApiClient({ fetchImpl: () => delayed.promise, getSession: () => ({ token: 'sintetico', generation }) })
    const pending = client.request('/a'); generation++; delayed.resolve(response(200, { sensitive: true }))
    await expect(pending).rejects.toMatchObject({ cancelled: true })
  })
  it('una recuperación401 fallida tardía no invalida una sesión nueva', async () => {
    const delayed = deferred(); const started = deferred(); let generation = 0; const invalidate = vi.fn()
    const client = createApiClient({ fetchImpl: async () => response(401), recover: () => { started.resolve(); return delayed.promise }, onUnauthorized: invalidate, getSession: () => ({ token: 'sintetico', generation }) })
    const pending = client.request('/a'); await started.promise; generation++; delayed.resolve(false)
    await expect(pending).rejects.toMatchObject({ cancelled: true }); expect(invalidate).not.toHaveBeenCalled(); expect(generation).toBe(1)
  })
  it('descarta respuesta cuando el caller cancela', async () => {
    const delayed = deferred(); const controller = new AbortController()
    const client = createApiClient({ fetchImpl: () => delayed.promise, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    const pending = client.request('/a', { signal: controller.signal }); controller.abort(); delayed.resolve(response(200, {}))
    await expect(pending).rejects.toMatchObject({ cancelled: true })
  })
  it('deduplica lecturas en curso sin conservar resultados', async () => {
    const delayed = deferred(); const fetchImpl = vi.fn().mockImplementationOnce(() => delayed.promise).mockResolvedValue(response(200, {}))
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'sintetico', generation: 0 }) })
    const a = client.acquireRead('/a'); const b = client.acquireRead('/a'); a.release()
    delayed.resolve(response(200, {})); await Promise.all([a.promise, b.promise]); b.release()
    const c = client.acquireRead('/a'); await c.promise; c.release(); expect(fetchImpl).toHaveBeenCalledTimes(2)
  })
  it('restaura refresh y me una sola vez y mantiene token fuera del snapshot', async () => {
    const fetchImpl = vi.fn(async (path) => response(200, path.endsWith('/refresh') ? token : principal))
    const manager = createSessionManager(fetchImpl)
    await Promise.all([manager.restore(), manager.restore()])
    expect(fetchImpl).toHaveBeenCalledTimes(2); expect(manager.getSnapshot()).toMatchObject({ status: 'authenticated', principal }); expect(manager.getSnapshot()).not.toHaveProperty('token')
  })
  it('login utiliza contrato real y logout limpia antes de recibir204', async () => {
    const delayed = deferred()
    const fetchImpl = vi.fn(async (path) => path.endsWith('/logout') ? delayed.promise : response(200, path.endsWith('/login') ? token : principal))
    const manager = createSessionManager(fetchImpl); await manager.login('cuenta-sintetica', 'clave-sintetica')
    const pending = manager.logout(); expect(manager.getSnapshot().status).toBe('anonymous'); delayed.resolve(response(204)); await pending
    expect(fetchImpl.mock.calls[0][1].body).toBe(JSON.stringify({ username: 'cuenta-sintetica', password: 'clave-sintetica' }))
    expect(fetchImpl.mock.calls.at(-1)[1].headers.get('Authorization')).toBe('Bearer acceso-sintetico')
  })
  it('no restaura identidad tras logout con respuesta tardía de refresh', async () => {
    const delayed = deferred(); const fetchImpl = vi.fn(() => delayed.promise)
    const manager = createSessionManager(fetchImpl); const pending = manager.restore(); manager.invalidate(); delayed.resolve(response(200, token)); await pending
    expect(manager.getSnapshot().status).toBe('anonymous'); expect(fetchImpl).toHaveBeenCalledTimes(1)
  })
  it('identidad403 en revalidación no recurre ni bloquea el cliente', async () => {
    let denied = false
    const fetchImpl = vi.fn(async (path) => response(denied ? 403 : 200, path.endsWith('/refresh') ? token : principal))
    const manager = createSessionManager(fetchImpl); await manager.restore(); denied = true
    expect(await manager.revalidate()).toBe(false); expect(manager.getSnapshot().status).toBe('anonymous'); expect(fetchImpl).toHaveBeenCalledTimes(3)
  })
  it('revalida al recuperar un access vencido con refresh vigente', async () => {
    let expired = false
    const fetchImpl = vi.fn(async (path) => {
      if (path.endsWith('/refresh')) { expired = false; return response(200, token) }
      return response(expired ? 401 : 200, principal)
    })
    const manager = createSessionManager(fetchImpl); await manager.restore(); expired = true
    expect(await manager.revalidate()).toBe(true); expect(manager.getSnapshot().status).toBe('authenticated')
    expect(fetchImpl.mock.calls.filter(([path]) => path.endsWith('/refresh'))).toHaveLength(2)
  })
  it('logout con access vencido revoca refresh sin restaurar contenido protegido', async () => {
    let expired = false; let refreshValid = true; const states = []
    const fetchImpl = vi.fn(async (path) => {
      if (path.endsWith('/refresh')) { if (!refreshValid) return response(401); expired = false; return response(200, token) }
      if (path.endsWith('/logout')) { if (expired) return response(401); refreshValid = false; return response(204) }
      return response(200, principal)
    })
    const manager = createSessionManager(fetchImpl); await manager.restore(); manager.subscribe((state) => states.push(state.status)); expired = true
    await manager.logout(); expect(refreshValid).toBe(false); expect(states.every((state) => state === 'anonymous')).toBe(true)
    const fresh = createSessionManager(fetchImpl); await fresh.restore(); expect(fresh.getSnapshot().status).toBe('anonymous')
    expect(fetchImpl.mock.calls.filter(([path]) => path.endsWith('/logout'))).toHaveLength(2)
  })
  it('un cierre pendiente serializa un nuevo login y no borra su cookie', async () => {
    const delayed = deferred(); const calls = []
    const fetchImpl = vi.fn(async (path) => { calls.push(path); return path.endsWith('/logout') ? delayed.promise : response(200, path.endsWith('/login') || path.endsWith('/refresh') ? token : principal) })
    const manager = createSessionManager(fetchImpl); await manager.restore(); const logout = manager.logout(); const login = manager.login('cuenta-sintetica', 'clave-sintetica')
    expect(calls.some((path) => path.endsWith('/login'))).toBe(false); delayed.resolve(response(204)); await Promise.all([logout, login])
    expect(manager.getSnapshot().status).toBe('authenticated'); expect(calls.indexOf('/api/auth/logout')).toBeLessThan(calls.indexOf('/api/auth/login'))
  })
  it('no repite un logout incierto y conserva su error seguro en estado anónimo', async () => {
    const fetchImpl = vi.fn(async (path) => { if (path.endsWith('/logout')) throw new Error('Detalle interno'); return response(200, path.endsWith('/refresh') ? token : principal) })
    const manager = createSessionManager(fetchImpl); await manager.restore(); await expect(manager.logout()).rejects.toMatchObject({ uncertain: true })
    expect(manager.getSnapshot()).toMatchObject({ status: 'anonymous', sessionError: { uncertain: true } }); expect(fetchImpl.mock.calls.filter(([path]) => path.endsWith('/logout'))).toHaveLength(1)
  })
  it('revalidación con roles diferentes cancela los datos de la generación anterior', async () => {
    let changed = false
    const manager = createSessionManager(async (path) => response(200, path.endsWith('/refresh') ? token : changed ? { ...principal, roles: ['TI'] } : principal))
    await manager.restore(); const before = manager.getSnapshot().generation; changed = true; await manager.revalidate()
    expect(manager.getSnapshot().generation).toBe(before + 1); expect(manager.getSnapshot().principal.roles).toEqual(['TI'])
  })
  it('una identidad DTO inválida falla cerrado', async () => {
    const manager = createSessionManager(async (path) => response(200, path.endsWith('/refresh') ? token : { username: 'sintetico', roles: ['TI'] }))
    await manager.restore(); expect(manager.getSnapshot().status).toBe('anonymous')
  })
})
describe('ROBUSTNESS: errores y límites del cliente', () => {
  it.each([404, 409, 422, 503])('sanitiza el cuerpo del error%s', async (status) => {
    const client = createApiClient({ fetchImpl: async () => response(status, { detail: 'SQL y metadatos no autorizados' }), getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await expect(client.request('/a')).rejects.toMatchObject({ status }); expect(new ApiError(status).message).not.toContain('SQL')
  })
  it.each(['https://externo.invalid', '//externo.invalid', '/../../externo', '/%2e%2e/externo', '/a\\externo'])('rechaza destino no válido%s', async (path) => {
    const fetchImpl = vi.fn(); const client = createApiClient({ fetchImpl })
    await expect(client.request(path)).rejects.toMatchObject({ status: 422 }); expect(fetchImpl).not.toHaveBeenCalled()
  })
  it('no presenta excepciones internas arbitrarias', () => { expect(safeError(new Error('Detalle secreto interno')).message).not.toContain('secreto') })
  it('rechaza JSON inválido y reconoce204', async () => {
    const client = createApiClient({ fetchImpl: vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => { throw new Error() } }).mockResolvedValueOnce(response(204)), getSession: () => ({ token: 'sintetico', generation: 0 }) })
    await expect(client.request('/a')).rejects.toMatchObject({ status: 503 }); expect(await client.request('/a')).toBeNull()
  })
})
