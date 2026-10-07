import React, { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { ApiError } from '../../api/errors.js'
import { PageHeader } from '../../components/layout.jsx'
import { Button, Input, FilterBar } from '../../components/controls.jsx'
import { DataTable, Badge } from '../../components/data.jsx'
import { ResourceState, ErrorState } from '../../components/feedback.jsx'
import { auditPage, readFilters, filterQuery } from './adapters.js'
import { queryPath, useOperationsRead } from './api.js'

export function AuditPage() {
  const { principal, generation } = useSession()
  if (!can(principal, 'audit.read.own')) return <ErrorState error={new ApiError(403)} />
  return <AuditWorkspace key={generation} principal={principal} />
}
function AuditWorkspace({ principal }) {
  const [params, setParams] = useSearchParams(); const filters = readFilters(params.toString(), 'audit')
  const [error, setError] = useState(null); const resource = useOperationsRead(queryPath('audit', filters), auditPage)
  function submit(event) {
    event.preventDefault(); const data = new FormData(event.currentTarget); const next = {}
    for (const [key, value] of data) if (value) next[key] = value
    try { setParams(filterQuery(next, 'audit')); setError(null) } catch (cause) { setError(cause) }
  }
  return <>
    <PageHeader title="Auditoría" description={can(principal, 'audit.read.all') ? 'Eventos autorizados por el servidor. Bitácora de solo lectura.' : 'Tus eventos autorizados de ingesta y cuarentena. Bitácora de solo lectura.'} />
    <form key={params.toString()} onSubmit={submit}><FilterBar>
      <Input compact label="Desde (fecha con zona)" name="occurred_from" defaultValue={filters.occurred_from || ''} placeholder="2030-01-01T00:00:00Z" />
      <Input compact label="Hasta (fecha con zona)" name="occurred_to" defaultValue={filters.occurred_to || ''} placeholder="2030-02-01T00:00:00Z" />
      <Input compact label="Acción" name="action" maxLength={128} defaultValue={filters.action || ''} />
      <Input compact label="Tipo de recurso" name="resource_type" maxLength={128} defaultValue={filters.resource_type || ''} />
      <Input compact label="Correlación (UUID)" name="correlation_id" defaultValue={filters.correlation_id || ''} />
      <Input compact label="Límite" name="limit" type="number" min={1} max={200} defaultValue={filters.limit || '100'} />
      <Button type="submit">Aplicar filtros</Button><Button variant="secondary" onClick={resource.reload}>Actualizar</Button>
    </FilterBar></form>
    {error && <ErrorState error={error} />}
    <ResourceState resource={resource}>{(data) => <div className="space-y-4"><DataTable caption="Eventos autorizados de auditoría" rows={data.items} columns={[
      { key: 'occurredLabel', label: 'Fecha y hora' }, { key: 'actor_identifier', label: 'Actor' }, { key: 'action', label: 'Acción' }, { key: 'resource_type', label: 'Tipo de recurso' }, { key: 'resource_identifier', label: 'Recurso' }, { key: 'result', label: 'Resultado', render: (row) => <Badge>{row.result}</Badge> }, { key: 'safe_cause_code', label: 'Causa segura' }, { key: 'correlation_id', label: 'Correlación' }, { key: 'resources', label: 'Referencias', render: (row) => row.resources.length ? <ul>{row.resources.map((item, index) => <li key={index}>{item.resource_type}: {item.resource_identifier}{item.id_documento && ` · ${item.id_documento}`}{item.fragment_id && ` · ${item.fragment_id}`}</li>)}</ul> : '—' },
    ]} />{data.next_cursor && <Button variant="secondary" onClick={() => setParams(filterQuery({ ...filters, cursor: data.next_cursor }, 'audit'))}>Página siguiente</Button>}</div>}</ResourceState>
  </>
}
