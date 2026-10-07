import React from 'react'
import { ContractsPage, LitigationPage, SummaryPage, TrendsPage } from './pages.jsx'

export const routes = [
  { path: '/', element: <SummaryPage />, capability: 'dashboard.read' },
  { path: '/contratos', element: <ContractsPage />, capability: 'dashboard.read' },
  { path: '/litigios', element: <LitigationPage />, capability: 'dashboard.read' },
  { path: '/tendencias', element: <TrendsPage />, capability: 'dashboard.read' },
]
export const navigation = [
  { id: 'analytics-summary', path: '/', label: 'Resumen ejecutivo', capability: 'dashboard.read', order: 10 },
  { id: 'analytics-contracts', path: '/contratos', label: 'Riesgo contractual', capability: 'dashboard.read', order: 20 },
  { id: 'analytics-litigation', path: '/litigios', label: 'Gestión de litigios', capability: 'dashboard.read', order: 30 },
  { id: 'analytics-trends', path: '/tendencias', label: 'Tendencias y riesgos', capability: 'dashboard.read', order: 40 },
]
