import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  preview: {
    proxy: process.env.GYM_E2E_API
      ? {
          '/api/': process.env.GYM_E2E_API,
          '/auth/': process.env.GYM_E2E_API,
          '/__test/': process.env.GYM_E2E_API,
          '/__gym/': process.env.GYM_E2E_API,
        }
      : undefined,
  },
  test: { include: ['src/**/*.test.ts'], environment: 'node' },
});
