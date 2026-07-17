import fs from 'node:fs'
import http from 'node:http'
import type net from 'node:net'

import { app, BrowserWindow, session } from 'electron'

import { startLinkTitleSocksGateway } from './link-title-socks'
import { configureLinkTitleSession, createLinkTitleWindow, readLinkTitleWindowTitle } from './link-title-window'

const receiptPath = process.env.HERMES_LINK_TITLE_E2E_RECEIPT

if (!receiptPath) {
  throw new Error('HERMES_LINK_TITLE_E2E_RECEIPT is required')
}

function listen(server: net.Server | http.Server): Promise<number> {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()

      if (!address || typeof address === 'string') {
        reject(new Error('server did not bind a TCP port'))

        return
      }

      resolve(address.port)
    })
  })
}

function close(server: net.Server | http.Server): Promise<void> {
  return new Promise(resolve => server.close(() => resolve()))
}

async function loadTitle(window: BrowserWindow, url: string): Promise<string> {
  await window.loadURL(url)
  await new Promise(resolve => setTimeout(resolve, 25))

  return readLinkTitleWindowTitle(window)
}

async function run(): Promise<void> {
  await app.whenReady()

  let requestCount = 0

  const target = http.createServer((request, response) => {
    requestCount += 1
    response.setHeader('content-type', 'text/html')
    response.end('<title>Pinned BrowserWindow title</title>')
  })

  const port = await listen(target)

  const gateway = await startLinkTitleSocksGateway({
    connectTimeoutMs: 1_000,
    resolve: async hostname => {
      if (hostname !== 'public-title.invalid') {
        throw new Error(`unexpected hostname: ${hostname}`)
      }

      return [{ address: '127.0.0.1', family: 4 }]
    }
  })

  const partition = `link-title-e2e-${process.pid}-${Date.now()}`
  const partitionSession = session.fromPartition(partition, { cache: false })
  const window = createLinkTitleWindow(BrowserWindow, partitionSession)

  try {
    await configureLinkTitleSession(partitionSession, value => value, gateway.proxyUrl, 1_000)
    const title = await loadTitle(window, `http://public-title.invalid:${port}/title`)

    if (title !== 'Pinned BrowserWindow title' || requestCount !== 1) {
      throw new Error(`proxied BrowserWindow request failed: title=${JSON.stringify(title)}, requests=${requestCount}`)
    }

    const failedSession = session.fromPartition(`${partition}-failed`, { cache: false })
    const failedWindow = createLinkTitleWindow(BrowserWindow, failedSession)

    try {
      await configureLinkTitleSession(failedSession, value => value, 'socks5://127.0.0.1:1', 1_000)
      requestCount = 0

      const loadFailed = await failedWindow.loadURL(`http://127.0.0.1:${port}/title`).then(
        () => false,
        () => true
      )

      if (!loadFailed || requestCount !== 0) {
        throw new Error(`proxy failure did not fail closed: failed=${loadFailed}, requests=${requestCount}`)
      }
    } finally {
      if (!failedWindow.isDestroyed()) {
        failedWindow.destroy()
      }
    }

    fs.writeFileSync(
      receiptPath,
      JSON.stringify({
        port,
        proxied: { host: `public-title.invalid:${port}`, title },
        proxyFailure: { failed: true, requestCount }
      })
    )
  } finally {
    if (!window.isDestroyed()) {
      window.destroy()
    }

    await gateway.close()
    await close(target)
    app.exit(0)
  }
}

run().catch(error => {
  fs.writeFileSync(receiptPath, JSON.stringify({ error: error instanceof Error ? error.stack || error.message : String(error) }))
  app.exit(1)
})
