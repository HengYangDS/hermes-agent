import assert from 'node:assert/strict'
import { test } from 'vitest'

import { needsLocalStorageFile, nodeMajor, withLocalStorageFile } from './run-vitest.mjs'

test('nodeMajor extracts the leading major version', () => {
  assert.equal(nodeMajor('26.5.0'), 26)
  assert.equal(nodeMajor('invalid'), 0)
})

test('Node 25+ receives an isolated localStorage backing file unless one is already supplied', () => {
  assert.equal(needsLocalStorageFile('24.13.0', ''), false)
  assert.equal(needsLocalStorageFile('26.5.0', ''), true)
  assert.equal(needsLocalStorageFile('26.5.0', '--localstorage-file=/tmp/existing.json'), false)
  assert.equal(
    withLocalStorageFile('--trace-warnings', '/tmp/storage.json'),
    '--trace-warnings --localstorage-file=/tmp/storage.json'
  )
})
