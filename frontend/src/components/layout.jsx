import React, { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useSession } from '../auth/SessionProvider.jsx'
import { can } from '../auth/permissions.js'
import { Button } from './controls.jsx'
import { ErrorState } from './feedback.jsx'
import { Icon, navigationIcon } from './Icon.jsx'

export function PageHeader({ title, description, actions }) { return <header className="flex flex-wrap items-end justify-between gap-4 pb-4"><div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{description && <p className="mt-1 text-[13px] text-secondary">{description}</p>}</div>{actions}</header> }
export function Modal({ open, title, onClose, children, drawer = false }) {
  const panel = useRef(null); const close = useRef(onClose); close.current = onClose
  const titleId = useId()
  useEffect(() => {
    if (!open) return
    const previousFocus = document.activeElement
    const previousOverflow = document.body.style.overflow
    const overlay = panel.current.parentElement
    const siblings = [...document.body.children].filter((element) => element !== overlay)
    const previous = siblings.map((element) => [element, element.inert, element.getAttribute('aria-hidden')])
    siblings.forEach((element) => { element.inert = true; element.setAttribute('aria-hidden', 'true') })
    document.body.style.overflow = 'hidden'
    const focusable = () => [...panel.current.querySelectorAll('button, a[href], input, select, textarea, [tabindex="0"]')].filter((element) => !element.disabled && element.getAttribute('aria-hidden') !== 'true')
    ;(focusable()[0] || panel.current).focus()
    const keyboard = (event) => {
      if (event.key === 'Escape') { event.preventDefault(); close.current(); return }
      if (event.key !== 'Tab') return
      const items = focusable(); const first = items[0]; const last = items.at(-1)
      if (!first) { event.preventDefault(); panel.current.focus() }
      else if (event.shiftKey && (document.activeElement === first || !panel.current.contains(document.activeElement))) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', keyboard)
    return () => {
      document.removeEventListener('keydown', keyboard); document.body.style.overflow = previousOverflow
      previous.forEach(([element, inert, hidden]) => { element.inert = inert; if (hidden === null) element.removeAttribute('aria-hidden'); else element.setAttribute('aria-hidden', hidden) })
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [open])
  if (!open) return null
  return createPortal(<div className={`fixed inset-0 z-50 flex bg-black/40 backdrop-blur-[1px] ${drawer ? 'items-stretch justify-start' : 'items-center justify-center p-4'}`}><section ref={panel} role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} className={`${drawer ? 'w-[var(--sidebar-width)]' : 'w-full max-w-lg rounded-panel'} max-h-[var(--modal-max-height)] overflow-y-auto border border-border bg-surface p-5 shadow-subtle`}><div className="mb-4 flex items-center justify-between gap-4"><h2 id={titleId} className="text-base font-semibold">{title}</h2><Button variant="ghost" aria-label="Cerrar" onClick={onClose}><Icon name="close" size={18} /></Button></div>{children}</section></div>, document.body)
}
export function AppShell({ navigation }) {
  const { principal, logout } = useSession()
  const [drawer, setDrawer] = useState(false); const [error, setError] = useState(null)
  const location = useLocation(); const main = useRef(null)
  useEffect(() => { setDrawer(false); main.current?.focus() }, [location.pathname])
  const links = navigation.filter((item) => can(principal, item.capability))
  const menu = <nav aria-label="Navegación principal" className="space-y-1">{links.map((item) => <NavLink key={item.id} to={item.path} end={item.path === '/'} className="nav-link" onClick={() => setDrawer(false)}><Icon name={navigationIcon(item.path)} size={16} />{item.label}</NavLink>)}</nav>
  async function exit() { try { await logout() } catch (cause) { setError(cause) } }
  return <div className="flex h-dvh w-full overflow-hidden bg-page font-sans text-primary antialiased"><a href="#contenido" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:bg-surface focus:p-3">Ir al contenido</a><aside className="hidden w-[var(--sidebar-width)] shrink-0 flex-col border-r border-border bg-surface md:flex"><div className="flex h-[var(--topbar-height)] items-center border-b border-border px-6 text-sm font-semibold">Riesgo Legal</div><div className="flex-1 overflow-y-auto p-3">{menu}</div><div className="border-t border-border p-3"><Button variant="ghost" className="w-full justify-start text-[var(--critical)]" onClick={exit}><Icon name="logout" size={16} />Cerrar sesión</Button></div></aside><div className="flex min-w-0 flex-1 flex-col"><div className="flex h-[var(--topbar-height)] shrink-0 items-center justify-between border-b border-border bg-surface px-4 md:justify-end md:px-6"><Button variant="ghost" className="md:hidden" aria-label="Abrir navegación" aria-expanded={drawer} onClick={() => setDrawer(true)}><Icon name="menu" size={16} /></Button><div className="flex items-center gap-3 text-xs"><span>{principal.username}</span><span className="text-muted">{principal.roles.join(' · ')}</span><Button variant="ghost" className="md:hidden" aria-label="Cerrar sesión" onClick={exit}><Icon name="logout" /></Button></div></div><main id="contenido" ref={main} tabIndex={-1} className="min-w-0 flex-1 overflow-y-auto p-6 sm:p-8"><div className="mx-auto max-w-[var(--content-width-standard)] space-y-8">{error && <ErrorState error={error} />}<Outlet /></div></main></div><Modal open={drawer} title="Riesgo Legal" drawer onClose={() => setDrawer(false)}>{menu}</Modal></div>
}
