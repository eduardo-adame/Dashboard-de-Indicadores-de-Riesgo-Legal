import React, { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { ApiError } from '../../api/errors.js'
import { PageHeader, Modal } from '../../components/layout.jsx'
import { Button, Input, Select, FilterBar } from '../../components/controls.jsx'
import { DataTable, Badge } from '../../components/data.jsx'
import { ResourceState, ErrorState } from '../../components/feedback.jsx'
import { quarantinePage, readFilters, changeFilters, filterQuery, families, quarantineStates, quarantineCauses } from './adapters.js'
import { queryPath, useOperationsRead, useOperation } from './api.js'
import { QuarantineEditor } from './QuarantineEditor.jsx'

export function QuarantinePage() {
  const { principal, generation } = useSession()
  if (!can(principal, 'quarantine.read')) return <ErrorState error={new ApiError(403)} />
  return <QuarantineWorkspace key={generation} principal={principal} />
}
function QuarantineWorkspace({ principal }) {
  const [params, setParams] = useSearchParams(); const filters = readFilters(params.toString(), 'quarantine')
  const [editing, setEditing] = useState(null); const [discarding, setDiscarding] = useState(null); const [justification, setJustification] = useState(''); const [notice, setNotice] = useState(null)
  const [filterError, setFilterError] = useState(null)
  const resource = useOperationsRead(queryPath('quarantine', filters), quarantinePage); const mutation = useOperation()
  function change(patch) {
    try { setParams(filterQuery(changeFilters(filters, patch), 'quarantine')); setFilterError(null); setEditing(null); setDiscarding(null); setNotice(null) }
    catch (cause) { setFilterError(cause) }
  }
  async function discard(event) {
    event.preventDefault(); if (mutation.busy || !discarding?.canAct || !justification.trim()) return
    const result = await mutation.execute('quarantine.discard', (api) => api.discard(discarding.id, justification))
    if (result) { setDiscarding(null); setJustification(''); setNotice('Descarte confirmado.'); resource.reload() }
  }
  return <>
    <PageHeader title="Zona de cuarentena" description="Revisión de elementos retenidos, sin modificar el contenido original." />
    <FilterBar>
      <Select label="Estado" value={filters.state || ''} onChange={(e) => change({ state: e.target.value })} options={[{ value: '', label: 'Todos' }, ...quarantineStates.map((v) => ({ value: v, label: v }))]} />
      <Select label="Familia" value={filters.source_family || ''} onChange={(e) => change({ source_family: e.target.value })} options={[{ value: '', label: 'Todas' }, ...families.map((v) => ({ value: v, label: v }))]} />
      <Select label="Regla infringida" value={filters.cause_code || ''} onChange={(e) => change({ cause_code: e.target.value })} options={[{ value: '', label: 'Todas' }, ...quarantineCauses.map((v) => ({ value: v, label: v.replaceAll('_', ' ') }))]} />
      <Input key={`file-${filters.ingest_file_id || ''}`} label="Archivo de origen (UUID)" compact defaultValue={filters.ingest_file_id || ''} onBlur={(e) => change({ ingest_file_id: e.target.value })} />
      <Input key={`from-${filters.rejected_from || ''}`} label="Rechazo desde (fecha con zona)" compact defaultValue={filters.rejected_from || ''} placeholder="2030-01-01T00:00:00Z" onBlur={(e) => change({ rejected_from: e.target.value })} />
      <Input key={`to-${filters.rejected_to || ''}`} label="Rechazo hasta (fecha con zona)" compact defaultValue={filters.rejected_to || ''} placeholder="2030-02-01T00:00:00Z" onBlur={(e) => change({ rejected_to: e.target.value })} />
      <Button variant="secondary" onClick={resource.reload}>Actualizar estado</Button>
    </FilterBar>
    {filterError && <ErrorState error={filterError} />}
    {notice && <p role="status">{notice}</p>}
    {mutation.error && <ErrorState error={mutation.error} />}
    <ResourceState resource={resource}>{(data) => <div className="space-y-4"><DataTable caption="Elementos en cuarentena" rows={data.items} columns={[
      { key: 'id', label: 'Elemento' }, { key: 'createdLabel', label: 'Rechazo' }, { key: 'source_family', label: 'Familia' }, { key: 'row_number', label: 'Fila' },
      { key: 'cause_description', label: 'Causa' }, { key: 'state', label: 'Estado', render: (row) => <Badge>{row.state}</Badge> },
      { key: 'actions', label: 'Acciones', render: (row) => <div className="flex flex-wrap gap-2">{row.canCorrect && can(principal, 'quarantine.reinject') && <Button variant="ghost" onClick={() => setEditing(row)}>Corregir</Button>}{row.canAct && can(principal, 'quarantine.discard') && <Button variant="danger" onClick={() => { mutation.clear(); setDiscarding(row); setJustification('') }}>Descartar</Button>}{!row.canAct && <span>Sin acciones</span>}{row.canAct && !row.canCorrect && <span>Sin registro tabular corregible</span>}</div> },
    ]} />{data.next_cursor && <Button variant="secondary" onClick={() => setParams(filterQuery({ ...filters, cursor: data.next_cursor }, 'quarantine'))}>Página siguiente</Button>}</div>}</ResourceState>
    {editing && !resource.error && <QuarantineEditor key={editing.id} item={editing} onClose={() => setEditing(null)} onConfirmed={(result) => { setNotice(result.message); resource.reload() }} />}
    <Modal open={Boolean(discarding) && !resource.error} title="Confirmar descarte" onClose={() => { setDiscarding(null); setJustification('') }}><form onSubmit={discard} className="space-y-4"><Input label="Justificación del descarte" value={justification} required disabled={mutation.busy} onChange={(e) => setJustification(e.target.value)} /><p className="text-sm text-secondary">El registro se excluirá de futuras reinyecciones. El original se conserva.</p>{mutation.error && <ErrorState error={mutation.error} />}<Button type="submit" variant="danger" busy={mutation.busy} disabled={!justification.trim() || Boolean(mutation.error)}>Confirmar descarte</Button></form></Modal>
  </>
}
