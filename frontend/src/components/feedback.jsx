import React from 'react'
import { safeError } from '../api/errors.js'
import { Button } from './controls.jsx'
export function Loading({ label = 'Cargando…' }) { return <div role="status" aria-live="polite" className="border border-border bg-secondary p-5 text-secondary">{label}</div> }
export function EmptyState({ title = 'Sin resultados', message, action }) { return <section className="rounded-panel border border-border p-5"><h2 className="text-sm font-semibold">{title}</h2>{message && <p className="mt-1 text-[13px] text-secondary">{message}</p>}{action && <div className="mt-3">{action}</div>}</section> }
export function ErrorState({ error, onRetry }) {
  const safe = safeError(error)
  return <section role="alert" className="rounded-panel border border-[var(--critical-border)] bg-[var(--critical-subtle)] p-5"><h2 className="text-sm font-semibold">{safe.status === 403 ? 'Sin autorización' : 'No se pudo completar la solicitud'}</h2><p className="mt-1 text-secondary">{safe.message}</p>{onRetry && safe.recoverable && !safe.uncertain && <Button className="mt-3" variant="secondary" onClick={onRetry}>Reintentar</Button>}</section>
}
export function ResourceState({ resource, children }) {
  if (resource.state === 'loading') return <Loading />
  if (resource.state === 'empty') return <EmptyState />
  if (resource.state === 'cancelled') return <EmptyState title="Consulta cancelada" />
  if (resource.error) return <ErrorState error={resource.error} onRetry={resource.reload} />
  return children(resource.data)
}
