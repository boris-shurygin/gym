import {
  applyMutation,
  ConflictError,
  WorkoutError,
  type Command,
  type PendingOperation,
  type Session,
} from './model';

function requestValue<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function openDatabase(name: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      for (const name of ['sessions', 'outbox']) {
        db.createObjectStore(name, { keyPath: ['ownerId', 'id'] }).createIndex('ownerId', 'ownerId');
      }
    };
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new WorkoutError('Закрой другие вкладки Gym и повтори открытие.'));
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => db.close();
      resolve(db);
    };
  });
}

export class WorkoutRepository {
  private database?: Promise<IDBDatabase>;
  constructor(private name = 'gym-local-v1') {}
  private db(): Promise<IDBDatabase> {
    return (this.database ??= openDatabase(this.name).catch((error) => {
      this.database = undefined;
      throw error;
    }));
  }

  async read(ownerId: string): Promise<{ sessions: Session[]; pendingCount: number }> {
    const db = await this.db();
    const transaction = db.transaction(['sessions', 'outbox'], 'readonly');
    const [sessions, operations] = await Promise.all([
      requestValue<Session[]>(transaction.objectStore('sessions').index('ownerId').getAll(ownerId)),
      requestValue<PendingOperation[]>(transaction.objectStore('outbox').index('ownerId').getAll(ownerId)),
    ]);
    return {
      sessions: sessions.sort((a, b) => b.startedAt.localeCompare(a.startedAt)),
      pendingCount: operations.length,
    };
  }

  async commit(command: Command): Promise<Session> {
    const db = await this.db();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['sessions', 'outbox'], 'readwrite');
      const sessions = transaction.objectStore('sessions');
      const outbox = transaction.objectStore('outbox');
      let result: Session | undefined;
      let failure: unknown;
      const abort = (error: unknown) => {
        failure = error;
        transaction.abort();
      };
      const fingerprint = JSON.stringify({
        sessionId: command.sessionId,
        expectedRevision: command.expectedRevision,
        mutation: command.mutation,
      });
      const existingRequest = outbox.get([command.ownerId, command.id]);
      transaction.oncomplete = () =>
        result ? resolve(result) : reject(new WorkoutError('Запись не завершена.'));
      transaction.onabort = () =>
        reject(failure ?? transaction.error ?? new WorkoutError('Не удалось сохранить на устройстве.'));
      transaction.onerror = () => {
        failure ??= transaction.error;
      };
      existingRequest.onsuccess = () => {
        const existing = existingRequest.result as PendingOperation | undefined;
        if (existing && existing.fingerprint !== fingerprint) {
          abort(new WorkoutError('Идентификатор действия уже использован для другой записи.'));
          return;
        }
        const currentRequest = sessions.get([command.ownerId, command.sessionId]);
        currentRequest.onsuccess = () => {
          try {
            const current = currentRequest.result as Session | undefined;
            // A retry returns the latest local state, never an obsolete pre-edit snapshot.
            if (existing) {
              if (!current) throw new WorkoutError('Сохранённая тренировка не найдена.');
              result = current;
              return;
            }
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
              const allRequest = sessions.index('ownerId').getAll(command.ownerId);
              allRequest.onsuccess = () => {
                try {
                  if ((allRequest.result as Session[]).some((s) => s.status === 'active')) {
                    throw new WorkoutError('Уже есть активная тренировка. Обнови данные и продолжи её.');
                  }
                  persist({ ...structuredClone(session), revision: 1 });
                } catch (error) {
                  abort(error);
                }
              };
            } else {
              if (!current || current.revision !== command.expectedRevision) throw new ConflictError();
              persist(applyMutation(current, command.mutation, command.createdAt));
            }
          } catch (error) {
            abort(error);
          }
        };
      };
      function persist(session: Session) {
        // Resolve only at transaction.oncomplete: neither write is visible alone.
        sessions.put(session);
        outbox.add({
          ...structuredClone(command),
          fingerprint,
          status: 'pending',
        } satisfies PendingOperation);
        result = session;
      }
    });
  }

  async close(): Promise<void> {
    (await this.database)?.close();
    this.database = undefined;
  }
}
