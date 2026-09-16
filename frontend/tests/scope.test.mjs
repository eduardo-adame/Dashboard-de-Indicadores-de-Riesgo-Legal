/**
 * Pruebas de alcance del frontend.
 *
 * Protegen contra la introducción accidental de dependencias y de lógica que no
 * corresponden a esta etapa del frontend. Se ejecutan con el runner nativo de
 * Node sobre los archivos fuente, sin transformar JSX.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..')
const read = (rel) => readFileSync(join(frontendRoot, rel), 'utf8')

const pkg = JSON.parse(read('package.json'))
const allDeps = { ...pkg.dependencies, ...pkg.devDependencies }

/**
 * Elimina comentarios de bloque y de línea de una fuente JS/JSX.
 *
 * Es necesario para que las comprobaciones no produzcan falsos positivos con los
 * propios comentarios que documentan qué queda fuera de alcance.
 */
const stripComments = (source) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

test('robustez: no incorpora dependencias de visualización de datos', () => {
  const forbidden = ['recharts', 'chart.js', 'd3', 'visx', 'nivo', 'apexcharts']
  const present = forbidden.filter((dep) => dep in allDeps)
  assert.deepEqual(
    present,
    [],
    `Dependencias no previstas: ${present.join(', ')}`,
  )
})

test('robustez: no incorpora dependencias de autenticación ni de estado global', () => {
  const forbidden = [
    'react-router',
    'react-router-dom',
    'redux',
    '@reduxjs/toolkit',
    'zustand',
    'axios',
    'jose',
    'jwt-decode',
  ]
  const present = forbidden.filter((dep) => Object.keys(allDeps).some((d) => d.startsWith(dep)))
  assert.deepEqual(present, [], `Dependencias no previstas: ${present.join(', ')}`)
})

test('robustez: el build no expone rutas de API mediante proxy', () => {
  const config = read('vite.config.js')
  assert.match(config, /outDir:\s*'dist'/, 'build.outDir debe ser dist')
  assert.doesNotMatch(config, /proxy\s*:/, 'no debe configurarse proxy de API')
})

test('robustez: la vista declara que no implementa funcionalidad de negocio', () => {
  const app = read('src/App.jsx')
  assert.match(
    app,
    /proporciona únicamente la estructura mínima de arranque|no implementa funcionalidad de negocio/,
    'debe declarar explícitamente la ausencia de funcionalidad de negocio',
  )

  // La búsqueda se hace sobre el código, sin comentarios.
  const code = stripComments(app)
  const leaked = ['Recharts', 'LineChart', 'BarChart', 'ResponsiveContainer'].filter(
    (token) => code.includes(token),
  )
  assert.deepEqual(leaked, [], `Marcadores de visualización en el código: ${leaked.join(', ')}`)
})

test('robustez: nginx expone health propio y endurecimiento básico', () => {
  const nginx = read('nginx.conf')
  assert.match(nginx, /location = \/health/, 'debe existir un health de liveness propio')
  assert.match(nginx, /X-Content-Type-Options/, 'cabecera de endurecimiento ausente')
  assert.match(nginx, /X-Frame-Options/, 'cabecera de endurecimiento ausente')
  assert.match(nginx, /deny all/, 'debe denegar archivos ocultos')
})

test('robustez: no hay secretos en archivos fuente del frontend', () => {
  const sources = [
    'package.json',
    'vite.config.js',
    'nginx.conf',
    'index.html',
    'src/App.jsx',
    'src/main.jsx',
  ].filter((rel) => existsSync(join(frontendRoot, rel)))

  for (const rel of sources) {
    const text = read(rel)
    assert.doesNotMatch(
      text,
      /(api[_-]?key|secret|password|token)\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]/i,
      `Posible secreto en ${rel}`,
    )
  }
})
