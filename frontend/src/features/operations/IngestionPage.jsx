import React, { useState } from 'react'
import { Link } from 'react-router-dom'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { ApiError } from '../../api/errors.js'
import { PageHeader } from '../../components/layout.jsx'
import { Button, Input, Select } from '../../components/controls.jsx'
import { DataTable, Badge, KpiMetric } from '../../components/data.jsx'
import { ErrorState, ResourceState } from '../../components/feedback.jsx'
import { locations, technicalResult } from './adapters.js'
import { newOperationId, useOperation, useOperationsRead } from './api.js'
import { OcrPanel } from './OcrPanel.jsx'

function TechnicalMetric() {
  const resource = useOperationsRead('/technical/kpis/KPI-CD-03', technicalResult)
  return <section className="space-y-3"><h2 className="text-base font-semibold">Calidad técnica de OCR</h2><ResourceState resource={resource}>{(data) => <KpiMetric label="Tasa de éxito OCR" displayValue={data.displayValue} previousContext={`Calculado: ${data.calculatedLabel}`} />}</ResourceState></section>
}
export function IngestionPage() {
  const { principal, generation } = useSession()
  if (!can(principal, 'ingest.upload')) return <ErrorState error={new ApiError(403)} />
  return <IngestionWorkspace key={generation} principal={principal} />
}
function IngestionWorkspace({ principal }) {
  const [file, setFile] = useState(null); const [location, setLocation] = useState(locations[0].value)
  const [rows, setRows] = useState([]); const [inputKey, setInputKey] = useState(0)
  const mutation = useOperation()
  const blocked = mutation.busy || Boolean(mutation.error && ![403, 409, 422].includes(mutation.error.status))
  async function dispatch(rowsToDispatch) {
    const outcomes = []
    for (const row of rowsToDispatch) {
      if (row.state !== 'COMPLETADO' || row.routing_target === 'NONE') continue
      const data = await mutation.execute('ingest.execute', (api) => api.dispatch(row.file_id, row.correlation_id))
      if (!data) break
      outcomes.push({ fileId: row.file_id, dispatch: data })
      setRows((current) => current.map((item) => item.file_id === row.file_id ? { ...item, dispatch: data } : item))
    }
    return outcomes
  }
  async function upload(event) {
    event.preventDefault(); if (blocked || !file) return
    const result = await mutation.execute('ingest.upload', (api) => api.upload(file, location, newOperationId()))
    if (!result) return
    setRows([result]); setFile(null); setInputKey((v) => v + 1)
    // La recepción y el despacho conservan resultados separados incluso si falla el segundo.
    if (can(principal, 'ingest.execute')) await dispatch([result])
  }
  async function run() {
    if (blocked) return
    const results = await mutation.execute('ingest.execute', (api) => api.run(location))
    if (results) { setRows(results); await dispatch(results) }
  }
  return <>
    <PageHeader title="Operaciones de ingesta" description="Recepción y procesamiento de archivos en ubicaciones controladas." actions={can(principal, 'audit.read.own') && <Link className="text-sm underline" to="/auditoria">Consultar auditoría</Link>} />
    <section className="space-y-4"><h2 className="text-base font-semibold">Recibir archivo</h2>
      <form onSubmit={upload} className="grid grid-cols-1 items-end gap-4 md:grid-cols-2">
        <Select label="Ubicación controlada" options={locations} value={location} disabled={blocked} onChange={(e) => setLocation(e.target.value)} />
        <Input key={inputKey} label="Archivo (máximo 50 MiB)" type="file" required disabled={blocked} accept=".csv,.xlsx,.pdf,.docx,.zip" onChange={(e) => setFile(e.target.files[0] || null)} />
        <div className="flex flex-wrap gap-3 md:col-span-2"><Button type="submit" busy={mutation.busy} disabled={blocked || !file || file.size > 52428800}>Recibir y procesar</Button>{can(principal, 'ingest.execute') && <Button variant="secondary" disabled={blocked} onClick={run}>Ejecutar ubicación</Button>}</div>
      </form>
      {file?.size > 52428800 && <p role="alert" className="text-[var(--critical)]">El archivo supera 50 MiB.</p>}
      {mutation.busy && <p role="status">Procesando solicitud…</p>}
      {mutation.error && <ErrorState error={mutation.error} />}
      {blocked && !mutation.busy && <p className="text-sm text-secondary">Resultado incierto. Consulta Auditoría antes de iniciar otra operación; no se repetirá automáticamente.</p>}
    </section>
    <section className="space-y-3"><h2 className="text-base font-semibold">Resultados de esta interacción</h2><DataTable caption="Recepción y despacho actuales" rows={rows} columns={[
      { key: 'file_id', label: 'Archivo recibido' }, { key: 'reception', label: 'Recepción' }, { key: 'family', label: 'Familia' },
      { key: 'dispatch', label: 'Procesamiento downstream', render: (row) => row.dispatch ? <Badge>{row.dispatch.state}</Badge> : <span>{row.state === 'COMPLETADO' && row.routing_target !== 'NONE' ? 'Sin confirmación de despacho' : 'No despachado'}</span> },
    ]} /><p className="text-xs text-muted">Esta vista no representa un historial persistente.</p></section>
    {can(principal, 'document.manage') && <OcrPanel />}
    {principal.roles.includes('TI') && <TechnicalMetric />}
  </>
}
