import React from 'react'
import { IngestionPage } from './IngestionPage.jsx'
import { QuarantinePage } from './QuarantinePage.jsx'
import { AuditPage } from './AuditPage.jsx'
import { AdministrationPage } from './AdministrationPage.jsx'

export const navigation = [
  { id: 'operations-ingestion', path: '/ingesta', label: 'Operaciones de ingesta', capability: 'ingest.upload', order: 70 },
  { id: 'operations-quarantine', path: '/cuarentena', label: 'Zona de cuarentena', capability: 'quarantine.read', order: 80 },
  { id: 'operations-audit', path: '/auditoria', label: 'Auditoría', capability: 'audit.read.own', order: 90 },
  { id: 'operations-administration', path: '/administracion', label: 'Administración', capability: 'user.create', order: 100 },
]
export const routes = [
  { id: 'operations-ingestion-route', path: '/ingesta', element: <IngestionPage />, capability: 'ingest.upload' },
  { id: 'operations-quarantine-route', path: '/cuarentena', element: <QuarantinePage />, capability: 'quarantine.read' },
  { id: 'operations-audit-route', path: '/auditoria', element: <AuditPage />, capability: 'audit.read.own' },
  { id: 'operations-administration-route', path: '/administracion', element: <AdministrationPage />, capability: 'user.create' },
]
