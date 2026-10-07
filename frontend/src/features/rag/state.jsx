import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { Loading } from '../../components/feedback.jsx'
import { ApiError } from '../../api/errors.js'
import { createRagSubmission, requestRag } from './api.js'

const RagContext = createContext(null)
export function RagLayout() {
  const session = useSession()
  const location = useLocation()
  const owner = `${session.principal?.id || ''}:${session.generation}`
  const [snapshot, setSnapshot] = useState({ result: null, error: null, busy: false, submission: null, owner })
  const [verified, setVerified] = useState(null)
  const epoch = useRef(0)
  const controller = useRef(null)
  const currentOwner = useRef(owner)
  currentOwner.current = owner
  const clear = useCallback(() => {
    epoch.current++; controller.current?.abort(); controller.current = null
    setSnapshot({ result: null, error: null, busy: false, submission: null, owner: currentOwner.current })
  }, [])
  useEffect(() => { clear() }, [owner, clear])
  useEffect(() => {
    let mounted = true
    setVerified(null)
    // Revalidar identidad no certifica ACL: solo una consulta explícita entrega evidencia autorizada.
    Promise.resolve(session.revalidate()).then((valid) => { if (mounted) { if (!valid) clear(); setVerified(location.key) } }).catch(() => { if (mounted) { clear(); setVerified(location.key) } })
    return () => { mounted = false }
  }, [location.key, session.revalidate, clear])
  useEffect(() => {
    const hidden = () => { if (document.visibilityState !== 'visible') clear() }
    window.addEventListener('blur', clear); document.addEventListener('visibilitychange', hidden)
    return () => { clear(); window.removeEventListener('blur', clear); document.removeEventListener('visibilitychange', hidden) }
  }, [clear])
  async function execute(submission) {
    clear()
    const expected = epoch.current
    const startedOwner = currentOwner.current
    controller.current = new AbortController()
    setSnapshot({ result: null, error: null, busy: true, submission, owner: startedOwner })
    try {
      const result = await requestRag(session.client, submission, controller.current.signal)
      if (expected === epoch.current && startedOwner === currentOwner.current) setSnapshot({ result, error: null, busy: false, submission, owner: startedOwner })
    } catch (cause) {
      if (expected !== epoch.current || startedOwner !== currentOwner.current) return
      const error = cause instanceof ApiError ? cause : new ApiError(503)
      setSnapshot({ result: null, error, busy: false, submission: [401, 403, 409, 422].includes(error.status) ? null : submission, owner: startedOwner })
    }
  }
  const visible = snapshot.owner === owner && session.status === 'authenticated' ? snapshot : { result: null, error: null, busy: false, submission: null }
  const value = { ...visible, clear, submit: (query, contextId) => execute(createRagSubmission(query, contextId)), retry: () => visible.submission && execute(visible.submission) }
  return <RagContext.Provider value={value}>{verified === location.key ? <Outlet /> : <Loading label="Comprobando acceso a la consulta…" />}</RagContext.Provider>
}
export function useRag() {
  const context = useContext(RagContext)
  if (!context) throw new Error('La vista requiere el contexto de consulta.')
  return context
}
