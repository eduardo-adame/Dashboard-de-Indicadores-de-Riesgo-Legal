import React, { useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useSession } from './SessionProvider.jsx'
import { Button, Input } from '../components/controls.jsx'
import { ErrorState, Loading } from '../components/feedback.jsx'

export function LoginPage({ destination = '/' }) {
  const { status, login, sessionError } = useSession()
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(null)
  if (status === 'authenticated') return <Navigate to={destination} replace />
  if (status === 'restoring' && !busy) return <Loading label="Comprobando sesión…" />
  async function submit(event) {
    event.preventDefault(); if (busy) return
    setBusy(true); setError(null)
    try { await login(username, password) } catch (cause) { setError(cause) } finally { setPassword(''); setBusy(false) }
  }
  return <main className="flex min-h-dvh items-center justify-center p-6"><section className="w-full max-w-sm rounded-panel border border-border bg-surface p-6 shadow-subtle"><p className="mb-6 text-sm font-semibold">Riesgo Legal</p><h1 className="text-2xl font-semibold">Iniciar sesión</h1><p className="mt-1 text-secondary">Accede con tu cuenta autorizada.</p><form className="mt-6 space-y-4" onSubmit={submit}><Input label="Usuario" autoComplete="username" value={username} maxLength={256} required disabled={busy} onChange={(event) => setUsername(event.target.value)} /><Input label="Contraseña" type="password" autoComplete="current-password" value={password} maxLength={128} required disabled={busy} onChange={(event) => setPassword(event.target.value)} />{(error || sessionError) && <ErrorState error={error || sessionError} />}<Button type="submit" className="w-full" busy={busy}>{busy ? 'Accediendo…' : 'Iniciar sesión'}</Button></form></section></main>
}
