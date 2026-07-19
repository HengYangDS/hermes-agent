import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // The UI suite shares jsdom/browser-shaped resources while exercising
    // renderer persistence. Keeping a bounded worker pool avoids host-level
    // contention without serializing the whole suite.
    maxWorkers: 4,
    globals: true
  }
}

const electronNative: TestProjectConfiguration = {
  test: {
    name: 'electron',
    environment: 'node',
    include: ['electron/**/*.test.ts', 'scripts/**.test.{ts,mjs}'],
    // Vitest schedules projects with the same default sequence group together.
    // Keep this equal to the UI pool so hosted Node 22 validation has one
    // coherent worker contract without serializing either project.
    maxWorkers: 4
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
