import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { QuarantinePage } from '../src/features/operations/QuarantinePage.jsx'
import { quarantineCauseItem } from './fixtures/quarantineCauses.js'

const fileA = '11111111-1111-4111-8111-111111111111'
const fileB = '22222222-2222-4222-8222-222222222222'
function Navigation() {
  const location = useLocation(); const navigate = useNavigate()
  return <><output aria-label="Consulta actual">{location.search}</output><button onClick={() => navigate(-1)}>Atrás de prueba</button><button onClick={() => navigate(1)}>Adelante de prueba</button></>
}
function mount(initial = '/cuarentena') {
  const client = { abortAll: vi.fn(), acquireRead: vi.fn((path) => {
    const query = new URLSearchParams(path.split('?')[1])
    const row = { ...quarantineCauseItem('FORMAT_MISMATCH', 'Formato incompatible'), ingest_file_id: query.get('ingest_file_id') || fileA }
    return { promise: Promise.resolve({ items: [row], next_cursor: null }), release: vi.fn() }
  }) }
  const snapshot = { status: 'authenticated', principal: { id: fileA, username: 'cuenta-sintetica', roles: ['TI'] }, generation: 1 }
  const manager = { client, getSnapshot: () => snapshot, subscribe: () => () => {}, restore: vi.fn(), revalidate: vi.fn(async () => true) }
  render(<SessionProvider manager={manager}><MemoryRouter initialEntries={[initial]}><Navigation /><QuarantinePage /></MemoryRouter></SessionProvider>)
  return client
}
async function consistent(client, id) {
  await waitFor(() => {
    expect(screen.getByLabelText('Archivo de origen (UUID)')).toHaveValue(id)
    expect(new URLSearchParams(screen.getByLabelText('Consulta actual').textContent).get('ingest_file_id') || '').toBe(id)
    expect(new URLSearchParams(client.acquireRead.mock.calls.at(-1)[0].split('?')[1]).get('ingest_file_id') || '').toBe(id)
  })
}
async function setFile(id) {
  fireEvent.change(screen.getByLabelText('Archivo de origen (UUID)'), { target: { value: id } })
  fireEvent.blur(screen.getByLabelText('Archivo de origen (UUID)'))
}

describe('SRS_REQUIRED: URL autoritativa en navegación de cuarentena', () => {
  it('mantiene URL, entrada y consulta iguales en A → B → Atrás → Adelante', async () => {
    const client = mount(); await screen.findByText('Formato incompatible')
    await setFile(fileA); await consistent(client, fileA)
    await setFile(fileB); await consistent(client, fileB)
    await userEvent.click(screen.getByRole('button', { name: 'Atrás de prueba' })); await consistent(client, fileA)
    await userEvent.click(screen.getByRole('button', { name: 'Adelante de prueba' })); await consistent(client, fileB)
  })
  it('restaura también las fechas de rechazo al navegar en el historial', async () => {
    mount('/cuarentena?rejected_from=2030-01-01T00%3A00%3A00Z'); await screen.findByText('Formato incompatible')
    fireEvent.change(screen.getByLabelText('Rechazo desde (fecha con zona)'), { target: { value: '2030-02-01T00:00:00Z' } })
    fireEvent.blur(screen.getByLabelText('Rechazo desde (fecha con zona)'))
    await userEvent.click(screen.getByRole('button', { name: 'Atrás de prueba' }))
    await waitFor(() => expect(screen.getByLabelText('Rechazo desde (fecha con zona)')).toHaveValue('2030-01-01T00:00:00Z'))
    await userEvent.click(screen.getByRole('button', { name: 'Adelante de prueba' }))
    await waitFor(() => expect(screen.getByLabelText('Rechazo desde (fecha con zona)')).toHaveValue('2030-02-01T00:00:00Z'))
  })
})
describe('ROBUSTNESS: validación de filtros sin alterar el destino', () => {
  it('no consulta un UUID inválido ni reemplaza silenciosamente el filtro válido', async () => {
    const client = mount(`/cuarentena?ingest_file_id=${fileA}`); await screen.findByText('Formato incompatible')
    const before = client.acquireRead.mock.calls.length
    await setFile('no-es-uuid')
    expect(await screen.findByRole('alert')).toHaveTextContent('Revisa los datos')
    expect(client.acquireRead).toHaveBeenCalledTimes(before)
    expect(new URLSearchParams(screen.getByLabelText('Consulta actual').textContent).get('ingest_file_id')).toBe(fileA)
  })
})
