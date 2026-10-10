import React, { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { ApiError } from '../../api/errors.js'
import { Button, Input, Select, FilterBar } from '../../components/controls.jsx'
import { Modal } from '../../components/layout.jsx'
import { DataTable, Badge } from '../../components/data.jsx'
import { ResourceState, ErrorState } from '../../components/feedback.jsx'
import { ocrPage, readFilters, changeFilters, filterQuery } from './adapters.js'
import { newOperationId, queryPath, useOperation, useOperationsRead } from './api.js'

export function OcrPanel() {
  const session = useSession()
  if (!can(session.principal, 'document.manage')) return <ErrorState error={new ApiError(403)} />
  return <OcrWorkspace key={session.generation} />
}
function OcrWorkspace() {
  const [params, setParams] = useSearchParams(); const filters = readFilters(params.toString(), 'ocr')
  const [selected, setSelected] = useState(null); const [requestId, setRequestId] = useState(null); const [notice, setNotice] = useState(null); const [filterError, setFilterError] = useState(null)
  const resource = useOperationsRead(queryPath('ocr', filters), ocrPage); const mutation = useOperation()
  function change(patch) { try { setParams(filterQuery(changeFilters(filters, patch), 'ocr')); setFilterError(null); setSelected(null) } catch (e) { setFilterError(e) } }
  async function reprocess() {
    if (!selected?.reprocess_eligible || mutation.busy || mutation.result || mutation.error) return
    const result = await mutation.execute('document.manage', (api) => api.reprocess(selected, requestId))
    if (result) { setNotice(`${result.message}${result.pending ? ' Trabajo posterior pendiente.' : ''}`); resource.reload() }
  }
  return <section className="space-y-4"><h2 className="text-base font-semibold">Gestión OCR</h2>
    <FilterBar><Input label="Documento conocido" compact defaultValue={filters.document_id || ''} key={`document-${filters.document_id || ''}`} onBlur={(e) => change({ document_id: e.target.value })} /><Select label="Estado OCR" value={filters.state || ''} options={[{ value: '', label: 'Todos' }, { value: 'Pendiente', label: 'Pendiente' }, { value: 'Rechazado por baja confianza', label: 'Rechazado por baja confianza' }]} onChange={(e) => change({ state: e.target.value })} /><Input label="Procesado desde (fecha con zona)" compact defaultValue={filters.processed_from || ''} onBlur={(e) => change({ processed_from: e.target.value })} /><Input label="Procesado hasta (fecha con zona)" compact defaultValue={filters.processed_to || ''} onBlur={(e) => change({ processed_to: e.target.value })} /><Button variant="secondary" onClick={() => { resource.reload(); mutation.clear(); setSelected(null) }}>Consultar estado OCR</Button></FilterBar>
    {filterError && <ErrorState error={filterError} />}{notice && <p role="status">{notice}</p>}
    <ResourceState resource={resource}>{(data) => <div className="space-y-3"><DataTable caption="Resultados operativos OCR" rows={data.items} columns={[
      { key: 'document_id', label: 'Documento' }, { key: 'file_name', label: 'Archivo' }, { key: 'processing_state', label: 'Procesamiento documental', render: (row) => <Badge>{row.processing_state}</Badge> }, { key: 'processedLabel', label: 'Procesado' }, { key: 'confidenceLabel', label: 'Confianza', numeric: true }, { key: 'ocrStateLabel', label: 'Estado OCR', render: (row) => <Badge>{row.ocrStateLabel}</Badge> }, { key: 'outcomeLabel', label: 'Resultado' }, { key: 'action', label: 'Acción', render: (row) => row.reprocess_eligible ? <Button variant="secondary" disabled={mutation.busy} onClick={() => { setSelected(row); setRequestId(newOperationId()); mutation.clear() }}>Reprocesar</Button> : <span className="text-secondary">Sin acciones</span> },
    ]} />{data.next_cursor && <Button variant="secondary" onClick={() => setParams(filterQuery({ ...filters, cursor: data.next_cursor }, 'ocr'))}>Siguiente página OCR</Button>}</div>}</ResourceState>
    <Modal open={Boolean(selected) && !resource.error} title="Confirmar reproceso OCR" onClose={() => setSelected(null)}><div className="space-y-4"><p className="text-sm">Se reprocesará la versión seleccionada. Una pérdida de respuesta no autoriza un intento nuevo.</p>{mutation.error && <ErrorState error={mutation.error} />}{mutation.result && <p role="status">{mutation.result.message}{mutation.result.pending && ' El recálculo sigue pendiente.'}</p>}<Button onClick={reprocess} busy={mutation.busy} disabled={Boolean(mutation.result || mutation.error)}>Confirmar reproceso</Button></div></Modal>
  </section>
}
