import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const NODE_LOCAL_STORAGE_FILE = /(?:^|\s)--localstorage-file(?:=|\s)/
const MIN_NODE_VERSION_REQUIRING_STORAGE_FILE = 25

export function nodeMajor(version = process.versions.node) {
  const match = /^(\d+)/.exec(version)
  return match ? Number(match[1]) : 0
}

export function needsLocalStorageFile(version = process.versions.node, nodeOptions = process.env.NODE_OPTIONS ?? '') {
  return nodeMajor(version) >= MIN_NODE_VERSION_REQUIRING_STORAGE_FILE && !NODE_LOCAL_STORAGE_FILE.test(nodeOptions)
}

export function withLocalStorageFile(nodeOptions, storageFile) {
  return [nodeOptions, `--localstorage-file=${storageFile}`].filter(Boolean).join(' ')
}

function main() {
  const inheritedOptions = process.env.NODE_OPTIONS ?? ''
  let tempRoot
  let env = process.env

  if (needsLocalStorageFile(process.versions.node, inheritedOptions)) {
    tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-vitest-localstorage-'))
    const storageFile = path.join(tempRoot, 'storage.json')
    fs.writeFileSync(storageFile, '', 'utf8')
    env = { ...process.env, NODE_OPTIONS: withLocalStorageFile(inheritedOptions, storageFile) }
  }

  try {
    const scriptDir = path.dirname(fileURLToPath(import.meta.url))
    const vitest = path.resolve(scriptDir, '../../../node_modules/vitest/vitest.mjs')
    const result = spawnSync(process.execPath, [vitest, ...process.argv.slice(2)], { env, stdio: 'inherit' })

    if (result.error) throw result.error
    if (result.signal) process.kill(process.pid, result.signal)
    process.exitCode = result.status ?? 1
  } finally {
    if (tempRoot) fs.rmSync(tempRoot, { force: true, recursive: true })
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main()
}
