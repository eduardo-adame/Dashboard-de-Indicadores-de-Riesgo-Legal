// Respuestas sintéticas para comprobar invalidación; no representan datos persistidos.
export const refreshId = '11111111-1111-4111-8111-111111111111'
export const refreshVersionId = '22222222-2222-4222-8222-222222222222'
export const refreshUpload = { file_id: refreshId, operation_id: refreshVersionId, correlation_id: refreshId, state: 'COMPLETADO', format: 'PDF', family: 'CONTRATOS_DOCUMENTOS', routing_target: 'DOCUMENT', safe_cause_code: null, idempotent: false }
export const refreshDispatch = { downstream_target: 'DOCUMENT', operation_id: refreshVersionId, downstream_result_id: refreshId, state: 'COMPLETED' }
export const refreshOcrItem = { document_id: 'DOC-REF', document_version_id: refreshVersionId, document_name: 'Documento sintético', file_name: 'sintetico.pdf', processing_state: 'RECHAZADA', ocr_applicable: true, reprocess_eligible: true, ocr_state: 'Rechazado por baja confianza', processed_at: '2026-10-10T00:00:00Z', confidence: 0.6, total_page_count: 1, ocr_processed_page_count: 1, granularity: 'PAGE', outcome: 'Rechazado por baja confianza' }
export const refreshOcrFinal = { ...refreshOcrItem, processing_state: 'LISTA', reprocess_eligible: false, ocr_state: 'Exitoso', confidence: 0.95, outcome: 'Exitoso' }
export const refreshReprocess = { operation_id: refreshId, document_id: 'DOC-REF', document_version_id: refreshId, ocr_state: 'Exitoso', processing_state: 'LISTA', kpi_job_id: refreshVersionId, kpi_job_state: 'COMPLETED' }
export const refreshKpi = (value) => ({ kpi_code: 'KPI-CD-03', availability: 'DISPONIBLE', value, calculated_at: '2026-10-10T00:00:00Z' })
