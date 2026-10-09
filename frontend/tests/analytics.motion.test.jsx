import React, { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { AnalyticsPage } from '../src/features/analytics/pages.jsx'
import { KpiMetric } from '../src/components/data.jsx'
import { KPI_CATALOG } from '../src/features/analytics/adapters.js'

vi.mock('recharts', () => {
  const Container = ({ children }) => <div>{children}</div>
  return { ResponsiveContainer: Container, BarChart: Container, LineChart: Container, PieChart: Container, Pie: () => null, Cell: () => null, CartesianGrid: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null, Line: () => null, Bar: () => null }
})

const kpiData = { period_start: null, period_end: null, period_reference: null, filters: {}, items: [{
  kpi_code: 'KPI-RC-03', name: KPI_CATALOG['KPI-RC-03'].name, unit: 'contratos', classification: 'MVP-NÚCLEO',
  dimensions: {}, entity_filter_applicable: false, availability: 'DISPONIBLE', value: '3',
  period_start: '2030-01-01', period_end: '2030-01-31', as_of_date: '2030-01-31', calculated_at: '2030-02-01T00:00:00Z',
}] }
const analysisData = { current_analytic_run_id: null, alert_count: 0, items: [] }

function fixture({ strict = false, view = 'summary' } = {}) {
  const listeners = new Set(); const pending = []
  let snapshot = { status: 'authenticated', principal: { id: '11111111-1111-4111-8111-111111111111', username: 'cuenta-sintetica', roles: ['JURIDICO'] }, generation: 1 }
  const client = { acquireRead: vi.fn((path) => ({ promise: new Promise((resolve) => pending.push({ path, resolve })), release: vi.fn() })), abortAll: vi.fn() }
  const manager = { client, getSnapshot: () => snapshot, subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) }, restore: vi.fn(), revalidate: vi.fn() }
  function publish(changes) { snapshot = { ...snapshot, ...changes }; listeners.forEach((listener) => listener(snapshot)) }
  const element = (selectedView = view) => {
    const page = <SessionProvider manager={manager}><MemoryRouter><AnalyticsPage view={selectedView} /></MemoryRouter></SessionProvider>
    return strict ? <StrictMode>{page}</StrictMode> : page
  }
  const rendered = render(element())
  async function resolveReads() {
    await act(async () => { pending.splice(0).forEach(({ path, resolve }) => resolve(path.startsWith('/dashboard/kpis') ? kpiData : analysisData)) })
    await waitFor(() => expect(document.querySelector('.kpi-metric[data-state="LOADING"]')).toBeNull())
  }
  return { client, publish, resolveReads, rerender: (selectedView) => rendered.rerender(element(selectedView)) }
}
const metrics = () => [...document.querySelectorAll('section[aria-label="Indicadores principales"] .kpi-metric')]

describe('ROBUSTNESS: entrada KPI aprobada, acotada y estable', () => {
  it.each([false, true])('loading no anima y el primer resultado entra una vez; StrictMode=%s', async (strict) => {
    const f = fixture({ strict })
    expect(document.querySelectorAll('.kpi-metric[data-state="LOADING"]')).toHaveLength(5)
    expect(document.querySelectorAll('.kpi-entry')).toHaveLength(0)
    await f.resolveReads()
    expect(metrics()).toHaveLength(5)
    expect(metrics().every((metric) => metric.classList.contains('kpi-entry'))).toBe(true)
    expect(metrics().map((metric) => metric.style.getPropertyValue('--kpi-entry-index'))).toEqual(['0', '1', '2', '3', '4'])
  })
  it('un rerender interno del filtro no remonta las tarjetas ni cambia su entrada', async () => {
    const f = fixture(); await f.resolveReads()
    const nodes = metrics(); const before = f.client.acquireRead.mock.calls.length
    await userEvent.selectOptions(screen.getByLabelText('Tipo de riesgo'), 'litigation')
    expect(metrics().every((metric, index) => metric === nodes[index])).toBe(true)
    expect(metrics().every((metric) => metric.classList.contains('kpi-entry'))).toBe(true)
    expect(f.client.acquireRead).toHaveBeenCalledTimes(before)
  })
  it('refetch por generation de sesión conserva identidad y no reinicia entrada', async () => {
    const f = fixture(); await f.resolveReads()
    const initialPaths = f.client.acquireRead.mock.calls.map(([path]) => path)
    act(() => f.publish({ generation: 2 }))
    expect(document.querySelectorAll('.kpi-metric[data-state="LOADING"]')).toHaveLength(5)
    await f.resolveReads()
    expect(metrics()).toHaveLength(5)
    expect(document.querySelectorAll('.kpi-entry')).toHaveLength(0)
    expect(f.client.acquireRead.mock.calls.slice(-2).map(([path]) => path)).toEqual(initialPaths)
  })
  it('cambio material de periodo inicia una entrada para la nueva selección', async () => {
    const f = fixture(); await f.resolveReads()
    await userEvent.selectOptions(screen.getByLabelText('Análisis'), 'historical')
    await userEvent.selectOptions(screen.getByLabelText('Periodo'), 'current_month')
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar filtros' }))
    await f.resolveReads()
    expect(document.querySelectorAll('.kpi-entry')).toHaveLength(5)
    expect(f.client.acquireRead.mock.calls.some(([path]) => path.includes('period=current_month'))).toBe(true)
  })
  it('identidad y roles iguales con username actualizado no cambian la entrada por sí solos', async () => {
    const f = fixture(); await f.resolveReads()
    const nodes = metrics()
    act(() => f.publish({ principal: { id: '11111111-1111-4111-8111-111111111111', username: 'cuenta-actualizada', roles: ['JURIDICO'] } }))
    expect(metrics().every((metric, index) => metric === nodes[index])).toBe(true)
  })
  it.each(['contracts', 'litigation'])('otras páginas %s no reciben entrada KPI', async (view) => {
    const f = fixture({ view }); await f.resolveReads()
    expect(document.querySelectorAll('.kpi-entry')).toHaveLength(0)
  })
})

describe('ROBUSTNESS: primitiva opt-in y máximo stagger', () => {
  it('KpiMetric sin entryIndex no anima; el índice alto se limita a cuatro', () => {
    const { rerender } = render(<KpiMetric label="Dato" displayValue="3" />)
    expect(screen.getByRole('group')).not.toHaveClass('kpi-entry')
    rerender(<KpiMetric label="Dato" displayValue="3" entryIndex={9} />)
    expect(screen.getByRole('group')).toHaveClass('kpi-entry')
    expect(screen.getByRole('group').style.getPropertyValue('--kpi-entry-index')).toBe('4')
  })
})
