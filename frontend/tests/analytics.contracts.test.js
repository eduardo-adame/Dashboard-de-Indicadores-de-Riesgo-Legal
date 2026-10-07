import { describe, expect, it, vi } from 'vitest'
import { analyticsFilters, analyticsPaths, createAnalyticsApi, replaceAnalyticsFilter } from '../src/features/analytics/api.js'
import { createApiClient } from '../src/api/client.js'
import { ApiError } from '../src/api/errors.js'

describe('SRS_REQUIRED: contrato HTTP y filtros URL', () => {
  it('KPI usa últimos seis meses y Analysis vigente omite period', () => {
    const paths = analyticsPaths(analyticsFilters(''))
    expect(paths.kpis).toBe('/dashboard/kpis?period=last_6_months&risk_type=all')
    expect(paths.analysis).toBe('/dashboard/analysis?risk_type=all')
  })
  it('histórico envía periodo explícito sin crear endpoints', () => {
    expect(analyticsPaths(analyticsFilters('period=last_3_months&risk_type=litigation')).analysis).toBe('/dashboard/analysis?period=last_3_months&risk_type=litigation')
  })
  it('ejecución explícita no requiere fabricar periodo', () => {
    const id = '11111111-1111-4111-8111-111111111111'
    expect(analyticsPaths(analyticsFilters(`analytic_run_id=${id}`)).analysis).toBe(`/dashboard/analysis?analytic_run_id=${id}&risk_type=all`)
  })
  it('serializa filtros válidos sin alterar consulta documental', () => {
    const filters = analyticsFilters('period=custom&period_start=2030-01-01&period_end=2030-02-28&entity=Área%20sintética&risk_type=compliance')
    expect(analyticsPaths(filters).kpis).toContain('entity=%C3%81rea+sint%C3%A9tica')
    expect(analyticsPaths(filters).analysis).not.toContain('query=')
  })
  it.each(['period=bad', 'period=custom&period_start=2030-02-01&period_end=2030-01-01', 'period=last_6_months&period_start=2030-01-01', 'query=secreto&risk_type=alto', 'period=last_3_months&period=last_6_months', 'analytic_run_id=unknown'])('retira filtros inválidos %s', (search) => {
    const filters = analyticsFilters(search); expect(filters.corrected).toBe(true); expect(filters.canonical).not.toContain('secreto')
  })
  it('la edición rechaza valores desconocidos y elimina rango obsoleto', () => {
    expect(() => replaceAnalyticsFilter({}, 'risk_type', 'alto')).toThrow(ApiError)
    expect(replaceAnalyticsFilter({ period: 'custom', period_start: '2030-01-01', period_end: '2030-01-31' }, 'period', 'last_12_months')).toBe('period=last_12_months')
  })
  it('utiliza únicamente lecturas del cliente común', async () => {
    const client = { request: vi.fn().mockResolvedValue({ items: [] }) }; const api = createAnalyticsApi(client)
    await api.kpis(analyticsFilters('')); await api.analysis(analyticsFilters(''))
    expect(client.request.mock.calls).toEqual([['/dashboard/kpis?period=last_6_months&risk_type=all'], ['/dashboard/analysis?risk_type=all']])
  })
  it.each([401, 403, 422, 503])('propaga %i mediante error seguro sin detalles backend', async (status) => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({ detail: 'SQL/documento-restringido' }) })
    const client = createApiClient({ fetchImpl, getSession: () => ({ token: 'token-sintetico-no-operativo', generation: 1 }), recover: async () => false })
    const error = await createAnalyticsApi(client).kpis(analyticsFilters('')).catch((cause) => cause)
    expect(error).toBeInstanceOf(ApiError); expect(error.status).toBe(status); expect(error.message).not.toMatch(/SQL|documento-restringido/)
  })
})
