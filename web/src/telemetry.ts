import type { SessionInfo } from './account';

export type DiagnosticName =
  | 'app_open'
  | 'local_save_failed'
  | 'sync_failed'
  | 'sync_completed'
  | 'api_request_slow'
  | 'sw_update_failed'
  | 'page_error';
export type DiagnosticCode = 'storage' | 'network' | 'timeout' | 'server' | 'unexpected' | 'update';
interface Entry {
  event_id: string;
  at: string;
  name: DiagnosticName;
  code: DiagnosticCode | null;
  duration_ms: number | null;
  count: number;
}
const names = [
  'app_open',
  'local_save_failed',
  'sync_failed',
  'sync_completed',
  'api_request_slow',
  'sw_update_failed',
  'page_error',
];
const codes = ['storage', 'network', 'timeout', 'server', 'unexpected', 'update', null];
const TTL = 86400_000;
let active: TelemetryClient | undefined;
const recent: { at: string; code: DiagnosticCode }[] = [];

export function recordDiagnostic(
  name: DiagnosticName,
  code: DiagnosticCode | null = null,
  duration?: number,
) {
  try {
    if (code) {
      recent.push({ at: new Date().toISOString(), code });
      recent.splice(0, Math.max(0, recent.length - 5));
    }
    active?.record(name, code, duration);
  } catch {
    /* Диагностика не участвует в корректности сохранения. */
  }
}
export const recentDiagnosticErrors = () => [...recent];

export class TelemetryClient {
  private events: Entry[] = [];
  private enabled = false;
  private running = false;
  private failures = 0;
  private stopped = false;
  private controller = new AbortController();
  private key: string;
  private consent: string;
  constructor(
    readonly owner: string,
    private session?: SessionInfo,
    private storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'> = localStorage,
    private transport: typeof fetch = (...args) => fetch(...args),
    private now = Date.now,
  ) {
    this.key = `gym.diagnostics.buffer:${owner}`;
    this.consent = `gym.diagnostics.consent:${owner}`;
    try {
      this.enabled = storage.getItem(this.consent) === 'v1';
      const value = this.enabled ? JSON.parse(storage.getItem(this.key) ?? '[]') : [];
      this.events = Array.isArray(value)
        ? value
            .filter(
              (e): e is Entry =>
                e &&
                names.includes(e.name) &&
                codes.includes(e.code) &&
                typeof e.at === 'string' &&
                this.now() - Date.parse(e.at) >= 0 &&
                this.now() - Date.parse(e.at) < TTL &&
                typeof e.event_id === 'string' &&
                /^[a-f0-9-]{36}$/.test(e.event_id) &&
                (e.duration_ms === null ||
                  (Number.isInteger(e.duration_ms) && e.duration_ms >= 0 && e.duration_ms <= 60000)) &&
                Number.isInteger(e.count) &&
                e.count >= 1 &&
                e.count <= 1000 &&
                Object.keys(e).length === 6,
            )
            .slice(-100)
        : [];
      if (!this.enabled) storage.removeItem(this.key);
    } catch {
      this.events = [];
    }
  }
  isEnabled() {
    return this.enabled;
  }
  size() {
    return this.events.length;
  }
  setConsent(enabled: boolean) {
    this.enabled = enabled;
    this.events = [];
    this.controller.abort();
    this.controller = new AbortController();
    try {
      if (enabled) this.storage.setItem(this.consent, 'v1');
      else this.storage.removeItem(this.consent);
      this.storage.removeItem(this.key);
    } catch {
      /* Ограниченный буфер в памяти при недоступном хранилище. */
    }
    if (enabled) this.record('app_open', null);
  }
  record(name: DiagnosticName, code: DiagnosticCode | null, duration?: number) {
    if (!this.enabled || this.stopped || !names.includes(name) || !codes.includes(code)) return;
    const last = this.events.at(-1);
    if (last && last.name === name && last.code === code && this.now() - Date.parse(last.at) < 60000) {
      last.count = Math.min(1000, last.count + 1);
      if (duration !== undefined) last.duration_ms = Math.min(60000, Math.max(0, Math.round(duration)));
    } else
      this.events.push({
        event_id: crypto.randomUUID(),
        at: new Date(this.now()).toISOString(),
        name,
        code,
        duration_ms: duration === undefined ? null : Math.min(60000, Math.max(0, Math.round(duration))),
        count: 1,
      });
    this.events = this.events.filter((e) => this.now() - Date.parse(e.at) < TTL).slice(-100);
    // Запись тренировки уже завершилась: буфер диагностики сохраняется отдельно.
    queueMicrotask(() => this.persist());
  }
  private persist() {
    try {
      if (this.enabled && !this.stopped) this.storage.setItem(this.key, JSON.stringify(this.events));
    } catch {
      /* Не повторяй ошибки хранилища рекурсивно. */
    }
  }
  async flush() {
    if (!this.enabled || this.stopped || this.running || !this.session || this.session.user_id !== this.owner)
      return;
    this.events = this.events.filter((e) => this.now() - Date.parse(e.at) < TTL);
    const batch = this.events.slice(0, 20);
    if (!batch.length) return;
    this.running = true;
    const controller = this.controller;
    const timeout = setTimeout(() => controller.abort(), 2000);
    try {
      const response = await this.transport('/api/v1/telemetry', {
        method: 'POST',
        credentials: 'same-origin',
        signal: controller.signal,
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': this.session.csrf_token,
          'X-Gym-Expected-Owner': this.owner,
        },
        body: JSON.stringify({ expected_owner_id: this.owner, events: batch }),
      });
      if (!response.ok) throw new Error('diagnostics_unavailable');
      const ids = new Set(batch.map((e) => e.event_id));
      this.events = this.events.filter((e) => !ids.has(e.event_id));
      this.failures = 0;
    } catch {
      if (++this.failures >= 4) {
        this.events = [];
        this.failures = 0;
      }
    } finally {
      clearTimeout(timeout);
      if (this.controller === controller) this.controller = new AbortController();
      this.running = false;
      this.persist();
    }
  }
  activate() {
    this.stopped = false;
    if (this.controller.signal.aborted) this.controller = new AbortController();
    active = this;
  }
  stop() {
    this.persist();
    this.stopped = true;
    this.controller.abort();
    if (active === this) active = undefined;
  }
}
