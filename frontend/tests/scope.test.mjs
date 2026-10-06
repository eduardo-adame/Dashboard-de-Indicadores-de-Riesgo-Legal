/** Controles de alcance y transporte; las fixtures HTTP son exclusivamente sintéticas. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createServer as createHttpServer } from 'node:http'
import { request as httpRequest } from 'node:http'
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { once } from 'node:events'
import { createServer as createViteServer } from 'vite'
import postcss from 'postcss'
import tailwind from 'tailwindcss'
import tailwindConfig from '../tailwind.config.js'
import config, { apiProxy } from '../vite.config.js'
const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (path) => readFileSync(join(root, path), 'utf8')
const pkg = JSON.parse(read('package.json'))
test('ROBUSTNESS: dependencias exactas aprobadas y sin bibliotecas alternativas', () => {
  const expected = { react: '18.3.1', 'react-dom': '18.3.1', 'react-router-dom': '7.18.4', recharts: '3.10.1', 'react-is': '18.3.1', ionicons: '8.1.0', '@fontsource/geist': '5.3.0', '@fontsource/geist-mono': '5.3.0', '@vitejs/plugin-react': '4.3.4', vite: '6.4.3', tailwindcss: '3.4.19', postcss: '8.5.29', autoprefixer: '10.6.1', vitest: '4.1.11', jsdom: '26.1.0', '@testing-library/react': '16.3.3', '@testing-library/dom': '10.4.1', '@testing-library/jest-dom': '7.0.1', '@testing-library/user-event': '14.6.7' }
  assert.deepEqual({ ...pkg.dependencies, ...pkg.devDependencies }, expected)
  assert.equal(pkg.overrides, undefined)
  for (const name of ['tailwindcss', 'postcss', 'autoprefixer', 'vite', 'vitest', '@testing-library/react']) assert.ok(name in pkg.devDependencies && !(name in pkg.dependencies))
})
test('ROBUSTNESS: lockfile contiene las versiones exactas sin Tinypool', () => {
  const lock = JSON.parse(read('package-lock.json'))
  for (const [name, version] of Object.entries({ ...pkg.dependencies, ...pkg.devDependencies })) assert.equal(lock.packages[`node_modules/${name}`].version, version)
  assert.ok(!Object.keys(lock.packages).some((path) => /(^|\/)tinypool$/.test(path)))
})
test('SRS_REQUIRED: preserva health y protección Nginx y proxifica API sin cache', () => {
  const nginx = read('nginx.conf')
  assert.match(nginx, /location = \/health/); assert.match(nginx, /X-Content-Type-Options/); assert.match(nginx, /X-Frame-Options/); assert.match(nginx, /deny all/)
  assert.match(nginx, /location \/api\//); assert.match(nginx, /proxy_pass http:\/\/backend:8000;/); assert.match(nginx, /proxy_cache off/); assert.match(nginx, /access_log off/); assert.match(nginx, /Cache-Control "no-store"/)
  assert.doesNotMatch(nginx, /proxy_cookie_path|proxy_cookie_flags|proxy_hide_header Set-Cookie/)
})
test('ROBUSTNESS: límite multipart y presupuestos largos permanecen acotados', () => {
  const nginx = read('nginx.conf')
  assert.match(nginx, /client_max_body_size 64m/); assert.match(nginx, /proxy_read_timeout 420s/); assert.match(nginx, /proxy_send_timeout 420s/)
  assert.equal(apiProxy().proxyTimeout, 420000); assert.equal(apiProxy().timeout, 420000)
})
test('ROBUSTNESS: servidores dev/test locales sin browser o interfaz de pruebas', () => {
  assert.equal(config.server.host, '127.0.0.1'); assert.equal(config.preview.host, '127.0.0.1')
  assert.equal(config.test.browser, undefined); assert.equal(config.test.api, undefined)
  assert.throws(() => apiProxy('http://usuario:clave@localhost:8000')); assert.throws(() => apiProxy('http://localhost:8000/api'))
})
test('ROBUSTNESS: Docker runtime solo copia dist sin Node ni fuentes', () => {
  const file = read('Dockerfile'); const runtime = file.split('FROM nginx:')[1]
  assert.match(file, /npm ci/); assert.match(file, /npm run build/); assert.match(runtime, /COPY --from=build \/build\/dist/)
  assert.doesNotMatch(runtime, /COPY.*(?:node_modules|package|src)|npm|node:/)
})
test('SRS_REQUIRED: frontend autocontenido sin fuentes jurídicas inventadas ni tooling HTTP', () => {
  const sources = []
  function collect(path) { for (const entry of readdirSync(join(root, path), { withFileTypes: true })) { const relative = `${path}/${entry.name}`; if (entry.isDirectory()) collect(relative); else if (/\.(js|jsx|css)$/.test(entry.name)) sources.push(relative) } }
  collect('src')
  for (const path of sources) {
    const text = read(path)
    assert.doesNotMatch(text, /(?:from\s+|@import\s+)['"][^'"]*(?:handoffs|Design\/|docs\/|tailwindcss|postcss|braces|selector-parser)/)
    assert.doesNotMatch(text, /dangerouslySetInnerHTML|localStorage|sessionStorage|document\.cookie/)
    assert.doesNotMatch(text, /(api[_-]?key|secret|password|token)\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]/i)
    assert.doesNotMatch(text, /(?:Codex|OpenCode|ChatGPT|orchestrator)/i)
  }
  assert.match(read('src/routing/registry.jsx'), /import\.meta\.glob\('\.\.\/features\/\*\/routes\.jsx'/)
  assert.doesNotMatch(read('src/App.jsx'), /LineChart|BarChart|ResponsiveContainer|VITE_BACKEND_HEALTH_URL/)
})
test('SRS_REQUIRED: dimensiones visuales canónicas y responsive sin aproximaciones', () => {
  const tokens = read('src/design/tokens.css'); const styles = read('src/styles.css')
  for (const [name, value] of Object.entries({ '--sidebar-width': '256px', '--topbar-height': '52px', '--control-height-default': '40px', '--control-height-compact': '32px', '--radius-small': '6px', '--radius-medium': '10px', '--radius-large': '14px', '--warning': '#B45309' })) assert.ok(tokens.includes(`${name}: ${value};`))
  assert.match(styles, /height: 36px/); assert.match(styles, /grid-template-columns: minmax\(0, 1fr\)/); assert.match(styles, /min-width: 768px/); assert.match(tokens, /prefers-reduced-motion: reduce/)
  assert.match(styles, /@fontsource\/geist\/latin-400.css/); assert.match(styles, /@fontsource\/geist-mono/)
})

test('SRS_REQUIRED: CSS compilado conserva colores de todas las variantes de botón', async () => {
  const result = await postcss([tailwind(tailwindConfig)]).process(read('src/styles.css'), { from: join(root, 'src/styles.css') })
  const declarations = (selector) => {
    const values = {}
    result.root.walkRules(selector, (rule) => rule.walkDecls((declaration) => { values[declaration.prop] = declaration.value }))
    return values
  }
  assert.equal(declarations('.button-primary').background, 'var(--text-primary)')
  assert.equal(declarations('.button-primary').color, 'var(--background-primary)')
  assert.equal(declarations('.button-secondary').background, 'var(--background-secondary)')
  assert.equal(declarations('.button-danger').background, 'var(--critical-subtle)')
  assert.equal(declarations('.button-ghost').color, 'var(--text-secondary)')
})
test('SRS_REQUIRED: proxy Vite conserva ruta, cookies, bearer, multipart y respuesta204', { timeout: 30000 }, async () => {
  const received = []
  const upstream = createHttpServer(async (request, response) => {
    let size = 0; for await (const chunk of request) size += chunk.length
    received.push({ path: request.url, method: request.method, cookie: request.headers.cookie, authorization: request.headers.authorization, key: request.headers['idempotency-key'], contentType: request.headers['content-type'], size })
    response.setHeader('Cache-Control', 'no-store')
    if (request.url.endsWith('/logout')) { response.setHeader('Set-Cookie', 'refresh_fixture=; Max-Age=0; Path=/api/auth; HttpOnly; SameSite=Strict; Secure'); response.writeHead(204); response.end(); return }
    if (request.url.endsWith('/login') || request.url.endsWith('/refresh')) response.setHeader('Set-Cookie', `refresh_fixture=${request.url.endsWith('/refresh') ? 'rotated' : 'initial'}; Path=/api/auth; HttpOnly; SameSite=Strict; Secure`)
    response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ synthetic: true }))
  })
  let vite
  try {
    upstream.listen(0, '127.0.0.1'); await once(upstream, 'listening')
    vite = await createViteServer({ configFile: false, server: { host: '127.0.0.1', port: 0, proxy: { '/api': apiProxy(`http://127.0.0.1:${upstream.address().port}`) } }, logLevel: 'silent' })
    await vite.listen(); const base = `http://127.0.0.1:${vite.httpServer.address().port}`
    const login = await fetch(`${base}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'fixture', password: 'synthetic' }) })
    assert.match(login.headers.get('set-cookie'), /initial.*Path=\/api\/auth; HttpOnly; SameSite=Strict; Secure/)
    const refresh = await fetch(`${base}/api/auth/refresh`, { method: 'POST', headers: { Cookie: 'refresh_fixture=initial' } })
    assert.match(refresh.headers.get('set-cookie'), /rotated/)
    const multipart = new FormData(); multipart.append('file', new Blob(['fixture']), 'synthetic.txt'); multipart.append('controlled_location', 'contracts-documents')
    await fetch(`${base}/api/ingestion/uploads`, { method: 'POST', body: multipart, headers: { Authorization: 'Bearer synthetic', 'Idempotency-Key': '11111111-1111-4111-8111-111111111111' } })
    const logout = await fetch(`${base}/api/auth/logout`, { method: 'POST', headers: { Authorization: 'Bearer synthetic' } })
    assert.equal(logout.status, 204); assert.match(logout.headers.get('set-cookie'), /Max-Age=0/)
    assert.equal(received[1].cookie, 'refresh_fixture=initial'); assert.equal(received[2].path, '/api/ingestion/uploads'); assert.equal(received[2].authorization, 'Bearer synthetic'); assert.match(received[2].contentType, /multipart\/form-data; boundary=/); assert.ok(received[2].size > 7); assert.equal(received[2].key, '11111111-1111-4111-8111-111111111111')
  } finally { if (vite) await vite.close(); upstream.closeAllConnections(); if (upstream.listening) await new Promise((resolve) => upstream.close(resolve)) }
})

// Gate independiente obligatorio: ejecutarlo con la imagen construida del mismo corte.
// No inicia servicios productivos ni utiliza cuentas o datos reales.
if (process.env.FOUNDATION_NGINX_IMAGE) test('SRS_REQUIRED: proxy Nginx real conserva cookies, multipart y presupuestos largos', { timeout: 240000 }, async () => {
  assert.equal(process.env.FOUNDATION_NGINX_IMAGE, 'riesgo-legal-foundation-review:local')
  const suffix = randomUUID().slice(0, 8)
  const network = `foundation-proxy-${suffix}`; const upstream = `${network}-upstream`; const proxy = `${network}-nginx`
  const created = []
  const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8', timeout: 60000, stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  const script = `
    const http = require('node:http');
    http.createServer(async (req, res) => {
      let size = 0; for await (const chunk of req) size += chunk.length;
      if (req.url.endsWith('/slow')) await new Promise(done => setTimeout(done, 61000));
      res.setHeader('Content-Type', 'application/json');
      if (req.url.endsWith('/login') || req.url.endsWith('/refresh')) res.setHeader('Set-Cookie', 'refresh_fixture=' + (req.url.endsWith('/refresh') ? 'rotated' : 'initial') + '; Path=/api/auth; HttpOnly; SameSite=Strict; Secure');
      if (req.url.endsWith('/logout')) { res.setHeader('Set-Cookie', 'refresh_fixture=; Max-Age=0; Path=/api/auth; HttpOnly; SameSite=Strict; Secure'); res.writeHead(204); res.end(); return; }
      res.end(JSON.stringify({ path: req.url, cookie: req.headers.cookie, authorization: req.headers.authorization, key: req.headers['idempotency-key'], contentType: req.headers['content-type'], size }));
    }).listen(8000, '0.0.0.0');
  `
  let networkCreated = false
  try {
    // La publicación loopback requiere un bridge normal en Docker Desktop; no expone la fixture a la red local.
    docker('network', 'create', '--label', 'purpose=foundation-synthetic-test', network); networkCreated = true
    docker('run', '-d', '--name', upstream, '--label', 'purpose=foundation-synthetic-test', '--network', network, '--network-alias', 'backend', '--read-only', '--cap-drop', 'ALL', 'node:22-alpine', 'node', '-e', script); created.push(upstream)
    docker('run', '-d', '--name', proxy, '--label', 'purpose=foundation-synthetic-test', '--network', network, '-p', '127.0.0.1::80', process.env.FOUNDATION_NGINX_IMAGE); created.push(proxy)
    const port = docker('port', proxy, '80/tcp'); assert.match(port, /^127\.0\.0\.1:\d+$/)
    const base = `http://${port}`
    let ready = false
    for (let attempt = 0; attempt < 60 && !ready; attempt++) {
      try { const health = await fetch(`${base}/health`, { signal: AbortSignal.timeout(1000) }); ready = health.status === 200 } catch {}
      if (!ready) await new Promise((done) => setTimeout(done, 250))
    }
    assert.ok(ready, 'El proxy temporal debe responder health antes del gate.')
    const login = await fetch(`${base}/api/auth/login`, { method: 'POST', body: JSON.stringify({ username: 'fixture', password: 'synthetic' }), headers: { 'Content-Type': 'application/json' } })
    assert.equal(login.status, 200); assert.match(login.headers.get('set-cookie'), /initial; Path=\/api\/auth; HttpOnly; SameSite=Strict; Secure/); assert.equal(login.headers.get('cache-control'), 'no-store'); await login.json()
    const refresh = await fetch(`${base}/api/auth/refresh`, { method: 'POST', headers: { Cookie: 'refresh_fixture=initial' } })
    assert.match(refresh.headers.get('set-cookie'), /rotated/); assert.equal((await refresh.json()).cookie, 'refresh_fixture=initial')
    const multipart = new FormData(); multipart.append('file', new Blob([new Uint8Array(50 * 1024 * 1024)]), 'synthetic.bin'); multipart.append('controlled_location', 'contracts-documents')
    const upload = await fetch(`${base}/api/ingestion/uploads`, { method: 'POST', body: multipart, headers: { Authorization: 'Bearer synthetic', 'Idempotency-Key': '11111111-1111-4111-8111-111111111111' }, signal: AbortSignal.timeout(90000) })
    assert.equal(upload.status, 200); const received = await upload.json()
    assert.equal(received.path, '/api/ingestion/uploads'); assert.equal(received.authorization, 'Bearer synthetic'); assert.equal(received.key, '11111111-1111-4111-8111-111111111111'); assert.match(received.contentType, /multipart\/form-data; boundary=/); assert.ok(received.size > 50 * 1024 * 1024 && received.size < 64 * 1024 * 1024)
    const excess = await fetch(`${base}/api/ingestion/uploads`, { method: 'POST', body: new Uint8Array(64 * 1024 * 1024 + 1), signal: AbortSignal.timeout(90000) })
    assert.equal(excess.status, 413); await excess.text()
    const delayedUpload = new Promise((resolve, reject) => {
      const request = httpRequest(`${base}/api/upload-slow`, { method: 'POST', headers: { 'Content-Length': '2', 'Content-Type': 'application/octet-stream' } }, (response) => {
        let text = ''; response.on('data', (chunk) => { text += chunk }); response.on('end', () => { try { assert.equal(response.statusCode, 200); assert.equal(JSON.parse(text).size, 2); resolve() } catch (error) { reject(error) } })
      })
      const finish = setTimeout(() => request.end('b'), 61000)
      request.on('error', (error) => { clearTimeout(finish); reject(error) }); request.setTimeout(90000, () => request.destroy(new Error('La subida sintética superó el presupuesto de prueba.'))); request.write('a')
    })
    await Promise.all([delayedUpload, (async () => { const slow = await fetch(`${base}/api/slow`, { signal: AbortSignal.timeout(90000) }); assert.equal(slow.status, 200); await slow.json() })()])
    const logout = await fetch(`${base}/api/auth/logout`, { method: 'POST', headers: { Authorization: 'Bearer synthetic', Cookie: 'refresh_fixture=rotated' } })
    assert.equal(logout.status, 204); assert.match(logout.headers.get('set-cookie'), /Max-Age=0; Path=\/api\/auth; HttpOnly; SameSite=Strict; Secure/)
    assert.equal((await fetch(`${base}/.env`)).status, 403)
  } finally {
    for (const name of created.reverse()) { assert.equal(docker('inspect', '--format', '{{index .Config.Labels "purpose"}}', name), 'foundation-synthetic-test'); docker('rm', '-f', name) }
    if (networkCreated) { assert.equal(docker('network', 'inspect', '--format', '{{index .Labels "purpose"}}', network), 'foundation-synthetic-test'); docker('network', 'rm', network) }
  }
})
