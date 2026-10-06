import React, { useLayoutEffect, useRef } from 'react'
import { defineCustomElement } from 'ionicons/components/ion-icon.js'
import { menuOutline, closeOutline, logOutOutline, searchOutline, chevronUpOutline, chevronDownOutline, swapVerticalOutline, arrowBackOutline, checkmarkCircleOutline, warningOutline, lockClosedOutline, helpCircleOutline, gridOutline, documentTextOutline, briefcaseOutline, trendingUpOutline, cloudUploadOutline, fileTrayOutline, shieldCheckmarkOutline, listOutline } from 'ionicons/icons'
// Solo SVG oficiales locales; nunca nombres remotos ni texto recibido por API.
export const ICONS = Object.freeze({ menu: menuOutline, close: closeOutline, logout: logOutOutline, search: searchOutline, up: chevronUpOutline, down: chevronDownOutline, sort: swapVerticalOutline, back: arrowBackOutline, success: checkmarkCircleOutline, warning: warningOutline, locked: lockClosedOutline, help: helpCircleOutline, summary: gridOutline, document: documentTextOutline, litigation: briefcaseOutline, trend: trendingUpOutline, upload: cloudUploadOutline, quarantine: fileTrayOutline, administration: shieldCheckmarkOutline, audit: listOutline })
const navigationIcons = Object.freeze({ '/': 'summary', '/contratos': 'document', '/litigios': 'litigation', '/tendencias': 'trend', '/consulta': 'search', '/documentos': 'document', '/ingesta': 'upload', '/cuarentena': 'quarantine', '/auditoria': 'audit', '/administracion': 'administration' })
export function navigationIcon(path) { return navigationIcons[path] || 'help' }
defineCustomElement()
export function Icon({ name, size = 14, label }) {
  const ref = useRef(null)
  useLayoutEffect(() => { if (ref.current) ref.current.icon = ICONS[name] }, [name])
  if (!Object.hasOwn(ICONS, name)) return null
  return <span className="inline-flex shrink-0" role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true}><ion-icon ref={ref} aria-hidden="true" style={{ fontSize: size }} /></span>
}
