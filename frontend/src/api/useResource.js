import { useEffect, useRef, useState } from 'react'
import { useSession } from '../auth/SessionProvider.jsx'
import { safeError } from './errors.js'

export function useResource(path, { isEmpty = (data) => data === null || (Array.isArray(data) && data.length === 0) } = {}) {
  const { client, generation, status } = useSession()
  const [attempt, setAttempt] = useState(0)
  const [result, setResult] = useState({ state: 'loading', data: null, error: null })
  const sequence = useRef(0)
  const emptyCheck = useRef(isEmpty)
  emptyCheck.current = isEmpty
  useEffect(() => {
    const current = ++sequence.current
    setResult({ state: 'loading', data: null, error: null, path, generation })
    if (!path || status !== 'authenticated') return
    const lease = client.acquireRead(path)
    lease.promise.then((data) => {
      if (sequence.current === current) setResult({ state: emptyCheck.current(data) ? 'empty' : 'success', data, error: null, path, generation })
    }).catch((error) => {
      if (sequence.current !== current) return
      const safe = safeError(error)
      setResult({ state: safe.cancelled ? 'cancelled' : safe.recoverable ? 'recoverable_error' : 'unrecoverable_error', data: null, error: safe, path, generation })
    })
    return () => { sequence.current++; lease.release() }
  }, [path, generation, status, client, attempt])
  // Ocultar respuestas obsoletas durante render, sin esperar al efecto siguiente.
  const compatible = status === 'authenticated' && result.path === path && result.generation === generation
  return { ...(compatible ? result : { state: 'loading', data: null, error: null }), reload: () => setAttempt((value) => value + 1) }
}
