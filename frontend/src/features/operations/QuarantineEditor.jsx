import React, { useState } from 'react'
import { Button } from '../../components/controls.jsx'
import { Modal } from '../../components/layout.jsx'
import { ErrorState } from '../../components/feedback.jsx'
import { ApiError } from '../../api/errors.js'
import { object } from './adapters.js'
import { newOperationId, useOperation } from './api.js'

export function QuarantineEditor({ item, onClose, onConfirmed }) {
  const [candidate, setCandidate] = useState(() => JSON.stringify(item.candidate_payload ?? item.original_payload, null, 2))
  const [inputError, setInputError] = useState(null)
  const [identity] = useState(newOperationId)
  const mutation = useOperation()
  const locked = mutation.busy || Boolean(mutation.result) || Boolean(mutation.error && ![403, 409, 422].includes(mutation.error.status))
  async function submit(event) {
    event.preventDefault(); if (locked || !item.canCorrect) return
    let corrected
    try { corrected = JSON.parse(candidate); if (!object(corrected) || !Object.keys(corrected).length) throw new Error() } catch { setInputError(new ApiError(422)); return }
    setInputError(null)
    const result = await mutation.execute('quarantine.reinject', (api) => api.reinject(item.id, corrected, identity))
    if (result) { setCandidate(''); onConfirmed(result) }
  }
  return <Modal open title="Corregir registro pendiente" onClose={onClose}><form onSubmit={submit} className="space-y-4">
    <div><h3 className="text-sm font-semibold">Original rechazado — solo lectura</h3><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-control bg-secondary p-3 text-xs">{JSON.stringify(item.original_payload, null, 2)}</pre></div>
    <div className="space-y-1"><label htmlFor="corrected-candidate" className="block text-xs font-medium text-secondary">Candidato corregido (JSON)</label><textarea id="corrected-candidate" className="control min-h-48 resize-y font-mono text-xs" required disabled={locked} value={candidate} onChange={(e) => setCandidate(e.target.value)} /></div>
    <p className="text-xs text-muted">La validación del dominio corresponde al servidor; el original no se modifica.</p>
    {(inputError || mutation.error) && <ErrorState error={inputError || mutation.error} />}
    {mutation.result && <p role="status">{mutation.result.message}</p>}
    {mutation.error && locked && <p className="text-sm">Consulta el estado antes de repetir. No se creó otro intento.</p>}
    <div className="flex flex-wrap gap-3"><Button type="submit" busy={mutation.busy} disabled={locked || !item.canCorrect}>Validar y reinyectar</Button><Button variant="secondary" onClick={onClose}>Cerrar</Button></div>
  </form></Modal>
}
