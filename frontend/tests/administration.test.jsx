import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { SessionProvider } from '../src/auth/SessionProvider.jsx'
import { ApiError } from '../src/api/errors.js'
import { AdministrationPage } from '../src/features/operations/AdministrationPage.jsx'

const ownId = '11111111-1111-4111-8111-111111111111'
const otherId = '22222222-2222-4222-8222-222222222222'
function mount({ roles = ['TI'], request = vi.fn(async (path) => path === '/security/users' ? { id: otherId } : null), onRevalidate } = {}) {
  let snapshot = { status: 'authenticated', principal: { id: ownId, username: 'admin', roles }, generation: 1 }
  const listeners = new Set()
  const client = { request, abortAll: vi.fn(), acquireRead: vi.fn() }
  const manager = {
    client, getSnapshot: () => snapshot, subscribe: (callback) => { listeners.add(callback); return () => listeners.delete(callback) }, restore: vi.fn(),
    revalidate: vi.fn(async () => { const replacement = await onRevalidate?.(snapshot); if (replacement) { snapshot = replacement; for (const callback of listeners) callback(snapshot) } return snapshot.status === 'authenticated' }),
  }
  render(<SessionProvider manager={manager}><MemoryRouter><AdministrationPage /></MemoryRouter></SessionProvider>)
  return { request, manager }
}
async function selectAction(value) { await userEvent.selectOptions(screen.getByLabelText('Operación administrativa'), value) }

describe('SRS_REQUIRED: administración TI', () => {
  it('deniega por defecto a un rol no TI', () => {
    const { request } = mount({ roles: ['ANALISTA'] }); expect(screen.getByRole('alert')).toHaveTextContent('Acceso no autorizado'); expect(request).not.toHaveBeenCalled()
  })

  it('crea una cuenta sin persistir contraseña en storage, URL ni resultado', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem'); const { request } = mount()
    await userEvent.type(screen.getByLabelText('Usuario'), 'nuevo')
    await userEvent.type(screen.getByLabelText('Nombre visible'), 'Cuenta nueva')
    await userEvent.type(screen.getByLabelText('Contraseña nueva'), 'Temporal-123')
    await userEvent.click(screen.getByRole('checkbox', { name: 'JURIDICO' }))
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar operación' }))
    expect(await screen.findByRole('status')).toHaveTextContent(`Identificador creado: ${otherId}`)
    expect(request.mock.calls[0][0]).toBe('/security/users'); expect(request.mock.calls[0][0]).not.toContain('Temporal-123')
    expect(storage).not.toHaveBeenCalled(); expect(document.body).not.toHaveTextContent('Temporal-123'); storage.mockRestore()
  })

  it('impide modificar roles propios antes de invocar backend', async () => {
    const { request } = mount(); await selectAction('roles')
    await userEvent.type(screen.getByLabelText(/Identificador conocido/), ownId)
    await userEvent.click(screen.getByRole('checkbox', { name: 'ANALISTA' }))
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar operación' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Acceso no autorizado'); expect(request).not.toHaveBeenCalled()
  })

  it('deshabilita solo después de confirmación explícita', async () => {
    const { request } = mount(); await selectAction('disable'); await userEvent.type(screen.getByLabelText(/Identificador conocido/), otherId)
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar operación' })); expect(request).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toHaveTextContent('No se elimina la cuenta')
    await userEvent.click(screen.getByRole('button', { name: 'Confirmar deshabilitación' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith(`/security/users/${otherId}/disable`, { method: 'POST', body: undefined }))
  })

  it('presenta conflicto del último TI como respuesta segura', async () => {
    const request = vi.fn(async () => { throw new ApiError(409) }); mount({ request }); await selectAction('disable'); await userEvent.type(screen.getByLabelText(/Identificador conocido/), otherId)
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar operación' })); await userEvent.click(screen.getByRole('button', { name: 'Confirmar deshabilitación' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('conflicto')
  })

  it('una revocación durante revalidación impide la mutación', async () => {
    const { request } = mount({ onRevalidate: async (previous) => ({ status: 'authenticated', principal: { ...previous.principal, roles: ['ANALISTA'] }, generation: previous.generation + 1 }) })
    await userEvent.type(screen.getByLabelText('Usuario'), 'nuevo'); await userEvent.type(screen.getByLabelText('Nombre visible'), 'Cuenta'); await userEvent.type(screen.getByLabelText('Contraseña nueva'), 'Temporal'); await userEvent.click(screen.getByRole('checkbox', { name: 'JURIDICO' }))
    await userEvent.click(screen.getByRole('button', { name: 'Aplicar operación' }))
    await waitFor(() => expect(request).not.toHaveBeenCalled())
    expect(screen.queryByText(/Identificador creado/)).not.toBeInTheDocument()
  })
})
