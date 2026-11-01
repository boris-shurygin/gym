import { describe, expect, it, vi } from 'vitest';
import { TelemetryClient } from './telemetry';
import type { SessionInfo } from './account';

const session: SessionInfo = {
  user_id: 'alice',
  csrf_token: 'csrf',
  expires_at: '',
  analytics_timezone: 'UTC',
};
const storage = () => {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      data.set(k, v);
    },
    removeItem: (k: string) => {
      data.delete(k);
    },
  };
};

describe('отдельная диагностическая очередь', () => {
  it('не собирает без согласия, ограничивает буфер/TTL и очищает при отказе', async () => {
    const store = storage(),
      send = vi.fn().mockResolvedValue({ ok: true });
    let now = Date.now();
    const client = new TelemetryClient('alice', session, store, send, () => now);
    client.record('page_error', 'unexpected');
    await client.flush();
    expect(send).not.toHaveBeenCalled();
    client.setConsent(true);
    for (let i = 0; i < 110; i++) {
      now += 60001;
      client.record('sync_failed', 'network');
    }
    expect(client.size()).toBe(100);
    await Promise.resolve();
    const restored = new TelemetryClient('alice', session, store, send, () => now + 86400_001);
    expect(restored.size()).toBe(0);
    client.setConsent(false);
    expect(client.size()).toBe(0);
    expect(store.getItem('gym.diagnostics.buffer:alice')).toBeNull();
  });
  it('изолирует владельцев, ограничивает повторы и не использует outbox при отказах', async () => {
    const store = storage(),
      send = vi.fn().mockRejectedValue(new Error('secret must not be collected'));
    const client = new TelemetryClient('alice', session, store, send);
    client.setConsent(true);
    client.record('local_save_failed', 'storage');
    expect(new TelemetryClient('bob', { ...session, user_id: 'bob' }, store, send).size()).toBe(0);
    for (let i = 0; i < 4; i++) await client.flush();
    expect(send).toHaveBeenCalledTimes(4);
    expect(client.size()).toBe(0);
    const body = JSON.parse(send.mock.calls[0][1].body);
    expect(body.events[1].code).toBe('storage');
    expect(JSON.stringify(body)).not.toContain('secret');
    client.record('sync_failed', 'network');
    client.stop();
    await client.flush();
    expect(send).toHaveBeenCalledTimes(4);
  });
  it('сбой собственного хранилища не ломает диагностику или операции приложения', async () => {
    const fail = () => {
      throw new Error('storage unavailable');
    };
    const send = vi.fn().mockResolvedValue({ ok: true });
    const client = new TelemetryClient(
      'alice',
      session,
      { getItem: fail, setItem: fail, removeItem: fail },
      send,
    );
    expect(() => {
      client.setConsent(true);
      client.record('local_save_failed', 'storage');
    }).not.toThrow();
    await client.flush();
    expect(client.size()).toBe(0);
  });
});
