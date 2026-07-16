import net, { type Socket } from 'node:net'

import type { LinkTitleAddress } from './link-title-dns'

export interface LinkTitleSocksGateway {
  proxyUrl: string
  close(): Promise<void>
}

export interface LinkTitleSocksGatewayController {
  close(): Promise<void>
  get(): Promise<LinkTitleSocksGateway>
}

export function createLinkTitleSocksGatewayController(options: {
  clearPins(): void
  start(): Promise<LinkTitleSocksGateway>
}): LinkTitleSocksGatewayController {
  let pending: Promise<LinkTitleSocksGateway> | null = null

  return {
    async close() {
      const current = pending
      pending = null

      try {
        await (await current?.catch(() => null))?.close()
      } finally {
        options.clearPins()
      }
    },
    get() {
      if (!pending) {
        const attempt = options.start()

        const guarded = attempt.catch(error => {
          if (pending === guarded) {
            pending = null
          }

          throw error
        })

        pending = guarded
      }

      return pending
    }
  }
}

const MAX_HANDSHAKE_BYTES = 1_024
const SOCKS_REPLY_SIZE = 10

function reply(code: number): Buffer {
  return Buffer.from([0x05, code, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
}

function ipv6FromBytes(value: Buffer): string {
  const words: string[] = []

  for (let offset = 0; offset < value.length; offset += 2) {
    words.push(value.readUInt16BE(offset).toString(16))
  }

  return words.join(':')
}

async function connectToApprovedAddress(
  addresses: readonly LinkTitleAddress[],
  port: number,
  connectTimeoutMs: number,
  createConnection: typeof net.createConnection
): Promise<Socket> {
  let lastError: unknown = new Error('Link title SOCKS resolver returned no approved addresses')

  for (const target of addresses) {
    try {
      const upstream = createConnection({ family: target.family, host: target.address, port })

      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {
          upstream.destroy()
          reject(new Error('Link title SOCKS upstream connection timed out'))
        }, connectTimeoutMs)

        const cleanup = () => {
          clearTimeout(timer)
          upstream.off('connect', onConnect)
          upstream.off('error', onError)
        }

        const onConnect = () => {
          cleanup()
          resolve()
        }

        const onError = (error: Error) => {
          cleanup()
          reject(error)
        }

        upstream.once('connect', onConnect)
        upstream.once('error', onError)
      })

      return upstream
    } catch (error) {
      lastError = error
    }
  }

  throw lastError
}

function handleClient(
  client: Socket,
  options: {
    resolve(hostname: string): Promise<readonly LinkTitleAddress[]>
    connectTimeoutMs: number
    createConnection: typeof net.createConnection
  },
  peers: Set<Socket>
): void {
  let buffer = Buffer.alloc(0)
  let phase: 'greeting' | 'request' | 'connecting' | 'relay' | 'closed' = 'greeting'
  let processing = false
  let upstream: Socket | null = null

  peers.add(client)

  const handshakeTimer = setTimeout(() => {
    if (phase !== 'relay' && phase !== 'closed') {
      phase = 'closed'
      client.destroy()
    }
  }, options.connectTimeoutMs)

  const closeWithReply = (code: number) => {
    if (phase === 'closed') {
      return
    }

    phase = 'closed'
    clearTimeout(handshakeTimer)
    client.end(reply(code))
  }

  const beginRelay = async (hostname: string, port: number, pending: Buffer) => {
    try {
      const addresses = await options.resolve(hostname)
      upstream = await connectToApprovedAddress(
        addresses,
        port,
        options.connectTimeoutMs,
        options.createConnection
      )
    } catch {
      closeWithReply(0x04)

      return
    }

    if (phase === 'closed' || client.destroyed) {
      upstream.destroy()

      return
    }

    phase = 'relay'
    clearTimeout(handshakeTimer)
    peers.add(upstream)

    upstream.once('close', () => {
      peers.delete(upstream as Socket)
      client.destroy()
    })
    upstream.once('error', () => client.destroy())
    client.once('error', () => upstream?.destroy())

    client.write(reply(0x00))

    if (pending.length) {
      upstream.write(pending)
    }

    client.pipe(upstream)
    upstream.pipe(client)
    client.resume()
  }

  const processBuffer = () => {
    if (processing || phase === 'connecting' || phase === 'relay' || phase === 'closed') {
      return
    }

    processing = true

    try {
      while (phase === 'greeting' || phase === 'request') {
        if (phase === 'greeting') {
          if (buffer.length < 2) {
            return
          }

          const methodCount = buffer[1]
          const greetingLength = 2 + methodCount

          if (buffer[0] !== 0x05 || methodCount === 0 || greetingLength > MAX_HANDSHAKE_BYTES) {
            phase = 'closed'
            clearTimeout(handshakeTimer)
            client.destroy()

            return
          }

          if (buffer.length < greetingLength) {
            return
          }

          const methods = buffer.subarray(2, greetingLength)
          buffer = buffer.subarray(greetingLength)

          if (!methods.includes(0x00)) {
            phase = 'closed'
            clearTimeout(handshakeTimer)
            client.end(Buffer.from([0x05, 0xff]))

            return
          }

          client.write(Buffer.from([0x05, 0x00]))
          phase = 'request'
        }

        if (phase !== 'request' || buffer.length < 4) {
          return
        }

        if (buffer[0] !== 0x05 || buffer[2] !== 0x00) {
          closeWithReply(0x01)

          return
        }

        if (buffer[1] !== 0x01) {
          closeWithReply(0x07)

          return
        }

        const addressType = buffer[3]
        let addressLength: number
        let addressOffset: number

        if (addressType === 0x01) {
          addressOffset = 4
          addressLength = 4
        } else if (addressType === 0x04) {
          addressOffset = 4
          addressLength = 16
        } else if (addressType === 0x03) {
          if (buffer.length < 5) {
            return
          }

          addressOffset = 5
          addressLength = buffer[4]

          if (addressLength === 0) {
            closeWithReply(0x08)

            return
          }
        } else {
          closeWithReply(0x08)

          return
        }

        const requestLength = addressOffset + addressLength + 2

        if (requestLength > MAX_HANDSHAKE_BYTES) {
          phase = 'closed'
          clearTimeout(handshakeTimer)
          client.destroy()

          return
        }

        if (buffer.length < requestLength) {
          return
        }

        const rawAddress = buffer.subarray(addressOffset, addressOffset + addressLength)

        const hostname =
          addressType === 0x01
            ? [...rawAddress].join('.')
            : addressType === 0x04
              ? ipv6FromBytes(rawAddress)
              : rawAddress.toString('ascii')

        const port = buffer.readUInt16BE(addressOffset + addressLength)
        const pending = buffer.subarray(requestLength)
        buffer = Buffer.alloc(0)
        phase = 'connecting'
        client.pause()
        void beginRelay(hostname, port, pending)

        return
      }
    } finally {
      processing = false
    }
  }

  client.on('data', chunk => {
    if (phase !== 'greeting' && phase !== 'request') {
      return
    }

    const next = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)

    if (buffer.length + next.length > MAX_HANDSHAKE_BYTES) {
      phase = 'closed'
      clearTimeout(handshakeTimer)
      client.destroy()

      return
    }

    buffer = Buffer.concat([buffer, next])
    processBuffer()
  })
  client.once('close', () => {
    phase = 'closed'
    clearTimeout(handshakeTimer)
    peers.delete(client)
    upstream?.destroy()
  })
  client.once('error', () => undefined)
}

export async function startLinkTitleSocksGateway(options: {
  resolve(hostname: string): Promise<readonly LinkTitleAddress[]>
  connectTimeoutMs: number
  createConnection?: typeof net.createConnection
}): Promise<LinkTitleSocksGateway> {
  const peers = new Set<Socket>()
  const createConnection = options.createConnection ?? net.createConnection

  const server = net.createServer(client =>
    handleClient(client, { ...options, createConnection }, peers)
  )

  await new Promise<void>((resolve, reject) => {
    const onError = (error: Error) => {
      server.off('listening', onListening)
      reject(error)
    }

    const onListening = () => {
      server.off('error', onError)
      resolve()
    }

    server.once('error', onError)
    server.once('listening', onListening)
    server.listen(0, '127.0.0.1')
  })

  const address = server.address()

  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Link title SOCKS gateway did not bind a TCP port')
  }

  let closePromise: Promise<void> | null = null

  return {
    async close() {
      if (!closePromise) {
        closePromise = new Promise<void>((resolve, reject) => {
          for (const peer of peers) {
            peer.destroy()
          }

          peers.clear()
          server.close(error => {
            if (error) {
              reject(error)
            } else {
              resolve()
            }
          })
        })
      }

      return closePromise
    },
    proxyUrl: `socks5://127.0.0.1:${address.port}`
  }
}
