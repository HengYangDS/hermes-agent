import { lookup as dnsLookup } from 'node:dns'
import { isIP } from 'node:net'

export type LinkTitleAddress = { address: string; family: 4 | 6 }

export interface LinkTitlePinnedResolver {
  clear(): void
  resolve(hostname: string): Promise<readonly LinkTitleAddress[]>
}

interface CachedAddresses {
  addresses: readonly LinkTitleAddress[]
  expiresAt: number
}

type LinkTitleLookup = (hostname: string) => Promise<readonly LinkTitleAddress[]>

function lookupAll(hostname: string): Promise<readonly LinkTitleAddress[]> {
  return new Promise((resolve, reject) => {
    dnsLookup(hostname, { all: true, verbatim: true }, (error, addresses) => {
      if (error) {
        reject(error)

        return
      }

      resolve(
        addresses.map(value => ({
          address: value.address,
          family: value.family as 4 | 6
        }))
      )
    })
  })
}

function normalizeHostname(hostname: string): string {
  let normalized = hostname.trim().toLowerCase()

  if (normalized.startsWith('[') && normalized.endsWith(']')) {
    normalized = normalized.slice(1, -1)
  }

  if (normalized.endsWith('.')) {
    normalized = normalized.slice(0, -1)
  }

  if (!normalized) {
    throw new Error('Link title DNS hostname is empty')
  }

  return normalized
}

function immutableAddresses(addresses: readonly LinkTitleAddress[]): readonly LinkTitleAddress[] {
  return Object.freeze(addresses.map(value => Object.freeze({ ...value })))
}

function approveAnswers(
  answers: readonly LinkTitleAddress[],
  isPublicAddress: (value: string) => boolean
): readonly LinkTitleAddress[] {
  if (answers.length === 0) {
    throw new Error('Link title DNS returned an empty answer set')
  }

  if (answers.some(answer => !isPublicAddress(answer.address))) {
    throw new Error('Link title DNS returned a non-public address')
  }

  const approved: LinkTitleAddress[] = []
  const seen = new Set<string>()

  for (const answer of answers) {
    const key = `${answer.family}:${answer.address}`

    if (!seen.has(key)) {
      seen.add(key)
      approved.push(answer)
    }
  }

  return immutableAddresses(approved)
}

export function createLinkTitlePinnedResolver(options: {
  isPublicAddress(value: string): boolean
  lookup?: LinkTitleLookup
  now?: () => number
  ttlMs: number
}): LinkTitlePinnedResolver {
  const cache = new Map<string, CachedAddresses>()
  const inflight = new Map<string, Promise<readonly LinkTitleAddress[]>>()
  const lookup = options.lookup ?? lookupAll
  const now = options.now ?? Date.now
  let generation = 0

  return {
    clear() {
      generation += 1
      cache.clear()
      inflight.clear()
    },
    async resolve(hostname) {
      const normalized = normalizeHostname(hostname)
      const family = isIP(normalized)

      if (family !== 0) {
        if (!options.isPublicAddress(normalized)) {
          throw new Error('Link title DNS literal is a non-public address')
        }

        return immutableAddresses([{ address: normalized, family: family === 4 ? 4 : 6 }])
      }

      const cached = cache.get(normalized)

      if (cached && now() < cached.expiresAt) {
        return cached.addresses
      }

      cache.delete(normalized)

      const pending = inflight.get(normalized)

      if (pending) {
        return pending
      }

      const startedGeneration = generation

      const resolution = (async () => {
        const approved = approveAnswers(await lookup(normalized), options.isPublicAddress)

        if (generation === startedGeneration) {
          cache.set(normalized, { addresses: approved, expiresAt: now() + options.ttlMs })
        }

        return approved
      })()

      inflight.set(normalized, resolution)

      try {
        return await resolution
      } finally {
        if (inflight.get(normalized) === resolution) {
          inflight.delete(normalized)
        }
      }
    }
  }
}
