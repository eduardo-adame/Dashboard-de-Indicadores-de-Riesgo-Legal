import { describe, expect, it } from 'vitest'
import { ApiError } from '../src/api/errors.js'
import { quarantineCauses, quarantinePage, filterQuery, readFilters } from '../src/features/operations/adapters.js'
import { canonicalQuarantineCauses, quarantineCauseItem } from './fixtures/quarantineCauses.js'

describe('SRS_REQUIRED: consulta y filtros de causas seguras de cuarentena', () => {
  it.each(canonicalQuarantineCauses)('acepta %s en respuesta y filtro sin alterar su descripción', (code, description) => {
    const row = quarantineCauseItem(code, description)
    expect(quarantinePage({ items: [row], next_cursor: null }).items[0]).toMatchObject({ ...row, canAct: true, canCorrect: false })
    expect(filterQuery({ cause_code: code }, 'quarantine')).toBe(`cause_code=${code}&limit=100`)
    expect(readFilters(`cause_code=${code}`, 'quarantine')).toEqual({ cause_code: code })
  })

  it('una página global con todos los motivos se adapta completa sin perder originales ni null', () => {
    const items = canonicalQuarantineCauses.map(([code, description], index) => quarantineCauseItem(code, description, index + 1))
    const result = quarantinePage({ items, next_cursor: 'cursor-contrato' })
    expect(result.items).toHaveLength(25)
    expect(result.next_cursor).toBe('cursor-contrato')
    expect(result.items.map(({ cause_code, cause_description }) => [cause_code, cause_description])).toEqual(canonicalQuarantineCauses)
    expect(result.items.every((row) => row.original_payload === null && row.row_number === null)).toBe(true)
  })
})

describe('ROBUSTNESS: catálogo exhaustivo y cerrado de cuarentena', () => {
  it('contiene exactamente los 25 códigos canónicos únicos en orden estable', () => {
    expect(quarantineCauses).toEqual(canonicalQuarantineCauses.map(([code]) => code))
    expect(new Set(quarantineCauses).size).toBe(25)
  })

  it('rechaza UNKNOWN en respuesta y filtro, sin sustituirlo por OTHER_CAUSE', () => {
    expect(() => quarantinePage({ items: [quarantineCauseItem('UNKNOWN', 'Causa desconocida')], next_cursor: null })).toThrow(ApiError)
    expect(() => filterQuery({ cause_code: 'UNKNOWN' }, 'quarantine')).toThrow(ApiError)
    expect(readFilters('cause_code=UNKNOWN', 'quarantine')).toEqual({})
  })
})
