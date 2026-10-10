import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { IngestionPage } from '../src/features/operations/IngestionPage.jsx'

function mount(role) {
  const client = { request: vi.fn(), acquireRead: vi.fn((path) => ({ promise: Promise.resolve(path.startsWith('/technical/kpis') ? { kpi_code: 'KPI-CD-03', availability: 'NO_DISPONIBLE', value: null, calculated_at: null } : { items: [], next_cursor: null }), release: vi.fn() })), abortAll: vi.fn() }
  const snapshot = { status: 'authenticated', principal: { id: '11111111-1111-4111-8111-111111111111', username: 'sintetico', roles: [role] }, generation: 1 }
  const manager = { client, getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn(async () => true) }
  render(<SessionProvider manager={manager}><MemoryRouter><IngestionPage /></MemoryRouter></SessionProvider>)
  return client
}

describe('SRS_REQUIRED: formatos ofrecidos por la carga manual', () => {
  it.each(['TI', 'ANALISTA'])('ofrece exactamente CSV/XLSX/PDF/DOCX para %s, sin ZIP en accept ni ayudas/opciones', async (role) => {
    const client = mount(role)
    const input = screen.getByLabelText('Archivo (máximo 50 MiB)')
    expect(input).toHaveAttribute('accept', '.csv,.xlsx,.pdf,.docx')
    expect(input.getAttribute('accept').split(',')).not.toContain('.zip')
    expect(screen.queryByText(/\bZIP\b/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /ZIP/i })).not.toBeInTheDocument()
    await screen.findByText('Gestión OCR')
    expect(client.request).not.toHaveBeenCalled()
  })

  it.each([
    ['csv', 'text/csv'],
    ['xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['pdf', 'application/pdf'],
    ['docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ])('mantiene la selección de %s sin procesar ni validar contenido en el cliente', async (extension, type) => {
    const client = mount('ANALISTA')
    const input = screen.getByLabelText('Archivo (máximo 50 MiB)')
    const file = new File(['Contenido sintético; no se envía'], `sintetico.${extension}`, { type })
    await userEvent.upload(input, file)
    expect(input.files).toHaveLength(1); expect(input.files[0]).toBe(file)
    expect(screen.getByRole('button', { name: 'Recibir y procesar', exact: true })).toBeEnabled()
    expect(client.request).not.toHaveBeenCalled()
  })
})
