// Catálogo sintético independiente, conforme al enum y descripciones HTTP canónicos.
export const canonicalQuarantineCauses = [
  ['MISSING_REQUIRED_FIELD', 'Falta un campo obligatorio'],
  ['INVALID_TYPE', 'Tipo de dato no válido'],
  ['INVALID_DATE', 'Fecha no válida'],
  ['OUT_OF_CATALOG', 'Valor fuera del catálogo'],
  ['NEGATIVE_AMOUNT', 'Importe negativo'],
  ['DATE_ORDER_VIOLATION', 'Orden de fechas no válido'],
  ['STRUCTURAL_INCONSISTENCY', 'Estructura inconsistente'],
  ['IDENTITY_CONFLICT', 'Conflicto de identidad'],
  ['TECHNICAL_READ_FAILURE', 'Fallo técnico de lectura'],
  ['OTHER_CAUSE', 'Otra causa de rechazo'],
  ['FORMAT_MISMATCH', 'El formato detectado no coincide con la extensión declarada'],
  ['ARCHIVE_LIMIT_EXCEEDED', 'El contenedor supera los límites de tamaño o entradas'],
  ['ARCHIVE_COMPRESSION_RATIO_EXCEEDED', 'El contenedor supera el límite de compresión'],
  ['CORRUPT_ARCHIVE', 'El contenedor está corrupto o no puede leerse'],
  ['BINARY_CONTENT', 'El archivo tabular contiene datos binarios'],
  ['UNSUPPORTED_ENCODING', 'La codificación del archivo no está admitida'],
  ['UNDETERMINABLE_STRUCTURE', 'No puede determinarse una estructura tabular consistente'],
  ['AMBIGUOUS_DELIMITER', 'El separador de campos es ambiguo'],
  ['PROTECTED_PDF', 'El PDF está protegido y no puede leerse'],
  ['CORRUPT_PDF', 'La estructura del PDF está corrupta'],
  ['UNSUPPORTED_FORMAT', 'El contenido no corresponde a un formato admitido'],
  ['TABULAR_LIMIT_EXCEEDED', 'El contenido tabular supera los límites de filas, columnas o celdas'],
  ['PROTECTED_DOCUMENT', 'El documento está protegido y no puede extraerse'],
  ['EMPTY_DOCUMENT', 'El documento no contiene contenido extraíble'],
  ['TECHNICAL_FAILURE', 'Fallo técnico de procesamiento'],
]

export function quarantineCauseItem(cause_code, cause_description, index = 1) {
  return {
    id: `11111111-1111-4111-8111-${String(index).padStart(12, '0')}`,
    ingest_file_id: '22222222-2222-4222-8222-222222222222',
    source_record_id: null, row_number: null, source_family: null,
    created_at: '2026-10-10T00:00:00Z', cause_code, cause_description,
    state: 'Pendiente', original_payload: null, candidate_payload: null,
    discard_justification: null,
  }
}
