import { useState } from 'react';
import { rebase, sessionFromWorkout, type RebaseResult, type Workout } from './sync-model';
import { unresolved, type LocalData, WorkoutRepository } from './repository';
import type { Session } from './model';
import type { SyncCoordinator, SyncNotice } from './sync';

function Results({ session }: { session: Session }) {
  return (
    <div className="conflict-results">
      <strong>
        {session.status === 'completed'
          ? 'Завершена'
          : session.status === 'cancelled'
            ? 'Отменена'
            : 'Активна'}
      </strong>
      {session.completedAt && (
        <p className="caption">Завершена: {new Date(session.completedAt).toLocaleString('ru-RU')}</p>
      )}
      {session.sets
        .filter((s) => !s.deletedAt)
        .map((set) => (
          <p key={set.id}>
            {session.snapshot.exercises.find((e) => e.id === set.exerciseId)?.name}:{' '}
            <strong>
              {set.weightKg} кг × {set.reps}
            </strong>
            <span className="caption block">
              {set.kind === 'warmup'
                ? 'Разминка'
                : set.plannedTargetId
                  ? 'Плановый рабочий'
                  : 'Дополнительный'}
              {set.comparisonTarget
                ? ` · цель ${set.comparisonTarget.weightKg} кг × ${set.comparisonTarget.reps}`
                : ''}
            </span>
          </p>
        ))}
      {!session.sets.some((s) => !s.deletedAt) && <p>Без подходов</p>}
    </div>
  );
}
interface Review {
  local: Session;
  server: Workout;
  ids: string[];
  preview?: RebaseResult;
  unavailable?: string;
}
export default function SyncPanel({
  owner,
  data,
  repository,
  coordinator,
  notice,
  changed,
}: {
  owner: string;
  data?: LocalData;
  repository: WorkoutRepository;
  coordinator?: SyncCoordinator;
  notice: SyncNotice;
  changed: () => void;
}) {
  const [review, setReview] = useState<Review>();
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const problematic = [
    ...new Set(
      data?.operations
        .filter((op) => op.wire && ['conflict', 'rejected'].includes(op.status))
        .map((op) => op.sessionId) ?? [],
    ),
  ];
  const messages: Record<string, string> = {
    upgrade_required: 'Для отправки требуется обновление приложения.',
    snapshot_unavailable: 'Исходный план пока недоступен серверу.',
    validation_failed: 'Сервер отклонил правку. Проверь обе версии и значения.',
    operation_id_reused:
      'Не совпало содержание ранее отправленного действия. Сохрани локальную копию для разбора.',
  };
  async function keepLocal(id: string) {
    if (document.querySelector('[data-unsaved="true"]')) {
      setMessage('Сначала запиши подход или сбрось незаписанный ввод.');
      return;
    }
    if (
      !window.confirm(
        'Оставить эту тренировку только на устройстве? Её непринятая очередь будет сохранена в архиве решений; автоматически на сервер она больше не отправится.',
      )
    )
      return;
    try {
      const latest = await repository.read(owner);
      const session = latest.sessions.find((s) => s.id === id)!;
      await repository.keepLocal(
        owner,
        id,
        session.revision,
        latest.operations.filter((op) => op.sessionId === id && unresolved(op)).map((op) => op.id),
      );
      setMessage('Тренировка сохранена только на устройстве. Можно продолжить её локально.');
      changed();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось сохранить решение.');
    }
  }
  async function inspect(id: string) {
    setMessage('');
    setBusy(true);
    try {
      try {
        await coordinator?.refreshWorkout(id);
      } catch {
        /* Cached revision is safe: a new edit still checks the server revision. */
      }
      const current = await repository.read(owner);
      const local = current.sessions.find((s) => s.id === id);
      const server = current.bases.find((w) => w.workout_id === id);
      if (!local || !server)
        throw new Error(
          'Серверная версия пока недоступна. Повтори после подключения; локальная запись сохранена.',
        );
      const suffix = current.operations.filter((op) => op.sessionId === id && unresolved(op));
      let preview: RebaseResult | undefined, unavailable: string | undefined;
      try {
        preview = rebase(server, local, suffix, new Date().toISOString());
      } catch (error) {
        unavailable = error instanceof Error ? error.message : 'Не удалось подготовить правки.';
      }
      setReview({ local, server, ids: suffix.map((op) => op.id), preview, unavailable });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось открыть конфликт.');
    } finally {
      setBusy(false);
    }
  }
  async function resolve(choice: 'local' | 'server') {
    if (!review || busy) return;
    if (document.querySelector('[data-unsaved="true"]')) {
      setMessage('Сначала запиши подход или сбрось незаписанный ввод.');
      return;
    }
    if (
      !window.confirm(
        choice === 'server'
          ? `Оставить серверную версию? Все ${review.ids.length} непринятых действий этой цепочки останутся в локальном архиве решений и не будут отправлены.`
          : `Отправить показанные правки новой цепочкой (${review.ids.length} исходных действий)? Сервер снова проверит версию; возможен новый конфликт.`,
      )
    )
      return;
    setBusy(true);
    try {
      await repository.resolve(owner, review.local.revision, review.server, review.ids, choice);
      setReview(undefined);
      setMessage('Решение сохранено на устройстве.');
      changed();
      void coordinator?.run(true);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Не удалось сохранить решение.');
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="sync-panel card" aria-label="Синхронизация" data-syncing={notice.busy}>
      <div className="between">
        <strong>Синхронизация</strong>
        <button
          className="secondary"
          disabled={notice.busy || busy}
          onClick={() => void coordinator?.run(true)}
        >
          Синхронизировать
        </button>
      </div>
      <p role="status">{notice.message}</p>
      <p className="caption">
        {data ? `${data.pendingCount - data.legacyCount} действий ожидают доставки` : 'Читаем очередь…'}
        {problematic.length ? ` · конфликтов: ${problematic.length}` : ''}
      </p>
      {data?.state.refreshedAt && (
        <p className="caption">Планы проверены: {new Date(data.state.refreshedAt).toLocaleString('ru-RU')}</p>
      )}
      <details>
        <summary>Состояние локальной очереди</summary>
        <p className="caption">
          Ожидают подтверждения: {data?.operations.filter(unresolved).length ?? 0}. Самая ранняя запись:{' '}
          {data?.operations
            .filter(unresolved)
            .map((op) => op.createdAt)
            .sort()[0] ?? 'нет'}
          .
        </p>
        <p className="caption">
          Последнее подтверждение сервера:{' '}
          {data?.operations
            .flatMap((op) => (op.receipt?.outcome === 'applied' ? [op.receipt.accepted_at] : []))
            .sort()
            .at(-1) ?? 'ещё нет'}
          .
        </p>
      </details>
      {notice.needsLogin && (
        <a className="secondary" href="/auth/login?return_to=/app">
          Подтвердить вход
        </a>
      )}
      {!!data?.legacyCount && (
        <p className="caption">
          Локальные записи ({data.legacyCount} действий) сохранены только на этом устройстве и не отправляются
          автоматически. Это прежние записи или тренировки, для которых ты выбрал локальное хранение. Демо
          также остаётся отдельно.
        </p>
      )}
      {problematic.map((id) => (
        <div key={id} className="sync-issue">
          <p>{data?.sessions.find((s) => s.id === id)?.snapshot.name}: требуется разбор правок</p>
          {data?.operations
            .filter((op) => op.sessionId === id && op.status === 'rejected')
            .map((op) => (
              <p className="caption" key={op.id}>
                {messages[op.receipt?.outcome === 'rejected' ? op.receipt.code : (op.reason ?? '')] ??
                  'Отправка остановлена. Проверь версии; непринятые правки сохранены.'}
              </p>
            ))}
          <button className="secondary" disabled={busy} onClick={() => void inspect(id)}>
            Сравнить версии
          </button>
          {!data?.bases.some((w) => w.workout_id === id) &&
            data?.operations.some(
              (op) =>
                op.sessionId === id && op.status === 'rejected' && op.wire?.command.type === 'start_workout',
            ) && (
              <button className="secondary" disabled={busy} onClick={() => void keepLocal(id)}>
                Оставить только на устройстве
              </button>
            )}
        </div>
      ))}
      {review && (
        <div className="conflict-review" role="region" aria-label="Разбор конфликта">
          <h2>Выбери версию тренировки</h2>
          <p>
            Разбираем всю непринятую цепочку: {review.ids.length} действий. Записи после конфликтующей правки
            тоже включены.
          </p>
          <div className="conflict-columns">
            <section>
              <h3>На сервере</h3>
              <Results session={sessionFromWorkout(review.server)} />
            </section>
            <section>
              <h3>На устройстве</h3>
              <Results session={review.local} />
            </section>
          </div>
          {review.preview && (
            <section>
              <h3>После применения твоих правок</h3>
              <Results session={review.preview.session} />
              {review.preview.warnings.map((text) => (
                <p className="error" key={text}>
                  {text}
                </p>
              ))}
            </section>
          )}
          {review.unavailable && (
            <p className="error">
              Эти правки нельзя применить к текущей серверной версии: {review.unavailable} Сохрани локальную
              копию перед выбором.
            </p>
          )}
          <div className="account-actions">
            <button className="secondary" disabled={busy} onClick={() => void resolve('server')}>
              Оставить серверную версию
            </button>
            <button
              className="primary"
              disabled={busy || !review.preview}
              onClick={() => void resolve('local')}
            >
              Применить мои правки
            </button>
            <button className="secondary" onClick={() => setReview(undefined)}>
              Решить позже
            </button>
          </div>
        </div>
      )}
      {message && <p role="alert">{message}</p>}
      <details>
        <summary>Локальная копия данных</summary>
        <p className="caption">
          JSON содержит твои тренировки, очередь и архив решений. Храни его как личные данные. Cookies и
          токенов входа в нём нет.
        </p>
        <button
          className="secondary"
          onClick={() => {
            void repository
              .export(owner)
              .then((text) => {
                const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
                const link = document.createElement('a');
                link.href = url;
                link.download = 'gym-local-backup.json';
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
              })
              .catch(() => setMessage('Не удалось выгрузить локальную копию. Данные не удалялись.'));
          }}
        >
          Скачать локальную копию
        </button>
      </details>
    </section>
  );
}
