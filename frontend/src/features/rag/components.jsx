import React from 'react'
import { Link } from 'react-router-dom'
import { Badge } from '../../components/data.jsx'
import { Icon } from '../../components/Icon.jsx'
import { formatDate } from '../../shared/formatters.js'
import { citationLocation, RAG_STATES } from './adapters.js'

export function RagStateBanner({ state }) {
  const variant = { EVIDENCIA_SUFICIENTE: 'success', EVIDENCIA_INSUFICIENTE: 'warning', SIN_AUTORIZACION: 'critical' }[state]
  const icon = { EVIDENCIA_SUFICIENTE: 'success', EVIDENCIA_INSUFICIENTE: 'warning', SIN_EVIDENCIA: 'help', SIN_AUTORIZACION: 'locked' }[state]
  if (!Object.hasOwn(RAG_STATES, state)) return null
  return <div role="status" aria-live="polite" className="flex items-center gap-2"><Icon name={icon} size={16} /><Badge variant={variant}>{RAG_STATES[state]}</Badge></div>
}
export function Citation({ citation }) {
  const location = citationLocation(citation)
  return <dl className="space-y-2 break-words text-[13px] text-secondary">
    <div><dt className="text-xs font-medium">Documento</dt><dd>{citation.documentName}</dd></div>
    <div><dt className="text-xs font-medium">Tipo</dt><dd>{citation.documentType}</dd></div>
    <div><dt className="text-xs font-medium">Fecha del documento</dt><dd>{citation.documentDate === null ? 'No disponible' : formatDate(citation.documentDate)}</dd></div>
    {location && <div><dt className="text-xs font-medium">Procedencia</dt><dd>{location}</dd></div>}
    <div><dt className="text-xs font-medium">Identificador del documento</dt><dd className="font-mono text-xs">{citation.documentId}</dd></div>
    <div><dt className="text-xs font-medium">Identificador del fragmento</dt><dd className="font-mono text-xs">{citation.fragmentId}</dd></div>
  </dl>
}
export function EvidenceCard({ fragment, handle }) {
  return <article className="min-w-0 rounded-panel border border-border bg-surface p-5">
    <h3 className="mb-3 break-words text-sm font-semibold">{handle ? `[${handle}] ` : ''}{fragment.documentName}</h3>
    <p className="mb-3 text-xs text-muted">{fragment.usage === 'EVIDENCE' ? 'Fragmento utilizado como evidencia' : 'Fragmento disponible como referencia'}</p>
    <blockquote className="mb-4 whitespace-pre-wrap break-words border-l-2 border-border bg-secondary p-3 text-[13px]">{fragment.text}</blockquote>
    <Citation citation={fragment} />
    <Link className="mt-4 inline-flex text-xs font-medium underline" to={`/documentos?fragment_id=${encodeURIComponent(fragment.fragmentId)}`}>Ver fragmento en visor</Link>
  </article>
}
export function DocumentViewer({ fragment }) {
  return <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-12">
    <section className="min-w-0 rounded-panel border border-border bg-surface p-5 lg:col-span-7" aria-label="Texto original del fragmento">
      <h2 className="mb-4 break-words text-base font-semibold">{fragment.documentName}</h2>
      <div tabIndex={0} role="region" aria-label="Fragmento documental" className="max-h-[740px] overflow-y-auto whitespace-pre-wrap break-words border-l-2 border-[var(--warning)] bg-[var(--warning-subtle)] p-4 font-serif text-sm leading-relaxed">{fragment.text}</div>
    </section>
    <aside className="min-w-0 rounded-panel border border-border bg-surface p-5 lg:col-span-5"><h2 className="mb-4 text-base font-semibold">Metadatos del fragmento</h2><Citation citation={fragment} /></aside>
  </div>
}
