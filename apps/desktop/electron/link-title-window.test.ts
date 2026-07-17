import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { build } from 'esbuild'
import { test } from 'vitest'

import {
  configureLinkTitleSession,
  createLinkTitleWindow,
  guardLinkTitleSession,
  linkTitleWindowOptions,
  readLinkTitleWindowTitle
} from './link-title-window'

const require = createRequire(import.meta.url)
const ELECTRON_BINARY = require('electron') as string
const TEST_DIR = path.dirname(fileURLToPath(import.meta.url))

async function waitForFile(filePath: string, timeoutMs: number): Promise<string> {
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    try {
      return await readFile(filePath, 'utf8')
    } catch {
      await new Promise(resolve => setTimeout(resolve, 25))
    }
  }

  throw new Error(`Timed out waiting for Electron receipt: ${filePath}`)
}

async function waitForExit(child: ReturnType<typeof spawn>, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) {
    return
  }

  await Promise.race([
    new Promise<void>(resolve => child.once('exit', () => resolve())),
    new Promise<void>(resolve => setTimeout(resolve, timeoutMs))
  ])
}

function makeFakeBrowserWindow() {
  const calls = { audioMuted: [], destroyed: 0, webRtcPolicies: [], windowOpenHandlers: [] }

  const FakeBrowserWindow = function (options) {
    this.options = options

    this.destroy = () => {
      calls.destroyed += 1
    }

    this.webContents = {
      setAudioMuted(value) {
        calls.audioMuted.push(value)
      },
      setWindowOpenHandler(handler) {
        calls.windowOpenHandlers.push(handler)
      },
      setWebRTCIPHandlingPolicy(value) {
        calls.webRtcPolicies.push(value)
      }
    }
  }

  return { FakeBrowserWindow, calls }
}

test('linkTitleWindowOptions keeps the offscreen, hardened defaults', () => {
  const session = { id: 'link-titles' }
  const options = linkTitleWindowOptions(session)

  assert.equal(options.show, false)
  assert.equal(options.webPreferences.session, session)
  assert.equal(options.webPreferences.contextIsolation, true)
  assert.equal(options.webPreferences.sandbox, true)
  assert.equal(options.webPreferences.nodeIntegration, false)
})

test('createLinkTitleWindow mutes audio so historical links never autoplay sound', () => {
  // Regression for #49505: the hidden title-fetch window loaded YouTube/watch
  // URLs (to read their <title>) without muting, leaking ~2s of audio on every
  // history re-render.
  const { FakeBrowserWindow, calls } = makeFakeBrowserWindow()

  const window = createLinkTitleWindow(FakeBrowserWindow, { id: 'link-titles' })

  assert.ok(window instanceof FakeBrowserWindow)
  assert.deepEqual(calls.audioMuted, [true])
  assert.deepEqual(calls.webRtcPolicies, ['disable_non_proxied_udp'])
  assert.equal(calls.windowOpenHandlers.length, 1)
  assert.deepEqual(calls.windowOpenHandlers[0](), { action: 'deny' })
})

test('createLinkTitleWindow still returns the window if muting throws', () => {
  const ThrowingBrowserWindow = function (options) {
    this.options = options
    this.webContents = {
      setAudioMuted() {
        throw new Error('webContents unavailable')
      },
      setWindowOpenHandler() {},
      setWebRTCIPHandlingPolicy() {}
    }
  }

  const window = createLinkTitleWindow(ThrowingBrowserWindow, { id: 'link-titles' })

  assert.ok(window instanceof ThrowingBrowserWindow)
})

test('createLinkTitleWindow fails closed when non-proxied WebRTC cannot be disabled', () => {
  let destroyed = false

  const ThrowingBrowserWindow = function (options) {
    this.options = options

    this.destroy = () => {
      destroyed = true
    }

    this.webContents = {
      setAudioMuted() {},
      setWindowOpenHandler() {},
      setWebRTCIPHandlingPolicy() {
        throw new Error('WebRTC policy unavailable')
      }
    }
  }

  assert.throws(
    () => createLinkTitleWindow(ThrowingBrowserWindow, { id: 'link-titles' }),
    /WebRTC policy unavailable/
  )
  assert.equal(destroyed, true)
})

test('createLinkTitleWindow fails closed when popup creation cannot be denied', () => {
  let destroyed = false

  const ThrowingBrowserWindow = function (options) {
    this.options = options

    this.destroy = () => {
      destroyed = true
    }

    this.webContents = {
      setAudioMuted() {},
      setWindowOpenHandler() {
        throw new Error('window-open policy unavailable')
      },
      setWebRTCIPHandlingPolicy() {}
    }
  }

  assert.throws(
    () => createLinkTitleWindow(ThrowingBrowserWindow, { id: 'link-titles' }),
    /window-open policy unavailable/
  )
  assert.equal(destroyed, true)
})

test('guardLinkTitleSession cancels downloads triggered by the title-fetch window', () => {
  let cancelled = false
  const handlers = {}
  guardLinkTitleSession(
    {
      on: (e, h) => {
        handlers[e] = h
      },
      webRequest: { onBeforeRequest() {} }
    },
    value => value
  )
  handlers['will-download'](null, {
    cancel: () => {
      cancelled = true
    }
  })
  assert.ok(cancelled)
})

test('configureLinkTitleSession installs the fixed SOCKS proxy before request guards', async () => {
  const calls = []

  const partitionSession = {
    on: () => calls.push('download-guard'),
    setProxy: async config => calls.push(['setProxy', config]),
    webRequest: { onBeforeRequest: () => calls.push('request-guard') }
  }

  await configureLinkTitleSession(partitionSession, value => value, 'socks5://127.0.0.1:48123')

  assert.deepEqual(calls, [
    [
      'setProxy',
      {
        mode: 'fixed_servers',
        proxyBypassRules: '<-loopback>',
        proxyRules: 'socks5://127.0.0.1:48123'
      }
    ],
    'request-guard',
    'download-guard'
  ])
})

test('configureLinkTitleSession fails before installing guards when proxy setup fails', async () => {
  let guarded = false

  const partitionSession = {
    on: () => {
      guarded = true
    },
    setProxy: async () => {
      throw new Error('proxy unavailable')
    },
    webRequest: {
      onBeforeRequest: () => {
        guarded = true
      }
    }
  }

  await assert.rejects(
    configureLinkTitleSession(partitionSession, value => value, 'socks5://127.0.0.1:48123'),
    /proxy unavailable/
  )
  assert.equal(guarded, false)
})

test('configureLinkTitleSession times out instead of holding a renderer queue slot forever', async () => {
  const partitionSession = {
    on() {},
    setProxy: () => new Promise(() => undefined),
    webRequest: { onBeforeRequest() {} }
  }

  const result = await Promise.race([
    configureLinkTitleSession(partitionSession, value => value, 'socks5://127.0.0.1:48123', 20).then(
      () => 'resolved',
      error => String(error?.message || error)
    ),
    new Promise<string>(resolve => setTimeout(() => resolve('still pending'), 200))
  ])

  assert.match(result, /timed out/i)
})

test('guardLinkTitleSession blocks rejected redirects and subrequests while allowing public HTTP(S)', () => {
  let beforeRequest

  const admitUrl = value => {
    const url = new URL(value)

    if (!['http:', 'https:'].includes(url.protocol) || ['10.0.0.1', '127.0.0.1'].includes(url.hostname)) {
      return null
    }

    return url.href
  }

  guardLinkTitleSession(
    {
      on() {},
      webRequest: {
        onBeforeRequest(handler) {
          beforeRequest = handler
        }
      }
    },
    admitUrl
  )

  const cancelled = (url, resourceType) => {
    let result
    beforeRequest({ resourceType, url }, value => {
      result = value.cancel
    })

    return result
  }

  assert.equal(cancelled('http://127.0.0.1/redirect', 'mainFrame'), true)
  assert.equal(cancelled('http://10.0.0.1/data', 'xhr'), true)
  assert.equal(cancelled('file:///tmp/private', 'mainFrame'), true)
  assert.equal(cancelled('https://example.com/', 'mainFrame'), false)
  assert.equal(cancelled('http://cdn.example.com/app.js', 'script'), false)
  assert.equal(cancelled('https://example.com/site.css', 'stylesheet'), true)
})

test('guardLinkTitleSession is a no-op when session.on throws', () => {
  assert.doesNotThrow(() =>
    guardLinkTitleSession(
      {
        on() {
          throw new Error()
        },
        webRequest: { onBeforeRequest() {} }
      },
      value => value
    )
  )
})

test('readLinkTitleWindowTitle returns empty for missing or destroyed windows', () => {
  assert.equal(readLinkTitleWindowTitle(null), '')
  assert.equal(readLinkTitleWindowTitle(undefined), '')
  assert.equal(readLinkTitleWindowTitle({ isDestroyed: () => true }), '')
})

test('readLinkTitleWindowTitle returns empty when webContents is destroyed', () => {
  const window = {
    isDestroyed: () => false,
    webContents: { isDestroyed: () => true, getTitle: () => 'Should Not Read' }
  }

  assert.equal(readLinkTitleWindowTitle(window), '')
})

test('readLinkTitleWindowTitle swallows getTitle throws after teardown', () => {
  const window = {
    isDestroyed: () => false,
    webContents: {
      isDestroyed: () => false,
      getTitle: () => {
        throw new Error('Object has been destroyed')
      }
    }
  }

  assert.equal(readLinkTitleWindowTitle(window), '')
})

test('real Electron hidden BrowserWindow routes through SOCKS and fails closed when proxy establishment fails', async () => {
  const tempRoot = await mkdtemp(path.join(tmpdir(), 'hermes-link-title-electron-e2e-'))
  const bundlePath = path.join(tempRoot, 'link-title-window.e2e.mjs')
  const receiptPath = path.join(tempRoot, 'receipt.json')
  const userDataPath = path.join(tempRoot, 'user-data')
  let child: ReturnType<typeof spawn> | null = null
  let stderr = ''

  try {
    await build({
      bundle: true,
      entryPoints: [path.join(TEST_DIR, 'link-title-window.e2e.ts')],
      external: ['electron'],
      format: 'esm',
      outfile: bundlePath,
      platform: 'node',
      target: 'node20'
    })

    child = spawn(ELECTRON_BINARY, ['--no-sandbox', `--user-data-dir=${userDataPath}`, bundlePath], {
      env: { ...process.env, HERMES_LINK_TITLE_E2E_RECEIPT: receiptPath },
      stdio: ['ignore', 'ignore', 'pipe']
    })
    child.stderr?.on('data', chunk => {
      stderr += String(chunk)
    })

    const receipt = JSON.parse(await waitForFile(receiptPath, 10_000))

    assert.deepEqual(receipt.proxied, {
      host: `public-title.invalid:${receipt.port}`,
      title: 'Pinned BrowserWindow title'
    })
    assert.deepEqual(receipt.proxyFailure, { failed: true, requestCount: 0 })
  } catch (error) {
    throw new Error(`${error instanceof Error ? error.message : String(error)}\nElectron stderr:\n${stderr}`)
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM')
      await waitForExit(child, 2_000)
    }

    await rm(tempRoot, { force: true, recursive: true })
  }
}, 15_000)

test('readLinkTitleWindowTitle returns trimmed page title', () => {
  const window = {
    isDestroyed: () => false,
    webContents: {
      isDestroyed: () => false,
      getTitle: () => 'Example Domain'
    }
  }

  assert.equal(readLinkTitleWindowTitle(window), 'Example Domain')
})
