import React from 'react'
import { RagDocumentPage, RagQueryPage } from './pages.jsx'
import { RagLayout } from './state.jsx'

export const routes = [{ id: 'rag-layout', capability: 'document.query', element: <RagLayout />, children: [{ path: '/consulta', element: <RagQueryPage /> }, { path: '/documentos', element: <RagDocumentPage /> }] }]
export const navigation = [{ id: 'rag-query', path: '/consulta', label: 'Consulta documental', capability: 'document.query', order: 50 }, { id: 'rag-viewer', path: '/documentos', label: 'Visor documental', capability: 'document.query', order: 60 }]
