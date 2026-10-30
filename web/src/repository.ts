import {
  applyMutation,
  ConflictError,
  WorkoutError,
  type Command,
  type PendingOperation,
  type Session,
} from './model';
import {
  operationFor,
  rebase,
  sessionFromWorkout,
  type PlanSnapshot,
  type Receipt,
  type Workout,
} from './sync-model';
import type { ProgramView } from './account';

function value<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
const stores = ['sessions', 'outbox', 'bases', 'meta', 'decisions'];
function stable(input: unknown): string {
  if (Array.isArray(input)) return `[${input.map(stable).join(',')}]`;
  if (input && typeof input === 'object')
    return `{${Object.entries(input)
      .filter(([, val]) => val !== undefined)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, val]) => `${JSON.stringify(key)}:${stable(val)}`)
      .join(',')}}`;
  return JSON.stringify(input);
}
export const unresolved = (op: PendingOperation) => op.status !== 'acked' && op.status !== 'superseded';
export interface SyncState {
  ownerId: string;
  id: 'state';
  deviceId: string;
  sequence: number;
  cursor?: string;
  program?: ProgramView;
  plans: PlanSnapshot[];
  refreshedAt?: string;
}
export interface LocalData {
  sessions: Session[];
  operations: PendingOperation[];
  bases: Workout[];
  state: SyncState;
  pendingCount: number;
  legacyCount: number;
}
interface OwnerData extends LocalData {
  transaction: IDBTransaction;
}

function openDatabase(name: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name, 2);
    let blocked = false;
    request.onupgradeneeded = () => {
      if (blocked) {
        request.transaction?.abort();
        return;
      }
      for (const name of stores) {
        if (!request.result.objectStoreNames.contains(name))
          request.result
            .createObjectStore(name, { keyPath: ['ownerId', 'id'] })
            .createIndex('ownerId', 'ownerId');
      }
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => {
      blocked = true;
      reject(new WorkoutError('Закрой другие вкладки Gym и повтори открытие. Данные не удалялись.'));
    };
    request.onsuccess = () => {
      const db = request.result;
      if (blocked) {
        db.close();
        return;
      }
      db.onversionchange = () => db.close();
      resolve(db);
    };
  });
}

export class WorkoutRepository {
  private database?: Promise<IDBDatabase>;
  constructor(private name = 'gym-local-v1') {}
  private db(): Promise<IDBDatabase> {
    return (this.database ??= openDatabase(this.name)
      .then((db) => {
        db.onversionchange = () => {
          db.close();
          this.database = undefined;
        };
        return db;
      })
      .catch((error) => {
        this.database = undefined;
        throw error;
      }));
  }
  private async transaction<T>(ownerId: string, write: boolean, perform: (data: OwnerData) => T): Promise<T> {
    const db = await this.db();
    const tx = db.transaction(stores, write ? 'readwrite' : 'readonly');
    let result: T;
    let failure: unknown;
    const done = new Promise<T>((resolve, reject) => {
      tx.oncomplete = () => resolve(result);
      tx.onabort = () =>
        reject(failure ?? tx.error ?? new WorkoutError('Не удалось сохранить на устройстве.'));
      tx.onerror = () => {
        failure ??= tx.error;
      };
    });
    void (async () => {
      try {
        const [sessions, operations, rows, state] = await Promise.all([
          value<Session[]>(tx.objectStore('sessions').index('ownerId').getAll(ownerId)),
          value<PendingOperation[]>(tx.objectStore('outbox').index('ownerId').getAll(ownerId)),
          value<{ workout: Workout }[]>(tx.objectStore('bases').index('ownerId').getAll(ownerId)),
          value<SyncState | undefined>(tx.objectStore('meta').get([ownerId, 'state'])),
        ]);
        operations.sort(
          (a, b) => (a.sequence ?? 0) - (b.sequence ?? 0) || a.createdAt.localeCompare(b.createdAt),
        );
        result = perform({
          transaction: tx,
          sessions,
          operations,
          bases: rows.map((b) => b.workout),
          state: state ?? { ownerId, id: 'state', deviceId: crypto.randomUUID(), sequence: 0, plans: [] },
          pendingCount: operations.filter(unresolved).length,
          legacyCount: operations.filter((op) => !op.wire && unresolved(op)).length,
        });
      } catch (error) {
        failure = error;
        tx.abort();
      }
    })();
    return done;
  }
  async read(ownerId: string): Promise<LocalData> {
    return this.transaction(ownerId, false, (data) => ({
      sessions: data.sessions.sort((a, b) => b.startedAt.localeCompare(a.startedAt)),
      operations: data.operations,
      bases: data.bases,
      state: data.state,
      pendingCount: data.pendingCount,
      legacyCount: data.legacyCount,
    }));
  }
  async commit(command: Command): Promise<Session> {
    return this.transaction(command.ownerId, true, (data) => {
      const { transaction: tx, sessions, operations, state } = data;
      const fingerprint = JSON.stringify({
        sessionId: command.sessionId,
        expectedRevision: command.expectedRevision,
        mutation: command.mutation,
      });
      const existing = operations.find((o) => o.id === command.id);
      const current = sessions.find((s) => s.id === command.sessionId);
      if (existing) {
        if (existing.fingerprint !== fingerprint)
          throw new WorkoutError('Идентификатор действия уже использован для другой записи.');
        if (!current) throw new WorkoutError('Сохранённая тренировка не найдена.');
        return current;
      }
      let saved: Session;
      if (command.mutation.type === 'start') {
        const session = command.mutation.session;
        if (
          current ||
          command.expectedRevision !== 0 ||
          session.revision !== 0 ||
          session.ownerId !== command.ownerId ||
          session.id !== command.sessionId ||
          session.status !== 'active' ||
          session.sets.length
        )
          throw new ConflictError();
        if (sessions.some((s) => s.status === 'active'))
          throw new WorkoutError('Уже есть активная тренировка. Обнови данные и продолжи её.');
        saved = { ...structuredClone(session), revision: 1 };
      } else {
        if (!current || current.revision !== command.expectedRevision) throw new ConflictError();
        saved = applyMutation(current, command.mutation, command.createdAt);
      }
      const predecessor = operations
        .filter((op) => op.sessionId === saved.id && op.wire && op.status !== 'superseded')
        .at(-1);
      const precondition =
        predecessor && predecessor.status !== 'acked'
          ? { after_operation_id: predecessor.id }
          : {
              server_revision:
                predecessor?.receipt?.outcome === 'applied'
                  ? Math.max(saved.serverRevision ?? 0, predecessor.receipt.server_revision)
                  : (saved.serverRevision ?? 0),
            };
      const wire = operationFor(command, saved, state.deviceId, precondition);
      state.sequence++;
      tx.objectStore('sessions').put(saved);
      tx.objectStore('outbox').add({
        ...structuredClone(command),
        fingerprint,
        status: 'pending',
        wire,
        sequence: state.sequence,
      } satisfies PendingOperation);
      tx.objectStore('meta').put(state);
      return saved;
    });
  }
  async updateOperation(
    owner: string,
    id: string,
    update: Partial<Pick<PendingOperation, 'status' | 'attempts' | 'nextAttemptAt' | 'reason'>>,
  ): Promise<boolean> {
    return this.transaction(owner, true, ({ transaction, operations }) => {
      const op = operations.find((op) => op.id === id);
      if (!op || !unresolved(op) || ['conflict', 'rejected'].includes(op.status)) return false;
      transaction.objectStore('outbox').put({ ...op, ...update });
      return true;
    });
  }
  async receipt(owner: string, id: string, receipt: Receipt): Promise<void> {
    return this.transaction(owner, true, (data) => {
      const op = data.operations.find((o) => o.id === id);
      if (!op?.wire || receipt.operation_id !== id || receipt.workout_id !== op.sessionId)
        throw new WorkoutError('Некорректная квитанция.');
      if (!unresolved(op)) return;
      op.receipt = receipt;
      op.status =
        receipt.outcome === 'applied'
          ? 'acked'
          : receipt.code === 'revision_conflict'
            ? 'conflict'
            : 'rejected';
      data.transaction.objectStore('outbox').put(op);
      const session = data.sessions.find((s) => s.id === op.sessionId);
      if (session && receipt.outcome === 'applied') {
        session.serverRevision = Math.max(session.serverRevision ?? 0, receipt.server_revision);
        data.transaction.objectStore('sessions').put(session);
      }
      if (receipt.outcome === 'rejected')
        for (const child of data.operations.filter(
          (o) => o.sessionId === op.sessionId && o.id !== id && unresolved(o),
        )) {
          child.status = 'conflict';
          child.reason = 'Зависимая правка остановлена. Разбери всю цепочку.';
          data.transaction.objectStore('outbox').put(child);
        }
      this.mergeWorkouts(
        data,
        data.bases.filter((w) => w.workout_id === op.sessionId),
      );
    });
  }
  private mergeWorkouts(data: OwnerData, workouts: Workout[]) {
    for (const workout of workouts) {
      if (workout.owner_id !== data.state.ownerId || workout.plan.owner_id !== data.state.ownerId)
        throw new WorkoutError('Ответ другого аккаунта.');
      const oldBase = data.bases.find((b) => b.workout_id === workout.workout_id);
      if (oldBase && oldBase.server_revision > workout.server_revision) continue;
      data.transaction
        .objectStore('bases')
        .put({ ownerId: workout.owner_id, id: workout.workout_id, workout });
      data.bases = [...data.bases.filter((w) => w.workout_id !== workout.workout_id), workout];
      const current = data.sessions.find((s) => s.id === workout.workout_id);
      if (current?.localOnly) continue;
      if (
        !data.operations.some((op) => op.sessionId === workout.workout_id && unresolved(op)) &&
        (!current || (current.serverRevision ?? 0) <= workout.server_revision)
      ) {
        const projected = sessionFromWorkout(workout, current?.revision);
        if (!current || stable({ ...current, revision: 0 }) !== stable({ ...projected, revision: 0 })) {
          data.transaction.objectStore('sessions').put(projected);
          data.sessions = [...data.sessions.filter((s) => s.id !== projected.id), projected];
        }
      }
    }
  }
  async receive(
    owner: string,
    input: {
      workouts?: Workout[];
      cursor?: string;
      plans?: PlanSnapshot[];
      program?: ProgramView;
      refreshedAt?: string;
    },
    expectedCursor?: string,
  ): Promise<boolean> {
    return this.transaction(owner, true, (data) => {
      if (expectedCursor !== undefined && (data.state.cursor ?? '') !== expectedCursor) return false;
      if (
        input.plans?.some((p) => p.owner_id !== owner) ||
        (input.program && input.program.owner_id !== owner)
      )
        throw new WorkoutError('Ответ другого аккаунта.');
      this.mergeWorkouts(data, input.workouts ?? []);
      const { workouts: _, ...metadata } = input;
      Object.assign(data.state, metadata);
      data.transaction.objectStore('meta').put(data.state);
      return true;
    });
  }
  async lease(
    owner: string,
    workoutId: string,
    holder: string,
    now: number,
    release = false,
  ): Promise<boolean> {
    const db = await this.db();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('meta', 'readwrite');
      const store = tx.objectStore('meta');
      const id = `lease:${workoutId}`;
      let acquired = false;
      const request = store.get([owner, id]);
      request.onsuccess = () => {
        const previous = request.result;
        if (previous && previous.holder !== holder && previous.until > now) return;
        acquired = true;
        store.put({ ownerId: owner, id, holder, until: release ? 0 : now + 30_000 });
      };
      tx.oncomplete = () => resolve(acquired);
      tx.onabort = () => reject(tx.error);
    });
  }
  async resolve(
    owner: string,
    localRevision: number,
    server: Workout,
    suffixIds: string[],
    choice: 'server' | 'local',
  ): Promise<Session> {
    return this.transaction(owner, true, (data) => {
      const local = data.sessions.find((s) => s.id === server.workout_id);
      const suffix = data.operations.filter((op) => op.sessionId === server.workout_id && unresolved(op));
      const latest = data.bases.find((w) => w.workout_id === server.workout_id);
      if (
        !local ||
        local.revision !== localRevision ||
        latest?.server_revision !== server.server_revision ||
        JSON.stringify(suffix.map((op) => op.id)) !== JSON.stringify(suffixIds) ||
        suffix.some((op) => op.status === 'sending')
      )
        throw new ConflictError();
      if (!suffix.length || suffix.some((op) => !op.wire))
        throw new WorkoutError('Эти записи доступны только локально.');
      const now = new Date().toISOString();
      const preview = choice === 'local' ? rebase(server, local, suffix, now) : undefined;
      let saved = sessionFromWorkout(server, local.revision);
      const replacementIds: string[] = [];
      for (const old of suffix) data.transaction.objectStore('outbox').put({ ...old, status: 'superseded' });
      for (const [index, mutation] of (preview?.mutations ?? []).entries()) {
        const occurredAt = preview!.times[index];
        const command: Command = {
          id: crypto.randomUUID(),
          ownerId: owner,
          sessionId: local.id,
          expectedRevision: saved.revision,
          createdAt: occurredAt,
          mutation,
        };
        saved = applyMutation(saved, mutation, occurredAt);
        const wire = operationFor(
          command,
          saved,
          data.state.deviceId,
          replacementIds.length
            ? { after_operation_id: replacementIds.at(-1)! }
            : { server_revision: server.server_revision },
        );
        data.state.sequence++;
        data.transaction.objectStore('outbox').add({
          ...command,
          fingerprint: JSON.stringify(command),
          wire,
          sequence: data.state.sequence,
          status: 'pending',
        } satisfies PendingOperation);
        replacementIds.push(command.id);
      }
      data.transaction.objectStore('decisions').add({
        ownerId: owner,
        id: crypto.randomUUID(),
        at: now,
        choice,
        serverRevision: server.server_revision,
        local,
        oldIds: suffixIds,
        replacementIds,
      });
      data.transaction.objectStore('sessions').put(saved);
      data.transaction.objectStore('meta').put(data.state);
      return saved;
    });
  }
  async export(owner: string): Promise<string> {
    const data = await this.read(owner);
    const db = await this.db();
    const tx = db.transaction('decisions', 'readonly');
    const decisions = await value(tx.objectStore('decisions').index('ownerId').getAll(owner));
    return JSON.stringify(
      { format: 'gym-local-backup-v1', exportedAt: new Date().toISOString(), ...data, decisions },
      null,
      2,
    );
  }
  async keepLocal(owner: string, id: string, revision: number, ids: string[]): Promise<void> {
    return this.transaction(owner, true, (data) => {
      const local = data.sessions.find((s) => s.id === id);
      const suffix = data.operations.filter((op) => op.sessionId === id && unresolved(op));
      if (
        !local ||
        local.revision !== revision ||
        JSON.stringify(suffix.map((op) => op.id)) !== JSON.stringify(ids)
      )
        throw new ConflictError();
      if (
        data.bases.some((w) => w.workout_id === id) ||
        suffix.some((op) => op.status === 'sending') ||
        !suffix.some((op) => op.status === 'rejected' && op.wire?.command.type === 'start_workout')
      )
        throw new WorkoutError('Сначала проверь серверную версию и исход отправки.');
      data.transaction.objectStore('decisions').add({
        ownerId: owner,
        id: crypto.randomUUID(),
        at: new Date().toISOString(),
        choice: 'local-only',
        local,
        oldIds: ids,
        replacementIds: [],
      });
      for (const op of suffix) data.transaction.objectStore('outbox').put({ ...op, status: 'superseded' });
      data.transaction
        .objectStore('sessions')
        .put({ ...local, localOnly: true, revision: local.revision + 1 });
    });
  }
  async close(): Promise<void> {
    (await this.database)?.close();
    this.database = undefined;
  }
}
