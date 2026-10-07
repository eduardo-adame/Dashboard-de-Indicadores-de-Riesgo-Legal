import React, { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { Button } from '../../components/controls.jsx'
import { PageHeader } from '../../components/layout.jsx'
import { EmptyState, ErrorState, Loading } from '../../components/feedback.jsx'
import { Icon } from '../../components/Icon.jsx'
import { readQuery, writeQuery } from '../../shared/query.js'
import { ApiError } from '../../api/errors.js'
import { isUuid } from './adapters.js'
import { DocumentViewer, EvidenceCard, RagStateBanner } from './components.jsx'
import { useRag } from './state.jsx'

export function RagQueryPage() {
  const rag = useRag()
  const session = useSession()
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState('')
  const [validationError, setValidationError] = useState(null)
  const context = readQuery(params.toString(), { context_reference_id: isUuid }).context_reference_id ?? null
  const canonicalQuery = writeQuery(context ? { context_reference_id: context } : {}, { context_reference_id: isUuid })
  useEffect(() => {
    if (canonicalQuery !== params.toString()) setParams(canonicalQuery, { replace: true })
  }, [canonicalQuery, params, setParams])
  useEffect(() => {
    const clearForm = () => { setQuery(''); setValidationError(null) }
    const hidden = () => { if (document.visibilityState !== 'visible') clearForm() }
    window.addEventListener('blur', clearForm); document.addEventListener('visibilitychange', hidden)
    return () => { window.removeEventListener('blur', clearForm); document.removeEventListener('visibilitychange', hidden) }
  }, [])
  useEffect(() => { setQuery('') }, [session.generation])
  async function submit(event) {
    event.preventDefault(); setValidationError(null)
    try { await rag.submit(query, context) } catch { setValidationError(new ApiError(422)) }
  }
  // No habilitar la consulta mientras la URL todavía contiene parámetros ajenos al contrato RAG.
  if (canonicalQuery !== params.toString()) return <Loading label="Preparando la consulta segura…" />
  const result = rag.result
  return <div className="mx-auto max-w-[var(--content-width-rag)] space-y-8">
    <PageHeader title="Consulta documental" description="Consulta las fuentes autorizadas y distingue la evidencia original de la respuesta generada." />
    {context && <section className="rounded-control border border-border bg-secondary p-3 text-xs text-secondary"><p>Referencia contextual de un hallazgo. No constituye evidencia ni selecciona documentos.</p><Button className="mt-2" variant="ghost" onClick={() => { rag.clear(); setParams({}, { replace: true }) }}>Quitar referencia</Button></section>}
    <form className="space-y-3" onSubmit={submit}>
      <label htmlFor="rag-query" className="block text-xs font-medium text-secondary">Consulta en lenguaje natural</label>
      <textarea id="rag-query" required minLength={1} maxLength={4096} rows={4} value={query} onChange={(event) => { setQuery(event.target.value); setValidationError(null) }} className="w-full resize-y rounded-control border border-border bg-surface p-3 text-[13px]" disabled={rag.busy} />
      <div className="flex flex-wrap gap-3"><Button type="submit" busy={rag.busy}><Icon name="search" />Consultar</Button>{rag.submission && !rag.busy && (rag.error || result?.technicalFailure) && <Button variant="secondary" onClick={rag.retry}>Reintentar la misma operación</Button>}{(result || rag.error || rag.busy) && <Button variant="ghost" onClick={() => { rag.clear(); setQuery('') }}>Limpiar consulta</Button>}</div>
    </form>
    {validationError && <ErrorState error={validationError} />}
    {rag.busy && <Loading label="Consultando las fuentes autorizadas…" />}
    {rag.error && <ErrorState error={rag.error} />}
    {result && <section className="space-y-5" aria-label="Resultado de la consulta">
      <RagStateBanner state={result.state} />
      {result.technicalFailure && <ErrorState error={new ApiError(503)} />}
      {result.message && <p className="text-[13px] text-secondary">{result.message}</p>}
      {result.response && <section className="rounded-panel border border-border bg-surface p-5"><h2 className="mb-3 text-base font-semibold">Respuesta generada</h2><p className="whitespace-pre-wrap break-words text-[13px] leading-relaxed">{result.response}</p><p className="mt-4 text-xs text-muted">La síntesis no sustituye la consulta de las fuentes citadas ni el criterio profesional.</p></section>}
      {!!result.fragments.length && <section className="space-y-4"><h2 className="text-base font-semibold">Fragmentos autorizados</h2>{result.fragments.map((fragment) => <EvidenceCard key={fragment.fragmentId} fragment={fragment} handle={result.citations.find((citation) => citation.fragmentId === fragment.fragmentId)?.handle} />)}</section>}
    </section>}
    {!result && !rag.error && !rag.busy && <EmptyState title="Formula una consulta" message="Los resultados y los fragmentos permanecen únicamente en esta sesión de consulta." />}
  </div>
}

export function RagDocumentPage() {
  const rag = useRag()
  const [params, setParams] = useSearchParams()
  const id = readQuery(params.toString(), { fragment_id: isUuid }).fragment_id
  const canonicalQuery = writeQuery(id ? { fragment_id: id } : {}, { fragment_id: isUuid })
  const fragment = rag.result?.fragments.find((item) => item.fragmentId === id)
  useEffect(() => {
    if (params.toString() !== canonicalQuery) setParams(canonicalQuery, { replace: true })
  }, [canonicalQuery, params, setParams])
  if (params.toString() !== canonicalQuery) return <Loading label="Preparando el visor seguro…" />
  return <div className="space-y-8"><PageHeader title="Visor documental" description="Coteja únicamente un fragmento recibido en la consulta actual." actions={<Link className="inline-flex items-center gap-1 text-xs underline" to={rag.result?.contextReferenceId ? `/consulta?context_reference_id=${rag.result.contextReferenceId}` : '/consulta'}><Icon name="back" />Volver a consulta</Link>} />{fragment ? <DocumentViewer fragment={fragment} /> : <EmptyState title="No hay un fragmento disponible" message="Abre el visor desde una consulta autorizada. La recarga no recupera ni vuelve a ejecutar consultas anteriores." />}</div>
}
