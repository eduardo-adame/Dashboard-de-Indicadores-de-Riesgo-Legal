import React, { useState } from 'react'
import { useSession } from '../../auth/SessionProvider.jsx'
import { can } from '../../auth/permissions.js'
import { ApiError } from '../../api/errors.js'
import { PageHeader, Modal } from '../../components/layout.jsx'
import { Button, Input, Select } from '../../components/controls.jsx'
import { ErrorState } from '../../components/feedback.jsx'
import { families, uuid } from './adapters.js'
import { useOperation } from './api.js'

export const adminActions = [
  { value: 'create', label: 'Crear cuenta', capability: 'user.create' },
  { value: 'update', label: 'Editar nombre', capability: 'user.update' },
  { value: 'activate', label: 'Activar cuenta', capability: 'user.update' },
  { value: 'disable', label: 'Deshabilitar cuenta', capability: 'user.disable' },
  { value: 'reset', label: 'Restablecer acceso', capability: 'user.reset_access' },
  { value: 'roles', label: 'Asignar roles', capability: 'role.assign' },
  { value: 'scope', label: 'Alcance por familia', capability: 'document_acl.manage' },
  { value: 'user-exception', label: 'Excepción documental por cuenta', capability: 'document_acl.manage' },
  { value: 'role-exception', label: 'Excepción documental por rol', capability: 'document_acl.manage' },
]
const roles = ['JURIDICO', 'ANALISTA', 'TI']
export function AdministrationPage() {
  const { principal, generation } = useSession()
  if (!principal?.roles.includes('TI') || !can(principal, 'user.create')) return <ErrorState error={new ApiError(403)} />
  return <AdministrationWorkspace key={generation} principal={principal} />
}
function AdministrationWorkspace({ principal }) {
  const [action, setAction] = useState('create'); const [formKey, setFormKey] = useState(0); const [error, setError] = useState(null); const [pending, setPending] = useState(null)
  const mutation = useOperation()
  const account = ['update', 'activate', 'disable', 'reset', 'roles', 'user-exception'].includes(action)
  const role = ['scope', 'role-exception'].includes(action)
  const requiresPassword = ['create', 'reset'].includes(action)
  const blocked = mutation.busy || Boolean(mutation.error && ![403, 409, 422].includes(mutation.error.status))
  async function perform(fields) {
    setError(null)
    const result = await mutation.execute(adminActions.find((item) => item.value === action).capability, (api) => api.administer(action, fields))
    // La contraseña nunca forma parte del resultado ni del estado React.
    if (Object.hasOwn(fields, 'password')) fields.password = ''
    setFormKey((v) => v + 1); setPending(null)
    return result
  }
  async function submit(event) {
    event.preventDefault(); if (blocked) return
    const form = event.currentTarget; const data = new FormData(form)
    const fields = Object.fromEntries(data); fields.roles = data.getAll('roles'); fields.active = fields.active === 'true'
    try {
      if (account && !uuid(fields.account_id)) throw new ApiError(422)
      if (action === 'roles' && fields.account_id.toLowerCase() === principal.id.toLowerCase()) throw new ApiError(403)
      if (['create', 'roles'].includes(action) && !fields.roles.length) throw new ApiError(422)
      setError(null)
      if (action === 'disable') { setPending(fields); return }
      // Borrar de inmediato el control visible; el transporte solo lo retiene hasta completar la petición.
      if (requiresPassword) form.elements.password.value = ''
      await perform(fields)
    } catch (cause) { fields.password = ''; if (requiresPassword) form.elements.password.value = ''; setError(cause) }
  }
  return <>
    <PageHeader title="Administración" description="Operaciones TI sobre cuentas e identificadores conocidos. No existe un catálogo de búsqueda en esta API." />
    <Select label="Operación administrativa" value={action} disabled={mutation.busy} options={adminActions.filter((item) => can(principal, item.capability))} onChange={(e) => { setAction(e.target.value); setFormKey((v) => v + 1); setError(null); mutation.clear(); setPending(null) }} />
    <form key={`${action}:${formKey}`} onSubmit={submit} className="grid grid-cols-1 items-end gap-4 md:grid-cols-2">
      {account && <Input label="Identificador conocido de cuenta (UUID)" name="account_id" required disabled={blocked} />}
      {action === 'create' && <Input label="Usuario" name="username" required maxLength={256} autoComplete="off" disabled={blocked} />}
      {['create', 'update'].includes(action) && <Input label="Nombre visible" name="display_name" required maxLength={256} disabled={blocked} />}
      {requiresPassword && <Input label="Contraseña nueva" name="password" type="password" autoComplete="new-password" minLength={1} maxLength={128} required disabled={blocked} />}
      {['create', 'roles'].includes(action) && <fieldset className="space-y-2"><legend className="text-xs font-medium text-secondary">Roles activos</legend>{roles.map((value) => <label key={value} className="mr-4 inline-flex items-center gap-2 text-sm"><input type="checkbox" name="roles" value={value} disabled={blocked} />{value}</label>)}</fieldset>}
      {role && <Select label="Rol" name="role_id" options={roles.map((v) => ({ value: v, label: v }))} disabled={blocked} />}
      {action === 'scope' && <Select label="Familia documental" name="source_family" options={families.map((v) => ({ value: v, label: v }))} disabled={blocked} />}
      {['user-exception', 'role-exception'].includes(action) && <><Input label="Identificador conocido de documento" name="document_id" required maxLength={256} disabled={blocked} /><Select label="Decisión documental" name="decision" options={[{ value: 'DENY', label: 'Denegar' }, { value: 'ALLOW', label: 'Autorizar' }]} disabled={blocked} /></>}
      {['scope', 'user-exception', 'role-exception'].includes(action) && <Select label="Vigencia del permiso" name="active" options={[{ value: 'true', label: 'Activo' }, { value: 'false', label: 'Inactivo' }]} disabled={blocked} />}
      <div className="md:col-span-2"><Button type="submit" busy={mutation.busy} disabled={blocked}>Aplicar operación</Button></div>
    </form>
    {action === 'roles' && <p className="text-sm text-secondary">No puedes modificar tus propios roles.</p>}
    {action === 'disable' && <p className="text-sm text-secondary">El servidor impide deshabilitar al último TI activo y conserva las referencias históricas.</p>}
    {(error || mutation.error) && <ErrorState error={error || mutation.error} />}
    {blocked && !mutation.busy && <p className="text-sm">Resultado incierto. Revisa Auditoría antes de realizar otra mutación.</p>}
    {mutation.result && <p role="status">Operación confirmada.{mutation.result.id && ` Identificador creado: ${mutation.result.id}`}</p>}
    <Modal open={Boolean(pending)} title="Confirmar deshabilitación" onClose={() => setPending(null)}><div className="space-y-4"><p className="text-sm">Se invalidarán las sesiones afectadas. No se elimina la cuenta.</p><Button variant="danger" busy={mutation.busy} onClick={() => perform(pending)}>Confirmar deshabilitación</Button></div></Modal>
  </>
}
