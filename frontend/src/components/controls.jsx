import React, { forwardRef, useId } from 'react'
export const Button = forwardRef(function Button({ variant = 'primary', type = 'button', busy = false, disabled, children, className = '', ...props }, ref) {
  // Clases completas para que Tailwind conserve todas las variantes aprobadas.
  const variants = { primary: 'button-primary', secondary: 'button-secondary', danger: 'button-danger', ghost: 'button-ghost' }
  const variantClass = Object.hasOwn(variants, variant) ? variants[variant] : variants.primary
  return <button {...props} ref={ref} type={type} className={`button ${variantClass} ${className}`} disabled={disabled || busy} aria-busy={busy || undefined}>{children}</button>
})
export const Input = forwardRef(function Input({ label, error, compact = false, id: provided, className = '', ...props }, ref) {
  const generated = useId(); const id = provided || generated
  return <div className="space-y-1"><label htmlFor={id} className="block text-xs font-medium text-secondary">{label}</label><input {...props} ref={ref} id={id} className={`control ${compact ? 'control-compact' : ''} ${className}`} aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : props['aria-describedby']} />{error && <p id={`${id}-error`} className="text-xs text-[var(--critical)]">{error}</p>}</div>
})
export function Select({ label, options, id: provided, compact = true, ...props }) {
  const generated = useId(); const id = provided || generated
  return <div className="space-y-1"><label htmlFor={id} className="block text-xs font-medium text-secondary">{label}</label><select {...props} id={id} className={`control ${compact ? 'control-compact' : ''}`}>{options.map(({ value, label: text }) => <option key={value} value={value}>{text}</option>)}</select></div>
}
export function FilterBar({ children, count }) { return <div className="flex flex-wrap items-center justify-between gap-4 rounded-control border border-border bg-secondary p-3"><div className="flex flex-1 flex-wrap items-end gap-3">{children}</div>{count !== undefined && <span className="text-xs text-secondary">{count}</span>}</div> }
export function Tabs({ items, active, onChange, label }) {
  return <div role="tablist" aria-label={label} className="flex gap-4 border-b border-border">{items.map((item, index) => <button key={item.id} type="button" role="tab" id={item.id} aria-selected={item.id === active} aria-controls={item.panelId} tabIndex={item.id === active ? 0 : -1} className={`border-b-2 px-1 py-3 text-xs ${item.id === active ? 'border-primary font-medium text-primary' : 'border-transparent text-secondary'}`} onClick={() => onChange(item.id)} onKeyDown={(event) => {
    const next = event.key === 'ArrowRight' ? (index + 1) % items.length : event.key === 'ArrowLeft' ? (index + items.length - 1) % items.length : event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : null
    if (next !== null) { event.preventDefault(); onChange(items[next].id); event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next].focus() }
  }}>{item.label}</button>)}</div>
}
