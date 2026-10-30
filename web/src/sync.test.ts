import 'fake-indexeddb/auto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSession, grams, applyMutation, type Command } from './model';
import { WorkoutRepository } from './repository';
import {
  programFromPlans,
  rebase,
  sessionFromWorkout,
  type BootstrapPage,
  type Receipt,
  type Workout,
} from './sync-model';
import { SyncCoordinator, retryDelay, type Transport } from './sync';
import { ApiError } from './account';
import { plan, workout } from './test-fixtures';

const now = '2026-10-30T10:00:00Z';
const repos: WorkoutRepository[] = [];
const coordinators: SyncCoordinator[] = [];
function repository(name: string = crypto.randomUUID()) {
  const r = new WorkoutRepository(name);
  repos.push(r);
  return r;
}
function start(): Command {
  const p = plan();
  const session = createSession(
    programFromPlans('alice', [p]),
    p.template.template_id,
    'alice',
    crypto.randomUUID(),
    now,
  );
  return {
    id: crypto.randomUUID(),
    ownerId: 'alice',
    sessionId: session.id,
    expectedRevision: 0,
    createdAt: now,
    mutation: { type: 'start', session },
  };
}
function add(id: string, revision = 1): Command {
  return {
    id: crypto.randomUUID(),
    ownerId: 'alice',
    sessionId: id,
    expectedRevision: revision,
    createdAt: now,
    mutation: {
      type: 'addSet',
      setId: crypto.randomUUID(),
      exerciseId: plan().template.exercises[0].template_exercise_id,
      values: { kind: 'working', weightKg: 62.125, reps: 9 },
    },
  };
}
function applied(op: Command, revision = 1): Receipt {
  return {
    operation_id: op.id,
    workout_id: op.sessionId,
    outcome: 'applied',
    server_revision: revision,
    change_seq: revision,
    accepted_at: now,
  };
}
function rejected(op: Command): Receipt {
  return {
    operation_id: op.id,
    workout_id: op.sessionId,
    outcome: 'rejected',
    code: 'revision_conflict',
    observed_server_revision: 2,
    rejected_at: now,
  };
}
function base(session: ReturnType<typeof sessionFromWorkout>, revision = 1): Workout {
  return {
    ...workout(session.id, revision),
    status: session.status,
    completed_at: session.completedAt ?? null,
    sets: session.sets.map((s) => ({
      set_id: s.id,
      session_exercise_id: s.exerciseId,
      kind: s.kind,
      weight_g: grams(s.weightKg),
      reps: s.reps,
      planned_target_id: s.plannedTargetId,
      comparison_target_id: s.comparisonTarget?.id ?? null,
      deleted_at: s.deletedAt ?? null,
    })),
  };
}
function bootstrap(items: BootstrapPage['items'] = []): BootstrapPage {
  return {
    bootstrap_id: 'bs_test',
    high_watermark: 1,
    checkpoint_cursor: 'checkpoint',
    next_page_cursor: null,
    items,
  };
}
function transport(extra: (path: string, input?: object) => unknown = () => undefined): Transport {
  return async <T>(path: string, input?: object): Promise<T> => {
    const custom = await extra(path, input);
    if (custom !== undefined) return custom as T;
    if (path === '/auth/session') return { user_id: 'alice', csrf_token: 'c'.repeat(64) } as T;
    if (path.includes('/bootstrap')) return bootstrap() as T;
    if (path.includes('/changes'))
      return { high_watermark: 1, next_cursor: crypto.randomUUID(), has_more: false, items: [] } as T;
    if (path === '/api/v1/plans') return { items: [plan()] } as T;
    throw new Error(`Unexpected test route ${path}`);
  };
}
function coordinator(r: WorkoutRepository, t: Transport, active = () => true, foreground = () => true) {
  const changed = vi.fn(),
    notice = vi.fn();
  const c = new SyncCoordinator(r, 'alice', active, changed, notice, t, foreground);
  coordinators.push(c);
  return { c, changed, notice };
}
afterEach(async () => {
  vi.restoreAllMocks();
  for (const c of coordinators.splice(0)) c.stop();
  for (const r of repos.splice(0)) await r.close();
});

describe('Надёжная очередь P08', () => {
  it('скрытая вкладка не запускает сеть, возвращение продолжает доставку', async () => {
    const r = repository();
    let visible = false;
    const requests: string[] = [];
    const t = transport((path) => {
      requests.push(path);
    });
    const { c } = coordinator(
      r,
      t,
      () => true,
      () => visible,
    );
    await c.run();
    expect(requests).toHaveLength(0);
    visible = true;
    await c.run();
    expect(requests.length).toBeGreaterThan(0);
  });
  it('периодический таймер не продолжает опрос после скрытия вкладки', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const r = repository();
      let visible = true;
      const requests: string[] = [];
      const t = transport((path) => {
        requests.push(path);
      });
      const { c } = coordinator(
        r,
        t,
        () => true,
        () => visible,
      );
      await c.run();
      const calls = requests.length;
      visible = false;
      await vi.advanceTimersByTimeAsync(30_000);
      expect(requests).toHaveLength(calls);
      visible = true;
      await c.run();
      expect(requests.length).toBeGreaterThan(calls);
    } finally {
      vi.useRealTimers();
    }
  });
  it('запоздалый отправитель не возвращает конфликт/окончательный отказ в sending', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    await r.receipt('alice', command.id, rejected(command));
    expect(await r.updateOperation('alice', command.id, { status: 'sending' })).toBe(false);
    expect((await r.read('alice')).operations[0].status).toBe('conflict');
  });
  it('явный локальный архив отвергнутого старта сохраняет намерения и запрещает дальнейшую отправку', async () => {
    const r = repository(),
      command = start();
    const local = await r.commit(command);
    await r.receipt('alice', command.id, {
      operation_id: command.id,
      workout_id: command.sessionId,
      outcome: 'rejected',
      code: 'snapshot_unavailable',
      observed_server_revision: 0,
      rejected_at: now,
    });
    await r.keepLocal('alice', local.id, local.revision, [command.id]);
    const archived = (await r.read('alice')).sessions[0];
    expect(archived.localOnly).toBe(true);
    await r.commit(add(local.id, archived.revision));
    const data = await r.read('alice');
    expect(data.operations[1].wire).toBeUndefined();
    expect(data.legacyCount).toBe(1);
    const backup = JSON.parse(await r.export('alice'));
    expect(backup.decisions[0].oldIds).toEqual([command.id]);
  });
  it('неизвестный исход отправки нельзя молча архивировать как локальный старт', async () => {
    const r = repository(),
      command = start();
    const local = await r.commit(command);
    await r.updateOperation('alice', command.id, { status: 'sending' });
    await expect(r.keepLocal('alice', local.id, local.revision, [command.id])).rejects.toThrow();
    expect((await r.read('alice')).operations[0].status).toBe('sending');
  });
  it('подтверждение старта сохраняет его исходное время и не инвалидирует открытую форму', async () => {
    const r = repository(),
      command = start();
    command.createdAt = '2026-10-30T10:00:00.025Z';
    const local = await r.commit(command);
    const op = (await r.read('alice')).operations[0].wire!;
    expect(op.client_occurred_at).toBe(local.startedAt);
    await r.receipt('alice', command.id, applied(command));
    await r.receive('alice', { workouts: [{ ...workout(local.id), started_at: op.client_occurred_at }] });
    expect((await r.read('alice')).sessions[0].revision).toBe(local.revision);
  });
  it('записывает точные граммы и подлинный снимок, зависимость — ID предшественника', async () => {
    const r = repository(),
      first = start();
    await r.commit(first);
    const second = add(first.sessionId);
    await r.commit(second);
    const data = await r.read('alice');
    expect(data.operations[0].wire?.command).toEqual({
      type: 'start_workout',
      plan_snapshot_id: plan().plan_snapshot_id,
    });
    expect(data.operations[1].wire?.precondition).toEqual({ after_operation_id: first.id });
    expect(data.operations[1].wire?.command).toMatchObject({
      weight_g: 62125,
      reps: 9,
      planned_target_id: plan().template.exercises[0].targets[0].target_id,
    });
    expect(data.pendingCount).toBe(2);
  });
  it('не округляет четвёртый знак, отрицательные/огромные значения и ограничивает повторения', async () => {
    expect(grams(0.001)).toBe(1);
    expect(grams(2000)).toBe(2_000_000);
    expect(grams(0)).toBe(0);
    for (const n of [0.0001, -1, 2000.001, Infinity, NaN]) expect(() => grams(n)).toThrow();
    const r = repository(),
      first = start();
    await r.commit(first);
    const command = add(first.sessionId);
    if (command.mutation.type === 'addSet') command.mutation.values.reps = 1001;
    await expect(r.commit(command)).rejects.toThrow();
    expect((await r.read('alice')).pendingCount).toBe(1);
  });
  it('миграция v1 сохраняет старые строки без выдуманной wire-операции', async () => {
    const name = crypto.randomUUID(),
      command = start();
    if (command.mutation.type !== 'start') throw new Error();
    delete command.mutation.session.snapshot.planSnapshot;
    const session = { ...command.mutation.session, revision: 1 };
    await new Promise<void>((resolve, reject) => {
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => {
        for (const store of ['sessions', 'outbox'])
          request.result
            .createObjectStore(store, { keyPath: ['ownerId', 'id'] })
            .createIndex('ownerId', 'ownerId');
      };
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction(['sessions', 'outbox'], 'readwrite');
        tx.objectStore('sessions').put(session);
        tx.objectStore('outbox').add({ ...command, fingerprint: 'legacy', status: 'pending' });
        tx.oncomplete = () => {
          db.close();
          resolve();
        };
        tx.onabort = () => reject(tx.error);
      };
    });
    const data = await repository(name).read('alice');
    expect(data.sessions).toEqual([session]);
    expect(data.legacyCount).toBe(1);
    expect(data.operations[0].wire).toBeUndefined();
  });
  it('подтверждённая база не затирает ожидающую локальную правку', async () => {
    const r = repository(),
      first = start();
    await r.commit(first);
    await r.receipt('alice', first.id, applied(first));
    const second = add(first.sessionId);
    const local = await r.commit(second);
    const remote = base(local, 2);
    remote.sets[0].weight_g = 80000;
    await r.receive('alice', { workouts: [remote], cursor: 'next' });
    let data = await r.read('alice');
    expect(data.sessions[0].sets[0].weightKg).toBe(62.125);
    expect(data.bases[0].sets[0].weight_g).toBe(80000);
    await r.receipt('alice', second.id, rejected(second));
    data = await r.read('alice');
    expect(data.operations[1].status).toBe('conflict');
    expect(data.sessions[0].sets[0].weightKg).toBe(62.125);
  });
  it('сбой commit страницы откатывает и проекцию, и курсор, сохраняя очередь', async () => {
    const r = repository();
    await r.receive('alice', { cursor: 'before' });
    const original = IDBObjectStore.prototype.put;
    vi.spyOn(IDBObjectStore.prototype, 'put').mockImplementation(function (
      this: IDBObjectStore,
      input: unknown,
      key?: IDBValidKey,
    ) {
      if (this.name === 'meta') throw new DOMException('quota', 'QuotaExceededError');
      return original.call(this, input, key);
    });
    await expect(r.receive('alice', { workouts: [workout()], cursor: 'after' }, 'before')).rejects.toThrow();
    const data = await r.read('alice');
    expect(data.state.cursor).toBe('before');
    expect(data.sessions).toHaveLength(0);
    expect(data.bases).toHaveLength(0);
  });
  it('устаревшая страница другой вкладки не откатывает курсор', async () => {
    const r = repository();
    await r.receive('alice', { cursor: 'new' });
    expect(await r.receive('alice', { workouts: [workout()], cursor: 'old' }, 'before')).toBe(false);
    expect((await r.read('alice')).state.cursor).toBe('new');
  });
  it('lease исключает конкурирующую отправку, истечение разрешает восстановление', async () => {
    const name = crypto.randomUUID(),
      a = repository(name),
      b = repository(name);
    expect(await a.lease('alice', 'work', 'one', 100)).toBe(true);
    expect(await b.lease('alice', 'work', 'two', 100)).toBe(false);
    expect(await b.lease('alice', 'work', 'two', 30_101)).toBe(true);
    expect(await a.lease('alice', 'work', 'one', 30_102)).toBe(false);
  });
  it('чужие планы/снимки не попадают в кэш владельца', async () => {
    const r = repository();
    await expect(r.receive('alice', { plans: [plan('bob')] })).rejects.toThrow();
    const w = workout();
    w.owner_id = 'bob';
    await expect(r.receive('alice', { workouts: [w] })).rejects.toThrow();
    expect((await r.read('alice')).bases).toHaveLength(0);
  });
});

describe('Координатор доставки', () => {
  it('потерянный ответ после исполнения восстанавливается по квитанции без второго POST', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    let posts = 0,
      receiptReads = 0;
    const accepted = applied(command);
    const wire = JSON.stringify((await r.read('alice')).operations[0].wire);
    const t = transport((path, input) => {
      if (path === '/api/v1/sync/operations') {
        posts++;
        expect(JSON.stringify(input)).toBe(wire);
        throw new TypeError('network response lost');
      }
      if (path.endsWith(`/operations/${command.id}`)) {
        receiptReads++;
        return accepted;
      }
      if (path.endsWith(`/workouts/${command.sessionId}`)) return workout(command.sessionId);
    });
    const { c } = coordinator(r, t);
    await c.run();
    expect((await r.read('alice')).operations[0].status).toBe('sending');
    await r.close();
    await c.run(true);
    expect(posts).toBe(1);
    expect(receiptReads).toBe(1);
    expect((await r.read('alice')).pendingCount).toBe(0);
  });
  it('пустая квитанция после сетевого сбоя повторяет неизменный ID/тело', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    const bodies: string[] = [];
    const { c } = coordinator(
      r,
      transport((path, input) => {
        if (path === '/api/v1/sync/operations') {
          bodies.push(JSON.stringify(input));
          if (bodies.length === 1) throw new TypeError('offline');
          return applied(command);
        }
        if (path.endsWith(`/operations/${command.id}`)) throw new ApiError('not_found', '', 404);
        if (path.endsWith(`/workouts/${command.sessionId}`)) return workout(command.sessionId);
      }),
    );
    await c.run();
    await c.run(true);
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toBe(bodies[1]);
    expect((await r.read('alice')).operations[0].status).toBe('acked');
  });
  it('подтверждённый иной аккаунт останавливает POST и блокирует очередь прежнего', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    const calls: string[] = [];
    const { c, notice } = coordinator(
      r,
      transport((path) => {
        calls.push(path);
        if (path === '/auth/session') return { user_id: 'bob' };
      }),
    );
    await c.run();
    expect(calls).toEqual(['/auth/session']);
    expect((await r.read('alice')).operations[0].status).toBe('blocked_auth');
    expect(notice.mock.lastCall?.[0].needsLogin).toBe(true);
  });
  it('поздний ответ после скрытия профиля не подтверждает запись и не запускает новые запросы', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    let finish!: (r: Receipt) => void;
    let reached!: () => void;
    const reachedPromise = new Promise<void>((resolve) => {
      reached = resolve;
    });
    const t = transport((path) => {
      if (path === '/api/v1/sync/operations') {
        reached();
        return new Promise<Receipt>((resolve) => {
          finish = resolve;
        });
      }
    });
    const { c, changed } = coordinator(r, t);
    const running = c.run();
    await reachedPromise;
    c.stop();
    finish(applied(command));
    await running;
    expect((await r.read('alice')).operations[0].status).toBe('sending');
    expect(changed).not.toHaveBeenCalled();
  });
  it('неполный bootstrap не сохраняет часть снимка и не очищает outbox', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    const { c } = coordinator(
      r,
      transport((path) => {
        if (path === '/api/v1/sync/bootstrap')
          return { ...bootstrap([{ kind: 'workout', data: workout() }]), next_page_cursor: 'page2' };
        if (path.includes('page_cursor=')) throw new TypeError('offline');
      }),
    );
    await c.run();
    const data = await r.read('alice');
    expect(data.state.cursor).toBeUndefined();
    expect(data.bases).toHaveLength(0);
    expect(data.pendingCount).toBe(1);
  });
  it('410 запускает согласованный bootstrap и сохраняет неподтверждённые факты', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    await r.receive('alice', { cursor: 'expired' });
    const { c } = coordinator(
      r,
      transport((path) => {
        if (path.includes('/changes?cursor=expired')) throw new ApiError('cursor_expired', '', 410);
        if (path === '/api/v1/sync/operations') throw new ApiError('unavailable', '', 503);
      }),
    );
    await c.run();
    const data = await r.read('alice');
    expect(data.state.cursor).not.toBe('expired');
    expect(data.pendingCount).toBe(1);
    expect(data.sessions).toHaveLength(1);
  });
  it('upgrade_required останавливает повторы без удаления операции', async () => {
    const r = repository(),
      command = start();
    await r.commit(command);
    let posts = 0;
    const { c } = coordinator(
      r,
      transport((path) => {
        if (path === '/api/v1/sync/operations') {
          posts++;
          throw new ApiError('upgrade_required', '', 409);
        }
      }),
    );
    await c.run();
    await c.run(true);
    expect(posts).toBe(1);
    expect((await r.read('alice')).operations[0].reason).toBe('upgrade_required');
  });
  it('задержка ограничена, содержит jitter и учитывает Retry-After', () => {
    expect(retryDelay(1, 0, () => 0)).toBe(1500);
    expect(retryDelay(1, 90, () => 0)).toBe(90_000);
    expect(retryDelay(100, 0, () => 0.5)).toBe(256_000);
  });
});

describe('Явный разбор всей конфликтующей цепочки', () => {
  async function conflict() {
    const r = repository(),
      first = start();
    const empty = await r.commit(first);
    await r.receipt('alice', first.id, applied(first));
    const second = add(first.sessionId);
    let local = await r.commit(second);
    await r.receipt('alice', second.id, applied(second, 2));
    const setId = local.sets[0].id;
    const edit: Command = {
      ...add(first.sessionId, local.revision),
      mutation: { type: 'editSet', setId, values: { kind: 'working', weightKg: 75, reps: 8 } },
    };
    local = await r.commit(edit);
    const extra = add(first.sessionId, local.revision);
    local = await r.commit(extra);
    const complete: Command = { ...add(first.sessionId, local.revision), mutation: { type: 'complete' } };
    local = await r.commit(complete);
    const remote = base(
      applyMutation(empty, second.mutation as Exclude<Command['mutation'], { type: 'start' }>, now),
      3,
    );
    remote.sets[0].weight_g = 80000;
    await r.receive('alice', { workouts: [remote] });
    await r.receipt('alice', edit.id, rejected(edit));
    return { r, local, remote, suffix: [edit, extra, complete] };
  }
  it('новые ID и цепочка от увиденной ревизии сохраняют подходы и завершение', async () => {
    const { r, local, remote, suffix } = await conflict();
    await r.resolve(
      'alice',
      local.revision,
      remote,
      suffix.map((c) => c.id),
      'local',
    );
    const data = await r.read('alice'),
      replacements = data.operations.filter((op) => op.status === 'pending');
    expect(replacements).toHaveLength(3);
    expect(replacements[0].wire?.precondition).toEqual({ server_revision: 3 });
    expect(replacements[1].wire?.precondition).toEqual({ after_operation_id: replacements[0].id });
    expect(replacements.map((op) => op.id).some((id) => suffix.some((old) => old.id === id))).toBe(false);
    expect(data.sessions[0].sets[0].weightKg).toBe(75);
    expect(data.sessions[0].status).toBe('completed');
    expect(data.sessions[0].completedAt).toBe(now);
    expect(data.operations.filter((op) => op.status === 'superseded')).toHaveLength(3);
  });
  it('выбор сервера архивирует всю цепочку и исходные факты, а не выдаёт их за ack', async () => {
    const { r, local, remote, suffix } = await conflict();
    await r.resolve(
      'alice',
      local.revision,
      remote,
      suffix.map((c) => c.id),
      'server',
    );
    const data = await r.read('alice');
    expect(data.pendingCount).toBe(0);
    expect(data.sessions[0].sets[0].weightKg).toBe(80);
    const backup = JSON.parse(await r.export('alice'));
    expect(backup.decisions[0].local.sets[0].weightKg).toBe(75);
    expect(backup.decisions[0].oldIds).toHaveLength(3);
    expect(data.operations.filter((op) => op.status === 'acked')).toHaveLength(2);
  });
  it('конкурентная локальная запись после предпросмотра запрещает устаревшее решение', async () => {
    const { r, local, remote, suffix } = await conflict();
    await r.commit(add(local.id, local.revision));
    await expect(
      r.resolve(
        'alice',
        local.revision,
        remote,
        suffix.map((c) => c.id),
        'server',
      ),
    ).rejects.toThrow('другой вкладке');
    expect((await r.read('alice')).operations.some((op) => op.status === 'superseded')).toBe(false);
  });
  it('исправление удалённого сервером подхода создаёт новый ID с явным предупреждением', async () => {
    const { local, remote, suffix } = await conflict();
    remote.sets[0].deleted_at = now;
    const preview = rebase(remote, local, suffix, now);
    expect(preview.session.sets[0].deletedAt).toBe(now);
    expect(preview.session.sets[1].id).not.toBe(local.sets[0].id);
    expect(preview.warnings.join(' ')).toContain('новым подходом');
  });
  it('отменённое занятие не возрождается локальным rebasing', async () => {
    const { local, remote, suffix } = await conflict();
    remote.status = 'cancelled';
    expect(() => rebase(remote, local, suffix, now)).toThrow('отменена');
  });
});
