import React from 'react'
import { EmptyState } from './feedback.jsx'
import { Icon } from './Icon.jsx'
export function Badge({ variant = 'neutral', children }) {
  const semantic = ['critical', 'warning', 'success', 'information'].includes(variant)
  return <span className="badge" style={semantic ? { color: `var(--${variant})`, background: `var(--${variant}-subtle)`, borderColor: `var(--${variant}-border)` } : undefined}>{children}</span>
}
export function RiskBadge({ level }) { return <Badge variant={{ Alto: 'critical', Medio: 'warning', Bajo: 'success' }[level] || 'neutral'}>{level}</Badge> }
export function KpiMetric({ label, displayValue, shortContext, delta, previousContext, state, accessibleValue, valueKind, entryIndex }) {
  const compact = valueKind === 'state' || (valueKind === undefined && !/^[\d$−-]/.test(displayValue || ''))
  const expandedValue = accessibleValue && accessibleValue !== displayValue
  const hasEntry = Number.isInteger(entryIndex) && entryIndex >= 0
  return <div role="group" aria-label={label} data-state={state} className={`kpi-metric min-w-0 py-1${hasEntry ? ' kpi-entry' : ''}`} style={hasEntry ? { '--kpi-entry-index': Math.min(entryIndex, 4) } : undefined}><div className="h-[var(--kpi-title-slot-height)] text-xs font-medium leading-[18px] text-secondary"><span className="line-clamp-2">{label}</span></div><div className="kpi-value-slot mt-1 flex h-[var(--kpi-value-slot-height)] items-baseline gap-1.5"><span aria-hidden={expandedValue ? true : undefined} className={`min-w-0 truncate font-semibold tracking-tight tabular-nums text-primary ${compact ? 'text-[14px] leading-5' : 'text-[26px] leading-none'}`}>{displayValue}</span>{expandedValue && <span className="sr-only">{accessibleValue}</span>}{shortContext && <span className="shrink-0 truncate text-xs text-secondary">{shortContext}</span>}</div><div className="kpi-delta-slot mt-2 flex h-[var(--kpi-delta-slot-height)] items-center overflow-hidden text-xs">{delta ?? <span className="text-muted">—</span>}</div><div className="mt-1 h-[var(--kpi-previous-slot-height)] truncate text-[11px] text-muted">{previousContext}</div></div>
}
export function KpiSkeleton({ label }) {
  return <div aria-hidden="true" data-state="LOADING" data-label={label} className="kpi-metric min-w-0 py-1"><div className="h-[var(--kpi-title-slot-height)]"><div className="h-3 w-4/5 rounded-control bg-secondary" /></div><div className="mt-1 h-[var(--kpi-value-slot-height)]"><div className="h-6 w-1/2 rounded-control bg-secondary" /></div><div className="mt-2 h-[var(--kpi-delta-slot-height)]"><div className="h-3 w-3/4 rounded-control bg-secondary" /></div><div className="mt-1 h-[var(--kpi-previous-slot-height)]"><div className="h-2 w-2/3 rounded-control bg-secondary" /></div></div>
}
export function KpiRow({ children }) { return <div className="mb-8 border-b border-border pb-6"><div className={`kpi-row ${React.Children.count(children) === 5 ? 'kpi-row-five' : ''}`}>{children}</div></div> }
export function Delta({ text, sentiment = 'neutral', direction = 'flat' }) {
  const color = { positive: 'var(--success)', negative: 'var(--critical)', neutral: 'var(--text-secondary)' }[sentiment] || 'var(--text-secondary)'
  return <span className="inline-flex items-center gap-1 font-medium" style={{ color }}>{direction !== 'flat' && <Icon name={direction === 'up' ? 'up' : 'down'} size={12} />}{text}</span>
}
export function DataTable({ caption, columns, rows, rowKey = 'id', sort, onSort }) {
  if (!rows.length) return <EmptyState />
  return <div className="table-scroll" tabIndex={0} role="region" aria-label={caption}><table className="data-table"><caption className="sr-only">{caption}</caption><thead><tr>{columns.map((column) => <th key={column.key} scope="col" aria-sort={sort?.key === column.key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : undefined}>{column.sortable && onSort ? <button type="button" className="inline-flex items-center gap-1" onClick={() => onSort(column.key)}>{column.label}<Icon name={sort?.key === column.key ? (sort.direction === 'asc' ? 'up' : 'down') : 'sort'} size={12} /></button> : column.label}</th>)}</tr></thead><tbody>{rows.map((row) => <tr key={row[rowKey]}>{columns.map((column) => <td key={column.key} className={column.numeric ? 'text-right font-mono tabular-nums' : ''}>{column.render ? column.render(row) : row[column.key] ?? '—'}</td>)}</tr>)}</tbody></table></div>
}
