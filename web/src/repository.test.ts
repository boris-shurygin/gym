import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { demoProgram } from './demo-program';
import { createSession, type Command } from './model';
import { WorkoutRepository } from './repository';

const now = '2026-10-25T12:00:00Z';
let repository: WorkoutRepository;
let databaseName: string;
function start(ownerId = 'alice', id = 'session'): Command {
  return {
    id: `start-${id}`,
    ownerId,
    sessionId: id,
    expectedRevision: 0,
    createdAt: now,
    mutation: { type: 'start', session: createSession(demoProgram, 'upper', ownerId, id, now) },
  };
}
function add(id = 'op-one', expectedRevision = 1): Command {
  return {
    id,
    ownerId: 'alice',
    sessionId: 'session',
    expectedRevision,
    createdAt: now,
    mutation: {
      type: 'addSet',
      exerciseId: 'bench',
      setId: `set-${id}`,
      values: { kind: 'working', weightKg: 62.5, reps: 9 },
    },
  };
}
beforeEach(() => {
  databaseName = crypto.randomUUID();
  repository = new WorkoutRepository(databaseName);
});
afterEach(async () => {
  vi.restoreAllMocks();
  await repository.close();
});

describe('Локальные транзакции и очередь', () => {
  it('повтор действия не дублирует факт/очередь и не возвращает устаревшее состояние', async () => {
    await repository.commit(start());
    const command = add();
    await repository.commit(command);
    await repository.commit(add('op-two', 2));
    const replay = await repository.commit(structuredClone(command));
    expect(replay.revision).toBe(3);
    expect(replay.sets).toHaveLength(2);
    expect((await repository.read('alice')).pendingCount).toBe(3);
  });

  it('после закрытия и открытия соединения сохраняет занятие и очередь', async () => {
    await repository.commit(start());
    await repository.commit(add());
    await repository.close();
    repository = new WorkoutRepository(databaseName);
    const data = await repository.read('alice');
    expect(data.sessions[0].sets[0].weightKg).toBe(62.5);
    expect(data.pendingCount).toBe(2);
  });

  it('откатывает факт, если запись исходящей операции не удалась', async () => {
    await repository.commit(start());
    const original = IDBObjectStore.prototype.add;
    vi.spyOn(IDBObjectStore.prototype, 'add').mockImplementation(function (
      this: IDBObjectStore,
      value: unknown,
      key?: IDBValidKey,
    ) {
      if (this.name === 'outbox') throw new DOMException('Simulated quota', 'QuotaExceededError');
      return original.call(this, value, key);
    });
    await expect(repository.commit(add())).rejects.toThrow();
    const data = await repository.read('alice');
    expect(data.sessions[0].sets).toHaveLength(0);
    expect(data.sessions[0].revision).toBe(1);
    expect(data.pendingCount).toBe(1);
  });

  it('не перезаписывает результат из другой вкладки со старой ревизией', async () => {
    await repository.commit(start());
    const other = new WorkoutRepository(databaseName);
    try {
      await other.commit(add());
      await expect(repository.commit(add('stale', 1))).rejects.toThrow('другой вкладке');
      expect((await repository.read('alice')).sessions[0].sets).toHaveLength(1);
    } finally {
      await other.close();
    }
  });

  it('другой payload под прежним ID отклоняется', async () => {
    await repository.commit(start());
    const command = add();
    await repository.commit(command);
    const changed = structuredClone(command);
    if (changed.mutation.type === 'addSet') changed.mutation.values.weightKg = 100;
    await expect(repository.commit(changed)).rejects.toThrow('Идентификатор');
  });

  it('локальные разделы владельцев не смешиваются, это не замена серверной авторизации', async () => {
    await repository.commit(start());
    await repository.commit(start('bob'));
    await repository.commit(add());
    const bob = await repository.read('bob');
    expect(bob.pendingCount).toBe(1);
    expect(bob.sessions[0].sets).toHaveLength(0);
    const forged = start('alice', 'forged');
    if (forged.mutation.type === 'start') forged.mutation.session.ownerId = 'bob';
    await expect(repository.commit(forged)).rejects.toThrow();
  });

  it('одновременный старт не создаёт две активные тренировки владельца', async () => {
    const results = await Promise.allSettled([
      repository.commit(start('alice', 'one')),
      repository.commit(start('alice', 'two')),
    ]);
    expect(results.filter((r) => r.status === 'fulfilled')).toHaveLength(1);
    expect((await repository.read('alice')).sessions).toHaveLength(1);
  });
});
