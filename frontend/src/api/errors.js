const messages = {
  401: 'Credenciales o sesión no válidas.', 403: 'Acceso no autorizado.',
  404: 'El recurso no está disponible.', 409: 'La operación presenta un conflicto. Consulta su estado antes de repetirla.',
  422: 'Revisa los datos de la solicitud.', 503: 'El servicio no está disponible temporalmente.',
}
export class ApiError extends Error {
  constructor(status = 0, { uncertain = false, cancelled = false } = {}) {
    super(cancelled ? 'Solicitud cancelada.' : uncertain
      ? 'No se pudo confirmar el resultado. Consulta el estado antes de repetir la operación.'
      : messages[status] || 'No se pudo completar la solicitud.')
    this.name = 'ApiError'
    this.status = status
    this.uncertain = uncertain
    this.cancelled = cancelled
    this.recoverable = status === 0 || status >= 500
  }
}
export function safeError(error) { return error instanceof ApiError ? error : new ApiError() }
