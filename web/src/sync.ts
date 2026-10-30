import { api, ApiError, type SessionInfo, type ProgramView } from './account';
import { WorkoutRepository, unresolved } from './repository';
import type { BootstrapPage, ChangePage, PlanSnapshot, Receipt, Workout } from './sync-model';
import type { PendingOperation } from './model';

export type Transport = <T>(
  path: string,
  input?: object,
  session?: SessionInfo,
  signal?: AbortSignal,
) => Promise<T>;
export interface SyncNotice {
  busy: boolean;
  message: string;
  needsLogin?: boolean;
}
export function retryDelay(attempt: number, retryAfter = 0, random = Math.random): number {
  return Math.max(
    Math.min(300_000, 1000 * 2 ** Math.min(attempt, 8)) * (0.75 + random() * 0.5),
    retryAfter * 1000,
  );
}
const authError = (error: unknown) => error instanceof ApiError && [401, 403].includes(error.status);

export class SyncCoordinator {
  private controller = new AbortController();
  private holder = crypto.randomUUID();
  private running = false;
  private again = false;
  private timer?: ReturnType<typeof setTimeout>;
  constructor(
    private repository: WorkoutRepository,
    readonly owner: string,
    private active: () => boolean,
    private changed: () => void,
    private notice: (value: SyncNotice) => void,
    private transport: Transport = api,
    private foreground: () => boolean = () => true,
  ) {}

  stop() {
    this.controller.abort();
    clearTimeout(this.timer);
  }
  private valid() {
    return !this.controller.signal.aborted && this.active();
  }
  private async request<T>(path: string, input?: object, session?: SessionInfo): Promise<T> {
    if (!this.valid()) throw new DOMException('Profile stopped', 'AbortError');
    const controller = new AbortController();
    const abort = () => controller.abort();
    this.controller.signal.addEventListener('abort', abort, { once: true });
    const timeout = setTimeout(abort, 20_000);
    try {
      const result = await this.transport<T>(path, input, session, controller.signal);
      if (!this.valid()) throw new DOMException('Profile stopped', 'AbortError');
      return result;
    } finally {
      clearTimeout(timeout);
      this.controller.signal.removeEventListener('abort', abort);
    }
  }
  async run(manual = false): Promise<void> {
    if (!this.valid()) return;
    // Таймер не должен опрашивать сервер из скрытой вкладки. Текущий запрос
    // завершается обычно; visibilitychange/online запускают следующую попытку.
    if (!manual && !this.foreground()) return;
    if (this.running) {
      this.again = true;
      return;
    }
    this.running = true;
    clearTimeout(this.timer);
    this.notice({ busy: true, message: 'Синхронизируем…' });
    try {
      const session = await this.request<SessionInfo>('/auth/session');
      if (session.user_id !== this.owner) throw new ApiError('account_mismatch', '', 403);
      await this.pull();
      await this.flush(session, manual);
      if (this.valid()) {
        await this.pull();
        this.notice({ busy: false, message: 'Связь с сервером проверена' });
        this.changed();
      }
    } catch (error) {
      if (!this.valid()) return;
      if (authError(error)) {
        const data = await this.repository.read(this.owner);
        for (const op of data.operations.filter(
          (op) => op.wire && unresolved(op) && !['conflict', 'rejected'].includes(op.status),
        ))
          await this.repository.updateOperation(this.owner, op.id, {
            status: 'blocked_auth',
            reason: (error as ApiError).code,
          });
        this.notice({
          busy: false,
          message: 'Для отправки записей нужен вход прежним аккаунтом. Очередь сохранена.',
          needsLogin: true,
        });
      } else
        this.notice({
          busy: false,
          message: 'Сервер сейчас недоступен. Продолжай записывать на устройстве; повторим позже.',
        });
      this.changed();
    } finally {
      this.running = false;
      if (this.valid()) {
        const delay = this.again ? 500 : 30_000;
        this.again = false;
        this.timer = setTimeout(() => void this.run(), delay);
      }
    }
  }

  private async bootstrap(expectedCursor: string): Promise<void> {
    let page = await this.request<BootstrapPage>('/api/v1/sync/bootstrap');
    const id = page.bootstrap_id,
      high = page.high_watermark,
      checkpoint = page.checkpoint_cursor;
    const workouts: Workout[] = [],
      plans: PlanSnapshot[] = [];
    let program: ProgramView = { owner_id: this.owner, version: null };
    // Nothing is committed until the entire consistent manifest has arrived.
    for (let count = 0; ; count++) {
      if (
        count >= 10_000 ||
        page.bootstrap_id !== id ||
        page.high_watermark !== high ||
        page.checkpoint_cursor !== checkpoint
      )
        throw new Error('Invalid bootstrap');
      for (const item of page.items) {
        if (item.kind === 'workout') workouts.push(item.data);
        else if (item.kind === 'plan') plans.push(item.data);
        else program = item.data;
      }
      if (!page.next_page_cursor) break;
      page = await this.request<BootstrapPage>(
        `/api/v1/sync/bootstrap?page_cursor=${encodeURIComponent(page.next_page_cursor)}`,
      );
    }
    if (this.valid())
      await this.repository.receive(
        this.owner,
        { workouts, plans, program, cursor: checkpoint, refreshedAt: new Date().toISOString() },
        expectedCursor,
      );
  }

  private async pull(): Promise<void> {
    let data = await this.repository.read(this.owner);
    if (!data.state.cursor) await this.bootstrap('');
    data = await this.repository.read(this.owner);
    let cursor = data.state.cursor;
    if (!cursor) return;
    let high: number | undefined;
    for (let count = 0; count < 10_000; count++) {
      let page: ChangePage;
      try {
        page = await this.request<ChangePage>(
          `/api/v1/sync/changes?cursor=${encodeURIComponent(cursor)}&limit=100`,
        );
      } catch (error) {
        if (error instanceof ApiError && error.code === 'cursor_expired') {
          await this.bootstrap(cursor);
          break;
        }
        throw error;
      }
      high ??= page.high_watermark;
      if (page.high_watermark !== high) throw new Error('Changed high watermark');
      const values: Parameters<WorkoutRepository['receive']>[1] = {
        workouts: page.items.flatMap((item) => (item.workout ? [item.workout] : [])),
        cursor: page.next_cursor,
      };
      for (const item of page.items) {
        if (item.plans) values.plans = item.plans;
        if (item.kind === 'program_published' && item.program_version)
          values.program = { owner_id: this.owner, version: item.program_version };
      }
      if (!(await this.repository.receive(this.owner, values, cursor))) break;
      cursor = page.next_cursor;
      if (!page.has_more) break;
    }
    // This also refreshes server-calculated plans after history corrections; old starts keep their own snapshot.
    const plans = await this.request<{ items: PlanSnapshot[] }>('/api/v1/plans');
    if (this.valid())
      await this.repository.receive(this.owner, {
        plans: plans.items,
        refreshedAt: new Date().toISOString(),
      });
  }

  private async flush(session: SessionInfo, manual: boolean): Promise<void> {
    const initial = await this.repository.read(this.owner);
    const workouts = [
      ...new Set(initial.operations.filter((op) => op.wire && unresolved(op)).map((op) => op.sessionId)),
    ];
    for (const workoutId of workouts) {
      if (!this.valid()) return;
      if (!(await this.repository.lease(this.owner, workoutId, this.holder, Date.now()))) continue;
      try {
        for (let count = 0; count < 100 && this.valid(); count++) {
          const data = await this.repository.read(this.owner);
          const op = data.operations.find((op) => op.sessionId === workoutId && op.wire && unresolved(op));
          if (
            !op ||
            ['conflict', 'rejected'].includes(op.status) ||
            (!manual && (op.nextAttemptAt ?? 0) > Date.now())
          )
            break;
          await this.repository.lease(this.owner, workoutId, this.holder, Date.now());
          if (!(await this.send(op, session))) break;
        }
      } finally {
        await this.repository.lease(this.owner, workoutId, this.holder, Date.now(), true);
      }
    }
  }
  private async send(op: PendingOperation, session: SessionInfo): Promise<boolean> {
    if (!op.wire || !this.valid()) return false;
    if (!(await this.repository.updateOperation(this.owner, op.id, { status: 'sending' }))) return false;
    try {
      let receipt: Receipt | undefined;
      if (op.status === 'sending' || (op.attempts ?? 0) > 0) {
        try {
          receipt = await this.request<Receipt>(`/api/v1/sync/operations/${op.id}`);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
        }
      }
      receipt ??= await this.request<Receipt>('/api/v1/sync/operations', op.wire, session);
      await this.repository.receipt(this.owner, op.id, receipt);
      this.changed();
      await this.refreshWorkout(op.sessionId);
      return receipt.outcome === 'applied';
    } catch (error) {
      if (!this.valid()) return false;
      if (authError(error)) throw error;
      if (error instanceof ApiError && error.receipt) {
        await this.repository.receipt(this.owner, op.id, error.receipt);
        await this.refreshWorkout(op.sessionId);
      } else if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500 &&
        ![408, 429].includes(error.status) &&
        error.code !== 'dependency_pending'
      ) {
        await this.repository.updateOperation(this.owner, op.id, { status: 'rejected', reason: error.code });
      } else {
        const attempts = (op.attempts ?? 0) + 1;
        await this.repository.updateOperation(this.owner, op.id, {
          status: 'sending',
          attempts,
          nextAttemptAt: Date.now() + retryDelay(attempts, error instanceof ApiError ? error.retryAfter : 0),
          reason: 'Исход запроса неизвестен; проверим квитанцию и повторим тот же ID.',
        });
      }
      this.changed();
      return false;
    }
  }
  async refreshWorkout(id: string): Promise<void> {
    const workout = await this.request<Workout>(`/api/v1/workouts/${id}`);
    if (this.valid()) await this.repository.receive(this.owner, { workouts: [workout] });
  }
}
