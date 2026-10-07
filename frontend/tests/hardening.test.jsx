/**
 * Pruebas de regresión transversal — hardening W4.
 * Cubre comportamientos compartidos que no corresponden exclusivamente a una feature.
 * No duplica las suites de W1/W2/W3. Complementa el conjunto existente.
 * Clasificación mixta: SRS_REQUIRED y ROBUSTNESS según se indica.
 */
import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'

// ---------------------------------------------------------------------------
// Componentes compartidos
// ---------------------------------------------------------------------------
import { Button, Input, Select, FilterBar, Tabs } from '../src/components/controls.jsx'
import { Loading, EmptyState, ErrorState, ResourceState } from '../src/components/feedback.jsx'
import { Badge, RiskBadge, DataTable, KpiMetric, KpiRow, Delta } from '../src/components/data.jsx'
import { ApiError } from '../src/api/errors.js'

afterEach(() => { vi.restoreAllMocks() })

// ---------------------------------------------------------------------------
// Button — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('Button — accesibilidad y estados', () => {
  it('comunica aria-busy cuando busy=true', () => {
    render(<Button busy>Cargando</Button>)
    const btn = screen.getByRole('button')
    expect(btn).toHaveAttribute('aria-busy', 'true')
    expect(btn).toBeDisabled()
  })

  it('no tiene aria-busy cuando busy=false', () => {
    render(<Button>Acción</Button>)
    const btn = screen.getByRole('button')
    expect(btn).not.toHaveAttribute('aria-busy')
    expect(btn).not.toBeDisabled()
  })

  it('puede recibir className adicional sin perder variante', () => {
    render(<Button variant="secondary" className="w-full">OK</Button>)
    const btn = screen.getByRole('button')
    expect(btn.className).toContain('button-secondary')
    expect(btn.className).toContain('w-full')
  })

  it('acepta ref', () => {
    const ref = React.createRef()
    render(<Button ref={ref}>Ref</Button>)
    expect(ref.current).toBeInstanceOf(HTMLButtonElement)
  })
})

// ---------------------------------------------------------------------------
// Input — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('Input — labels y aria', () => {
  it('asocia label con input mediante id', () => {
    render(<Input label="Nombre" />)
    const input = screen.getByLabelText('Nombre')
    expect(input).toBeInTheDocument()
  })

  it('muestra aria-invalid cuando hay error y muestra el mensaje', () => {
    render(<Input label="Campo" error="Valor requerido" />)
    const input = screen.getByLabelText('Campo')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Valor requerido')).toBeInTheDocument()
  })

  it('aria-invalid=false cuando no hay error', () => {
    render(<Input label="Campo" />)
    const input = screen.getByLabelText('Campo')
    expect(input).toHaveAttribute('aria-invalid', 'false')
  })
})

// ---------------------------------------------------------------------------
// Select — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('Select — accesibilidad', () => {
  it('asocia label con select mediante id', () => {
    const options = [{ value: 'a', label: 'Opción A' }]
    render(<Select label="Estado" options={options} value="a" onChange={() => {}} />)
    expect(screen.getByLabelText('Estado')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// FilterBar — ROBUSTNESS
// ---------------------------------------------------------------------------
describe('FilterBar — layout', () => {
  it('renderiza hijos correctamente', () => {
    render(<FilterBar><span>Filtro</span></FilterBar>)
    expect(screen.getByText('Filtro')).toBeInTheDocument()
  })

  it('muestra count cuando se proporciona', () => {
    render(<FilterBar count={5}><span>A</span></FilterBar>)
    expect(screen.getByText('5')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Tabs — SRS_REQUIRED (navegación por teclado)
// ---------------------------------------------------------------------------
describe('Tabs — navegación por teclado', () => {
  const items = [
    { id: 'tab-a', label: 'Tab A', panelId: 'panel-a' },
    { id: 'tab-b', label: 'Tab B', panelId: 'panel-b' },
    { id: 'tab-c', label: 'Tab C', panelId: 'panel-c' },
  ]

  it('la tecla ArrowRight avanza al siguiente tab', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} active="tab-a" onChange={onChange} label="Tabs" />)
    const tabA = screen.getByRole('tab', { name: 'Tab A' })
    fireEvent.keyDown(tabA, { key: 'ArrowRight' })
    expect(onChange).toHaveBeenCalledWith('tab-b')
  })

  it('la tecla ArrowLeft retrocede al tab anterior', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} active="tab-b" onChange={onChange} label="Tabs" />)
    const tabB = screen.getByRole('tab', { name: 'Tab B' })
    fireEvent.keyDown(tabB, { key: 'ArrowLeft' })
    expect(onChange).toHaveBeenCalledWith('tab-a')
  })

  it('el tab activo tiene tabIndex=0 y los inactivos -1', () => {
    render(<Tabs items={items} active="tab-b" onChange={() => {}} label="Tabs" />)
    expect(screen.getByRole('tab', { name: 'Tab B' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: 'Tab A' })).toHaveAttribute('tabindex', '-1')
  })

  it('la tecla Home va al primer tab', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} active="tab-c" onChange={onChange} label="Tabs" />)
    const tabC = screen.getByRole('tab', { name: 'Tab C' })
    fireEvent.keyDown(tabC, { key: 'Home' })
    expect(onChange).toHaveBeenCalledWith('tab-a')
  })

  it('la tecla End va al último tab', () => {
    const onChange = vi.fn()
    render(<Tabs items={items} active="tab-a" onChange={onChange} label="Tabs" />)
    const tabA = screen.getByRole('tab', { name: 'Tab A' })
    fireEvent.keyDown(tabA, { key: 'End' })
    expect(onChange).toHaveBeenCalledWith('tab-c')
  })
})

// ---------------------------------------------------------------------------
// Loading / EmptyState / ErrorState — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('Loading — role y aria-live', () => {
  it('tiene role=status y aria-live=polite', () => {
    render(<Loading />)
    const el = screen.getByRole('status')
    expect(el).toHaveAttribute('aria-live', 'polite')
    expect(el).toHaveTextContent('Cargando')
  })

  it('admite label personalizada', () => {
    render(<Loading label="Consultando…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Consultando…')
  })
})

describe('EmptyState', () => {
  it('muestra título por defecto', () => {
    render(<EmptyState />)
    expect(screen.getByText('Sin resultados')).toBeInTheDocument()
  })

  it('muestra título y mensaje personalizados', () => {
    render(<EmptyState title="Sin hallazgos" message="Sin resultados para el periodo." />)
    expect(screen.getByText('Sin hallazgos')).toBeInTheDocument()
    expect(screen.getByText('Sin resultados para el periodo.')).toBeInTheDocument()
  })
})

describe('ErrorState — role=alert', () => {
  it('tiene role=alert', () => {
    render(<ErrorState error={new ApiError(500)} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('muestra "Sin autorización" para 403', () => {
    render(<ErrorState error={new ApiError(403)} />)
    expect(screen.getByRole('alert')).toHaveTextContent(/sin autorización/i)
  })

  it('no muestra stacktrace ni payload interno en el mensaje', () => {
    const err = new ApiError(503)
    render(<ErrorState error={err} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).not.toContain('Error:')
    expect(alert.textContent).not.toContain('at ')
  })
})

describe('ResourceState — delegación de estados', () => {
  it('muestra Loading cuando state=loading', () => {
    const resource = { state: 'loading', data: null, error: null, reload: () => {} }
    render(<ResourceState resource={resource}>{() => <span>Datos</span>}</ResourceState>)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByText('Datos')).not.toBeInTheDocument()
  })

  it('muestra EmptyState cuando state=empty', () => {
    const resource = { state: 'empty', data: null, error: null, reload: () => {} }
    render(<ResourceState resource={resource}>{() => <span>Datos</span>}</ResourceState>)
    expect(screen.getByText('Sin resultados')).toBeInTheDocument()
  })

  it('renderiza children cuando hay datos', () => {
    const resource = { state: 'loaded', data: { value: 42 }, error: null, reload: () => {} }
    render(<ResourceState resource={resource}>{(data) => <span>Valor: {data.value}</span>}</ResourceState>)
    expect(screen.getByText('Valor: 42')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Badge / RiskBadge — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('Badge', () => {
  it('renderiza variante neutral sin estilo semántico', () => {
    render(<Badge>Etiqueta</Badge>)
    const badge = screen.getByText('Etiqueta')
    expect(badge).toBeInTheDocument()
    // Sin inline style de color semántico en variante neutral
    expect(badge.style.color).toBeFalsy()
  })

  it('aplica inline color en variante critical', () => {
    render(<Badge variant="critical">Alto</Badge>)
    const badge = screen.getByText('Alto')
    expect(badge.style.color).toBeTruthy()
  })
})

describe('RiskBadge', () => {
  it('level=Alto produce variante critical', () => {
    render(<RiskBadge level="Alto" />)
    const badge = screen.getByText('Alto')
    expect(badge.style.color).toBeTruthy()
  })

  it('level=desconocido produce variante neutral', () => {
    render(<RiskBadge level="Desconocido" />)
    const badge = screen.getByText('Desconocido')
    expect(badge.style.color).toBeFalsy()
  })
})

// ---------------------------------------------------------------------------
// KpiMetric — SRS_REQUIRED (null vs 0)
// ---------------------------------------------------------------------------
describe('KpiMetric — presentación de valores', () => {
  it('muestra displayValue y unidad cortos', () => {
    render(<KpiMetric label="Contratos" displayValue="5" shortContext="contratos" previousContext="Ene 2026" delta={null} />)
    expect(screen.getByText('5')).toBeInTheDocument()
    expect(screen.getByText('contratos')).toBeInTheDocument()
    expect(screen.getByText('Ene 2026')).toBeInTheDocument()
  })

  it('muestra guión cuando delta es null', () => {
    render(<KpiMetric label="KPI" displayValue="3" previousContext="" delta={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// DataTable — SRS_REQUIRED
// ---------------------------------------------------------------------------
describe('DataTable', () => {
  const columns = [
    { key: 'name', label: 'Nombre' },
    { key: 'value', label: 'Valor', numeric: true },
  ]

  it('muestra EmptyState cuando rows está vacío', () => {
    render(<DataTable caption="Tabla" columns={columns} rows={[]} />)
    expect(screen.getByText('Sin resultados')).toBeInTheDocument()
  })

  it('renderiza filas y celdas correctamente', () => {
    const rows = [{ id: '1', name: 'Contrato A', value: '10' }]
    render(<DataTable caption="Tabla" columns={columns} rows={rows} />)
    expect(screen.getByText('Contrato A')).toBeInTheDocument()
    expect(screen.getByText('10')).toBeInTheDocument()
  })

  it('tiene caption oculta visualmente', () => {
    const rows = [{ id: '1', name: 'X', value: '1' }]
    render(<DataTable caption="Caption accesible" columns={columns} rows={rows} />)
    const caption = screen.getByText('Caption accesible')
    expect(caption.tagName).toBe('CAPTION')
    expect(caption.className).toContain('sr-only')
  })

  it('muestra — para valores undefined o null', () => {
    const rows = [{ id: '1', name: undefined, value: null }]
    render(<DataTable caption="Tabla" columns={columns} rows={rows} />)
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(2)
  })

  it('los th tienen scope=col', () => {
    const rows = [{ id: '1', name: 'A', value: '1' }]
    const { container } = render(<DataTable caption="Tabla" columns={columns} rows={rows} />)
    const ths = container.querySelectorAll('th[scope="col"]')
    expect(ths.length).toBe(2)
  })

  it('el contenedor tiene role=region con label', () => {
    const rows = [{ id: '1', name: 'A', value: '1' }]
    const { container } = render(<DataTable caption="Mi tabla" columns={columns} rows={rows} />)
    const region = container.querySelector('[role="region"]')
    expect(region?.getAttribute('aria-label')).toBe('Mi tabla')
  })
})

// ---------------------------------------------------------------------------
// Delta — ROBUSTNESS
// ---------------------------------------------------------------------------
describe('Delta', () => {
  it('muestra texto de variación', () => {
    render(<Delta text="+2.3%" sentiment="positive" direction="up" />)
    expect(screen.getByText(/\+2\.3%/)).toBeInTheDocument()
  })

  it('no muestra ícono cuando direction=flat', () => {
    const { container } = render(<Delta text="0%" sentiment="neutral" direction="flat" />)
    // No debe haber ícono SVG o ion-icon
    const icons = container.querySelectorAll('ion-icon, svg')
    expect(icons.length).toBe(0)
  })
})
