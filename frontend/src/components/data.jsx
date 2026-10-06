import React from 'react'
import { EmptyState } from './feedback.jsx'
import { Icon } from './Icon.jsx'
export function Badge({ variant = 'neutral', children }) {
  const semantic = ['critical', 'warning', 'success', 'information'].includes(variant)
  return <span className="badge" style={semantic ? { color: `var(--${variant})`, background: `var(--${variant}-subtle)`, borderColor: `var(--${variant}-border)` } : undefined}>{children}</span>
}
export function RiskBadge({ level }) { return <Badge variant={{ Alto: 'critical', Medio: 'warning', Bajo: 'success' }[level] || 'neutral'}>{level}</Badge> }
export function KpiMetric({ label, displayValue, shortContext, delta, previousContext }) {
  return <div className="min-w-0 py-1"><div className="h-[var(--kpi-title-slot-height)] text-xs font-medium leading-[18px] text-secondary"><span className="line-clamp-2">{label}</span></div><div className="mt-1 flex h-[var(--kpi-value-slot-height)] items-baseline gap-1.5"><span className="text-[26px] font-semibold leading-none tracking-tight tabular-nums text-primary">{displayValue}</span>{shortContext && <span className="truncate text-xs text-secondary">{shortContext}</span>}</div><div className="mt-2 flex h-[var(--kpi-delta-slot-height)] items-center text-xs">{delta ?? <span className="text-muted">—</span>}</div><div className="mt-1 h-[var(--kpi-previous-slot-height)] text-[11px] text-muted">{previousContext}</div></div>
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
