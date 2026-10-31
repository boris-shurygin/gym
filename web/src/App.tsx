import { useEffect, useRef, useState } from 'react';
import {
  createSession,
  grams,
  exerciseSets,
  nextTarget,
  newSetDefaults,
  volumeRatio,
  WorkoutError,
  type Command,
  type Exercise,
  type Mutation,
  type Session,
  type SetKind,
  type SetResult,
  type Program,
} from './model';
import { WorkoutRepository } from './repository';
import type { LocalData } from './repository';
import { programFromPlans } from './sync-model';
import { SyncCoordinator, type SyncNotice } from './sync';
import SyncPanel from './SyncPanel';

type View =
  | { kind: 'home' }
  | { kind: 'history' }
  | { kind: 'preview'; templateId: string }
  | { kind: 'session'; sessionId: string }
  | { kind: 'exercise'; sessionId: string; exerciseId: string; editSetId?: string };
interface Draft {
  weight: string;
  reps: string;
  kind: SetKind;
  revision: number;
}
const repository = new WorkoutRepository();
function announceChange() {
  try {
    localStorage.setItem('gym.data-change', crypto.randomUUID());
  } catch {
    /* Notification is optional; the IndexedDB commit is authoritative. */
  }
}
const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
const percentage = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const date = (value: string) =>
  new Date(value).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' });
const format = (value: number) => number.format(value);
const plural = (value: number, forms: [string, string, string]) =>
  forms[
    value % 100 >= 11 && value % 100 <= 19
      ? 2
      : value % 10 === 1
        ? 0
        : value % 10 >= 2 && value % 10 <= 4
          ? 1
          : 2
  ];

function Volume({ set }: { set: SetResult }) {
  const ratio = volumeRatio(set);
  if (ratio === null)
    return (
      <p className="caption">
        {set.kind === 'warmup'
          ? 'Разминка · вне рабочего плана'
          : 'Нет положительного планового объёма для сравнения'}
      </p>
    );
  const percent = ratio * 100;
  const target = set.comparisonTarget!;
  return (
    <div className="volume">
      <div
        className="volume-track"
        role="progressbar"
        aria-label="Объём относительно плана"
        aria-valuemin={0}
        aria-valuemax={Math.max(130, percent)}
        aria-valuenow={percent}
        aria-valuetext={`${percentage.format(percent)}% от плана`}
      >
        <div className="volume-fill" style={{ width: `${Math.min(100, percent / 1.3)}%` }} />
        <span className="plan-marker" />
      </div>
      <div className="between caption">
        <span>
          {format(set.weightKg * set.reps)} / {format(target.weightKg * target.reps)} кг·повт.
        </span>
        <strong>
          {percentage.format(percent)}%{percent > 130 ? ' ↑' : ''}
        </strong>
      </div>
      <p className="caption">
        План: {format(target.weightKg)} кг × {target.reps} · отметка 100%
      </p>
    </div>
  );
}

export default function App({
  ownerId,
  program: suppliedProgram,
  demo = false,
}: {
  ownerId: string;
  program: Program;
  demo?: boolean;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [pending, setPending] = useState(0);
  const [view, setView] = useState<View>({ kind: 'home' });
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState('');
  const [offline, setOffline] = useState(!navigator.onLine);
  const [offlineReady, setOfflineReady] = useState(false);
  const [data, setData] = useState<LocalData>();
  const [notice, setNotice] = useState<SyncNotice>({
    busy: false,
    message: 'Записи сохраняются на устройстве',
  });
  const [coordinator, setCoordinator] = useState<SyncCoordinator>();
  const [syncEpoch, setSyncEpoch] = useState(0);
  const program = demo
    ? suppliedProgram
    : data?.state.plans.length
      ? programFromPlans(ownerId, data.state.plans)
      : { ...suppliedProgram, templates: [] };
  const active = sessions.find((s) => s.status === 'active');
  const session = 'sessionId' in view ? sessions.find((s) => s.id === view.sessionId) : undefined;

  async function load(reset = false) {
    if (
      reset &&
      Object.keys(drafts).length &&
      !window.confirm('Обновить экран и сбросить незаписанный ввод? Сохранённые подходы останутся.')
    )
      return;
    try {
      const data = await repository.read(ownerId);
      setSessions(data.sessions);
      setPending(data.pendingCount);
      setData(data);
      if (reset) {
        setError('');
        setView({ kind: 'home' });
        setDrafts({});
      }
    } catch {
      setError('Не удалось открыть локальные данные. Записи не сбрасывались. Попробуй обновить данные.');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);
  const refreshRef = useRef(load);
  refreshRef.current = load;
  useEffect(() => {
    if (demo) return;
    const changed = () => {
      void refreshRef.current();
      announceChange();
    };
    const sync = new SyncCoordinator(
      repository,
      ownerId,
      () => localStorage.getItem('gym.active-owner') === ownerId && !localStorage.getItem('gym.hidden'),
      changed,
      setNotice,
      undefined,
      () => navigator.onLine && document.visibilityState === 'visible',
    );
    setCoordinator(sync);
    const run = () => {
      if (navigator.onLine && document.visibilityState === 'visible') void sync.run();
    };
    const storage = (event: StorageEvent) => {
      if (event.key === 'gym.data-change') void refreshRef.current();
      if (event.key === 'gym.hidden' || event.key === 'gym.active-owner') sync.stop();
    };
    const stop = () => sync.stop();
    const ready = () => setSyncEpoch((value) => value + 1);
    window.addEventListener('online', run);
    document.addEventListener('visibilitychange', run);
    window.addEventListener('storage', storage);
    window.addEventListener('gym-profile-closing', stop);
    window.addEventListener('gym-account-ready', ready);
    run();
    return () => {
      sync.stop();
      window.removeEventListener('online', run);
      document.removeEventListener('visibilitychange', run);
      window.removeEventListener('storage', storage);
      window.removeEventListener('gym-profile-closing', stop);
      window.removeEventListener('gym-account-ready', ready);
    };
  }, [ownerId, demo, syncEpoch]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (Object.keys(drafts).length) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [drafts]);
  useEffect(() => {
    const update = () => setOffline(!navigator.onLine);
    window.addEventListener('online', update);
    window.addEventListener('offline', update);
    if (import.meta.env.PROD && 'serviceWorker' in navigator) {
      void navigator.serviceWorker.ready
        .then(() => setOfflineReady(true))
        .catch(() => setOfflineReady(false));
    }
    return () => {
      window.removeEventListener('online', update);
      window.removeEventListener('offline', update);
    };
  }, []);

  async function execute(
    mutation: Mutation,
    sessionId: string,
    revision: number,
    nextView: View,
    draftKey?: string,
  ) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    const command: Command = {
      id: crypto.randomUUID(),
      ownerId,
      sessionId,
      expectedRevision: revision,
      mutation,
      createdAt: new Date().toISOString(),
    };
    try {
      const saved = await repository.commit(command);
      setSessions((previous) => [saved, ...previous.filter((s) => s.id !== saved.id)]);
      setPending((previous) => previous + 1);
      setDrafts((previous) =>
        Object.fromEntries(
          Object.entries(previous)
            .filter(([key]) => key !== draftKey)
            .map(([key, value]) => [
              key,
              key.startsWith(`${sessionId}:`) ? { ...value, revision: saved.revision } : value,
            ]),
        ),
      );
      setView(nextView);
      void load();
      announceChange();
      if (!demo && navigator.onLine) void coordinator?.run();
    } catch (error) {
      setError(
        error instanceof WorkoutError
          ? error.message
          : 'Не удалось сохранить на устройстве. Ввод оставлен для повтора.',
      );
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function start(templateId: string) {
    try {
      const created = createSession(
        program,
        templateId,
        ownerId,
        crypto.randomUUID(),
        new Date().toISOString(),
      );
      void execute({ type: 'start', session: created }, created.id, 0, {
        kind: 'session',
        sessionId: created.id,
      });
    } catch (error) {
      setError(error instanceof Error ? error.message : 'Не удалось подготовить тренировку.');
    }
  }

  function sessionOverview(current: Session) {
    const count = current.sets.filter((s) => !s.deletedAt && s.kind === 'working').length;
    const plan = current.snapshot.exercises.reduce((sum, exercise) => sum + exercise.targets.length, 0);
    const hasResults = current.sets.some((s) => !s.deletedAt);
    return (
      <>
        <button
          className="back"
          onClick={() => setView({ kind: current.status === 'active' ? 'home' : 'history' })}
        >
          ‹ Назад
        </button>
        <p className="eyebrow">
          {current.status === 'active' ? 'Активная тренировка' : 'Завершённая тренировка'} ·{' '}
          {date(current.startedAt)}
        </p>
        <h1>{current.snapshot.name}</h1>
        {!demo && (!current.snapshot.planSnapshot || current.localOnly) && (
          <p className="caption">Старая локальная запись · сохранена только на устройстве</p>
        )}
        {current.snapshot.planSnapshot && (
          <p className="caption">
            План от {date(current.snapshot.planSnapshot.calculated_at)} · версия{' '}
            {current.programVersionId.slice(0, 12)}. План этой тренировки не изменится после обновления
            программы.
          </p>
        )}
        {current.snapshot.planSnapshot?.calculator_version === 'fixed-v1' && (
          <p className="caption">
            Цели взяты из одобренной программы. Исправление результатов обновит показатели, сохранив исходный
            план. Вес и повторения изменятся только после одобрения новой программы.
          </p>
        )}
        <section className="card summary">
          <div className="between">
            <span>Рабочие подходы</span>
            <strong>
              {count} / {plan}
            </strong>
          </div>
          <div className="count-track">
            <span style={{ width: `${Math.min(100, (count / plan) * 100)}%` }} />
          </div>
          <p className="caption">План и фактические результаты сохраняются отдельно</p>
        </section>
        <div className="section-title">
          Упражнения <span>{current.snapshot.exercises.length}</span>
        </div>
        <div className="exercise-list">
          {current.snapshot.exercises.map((exercise, i) => {
            const done = exerciseSets(current, exercise.id).filter((s) => s.kind === 'working').length;
            return (
              <button
                key={exercise.id}
                className="exercise-row"
                onClick={() => setView({ kind: 'exercise', sessionId: current.id, exerciseId: exercise.id })}
              >
                <span className={`exercise-number ${done >= exercise.targets.length ? 'done' : ''}`}>
                  {done >= exercise.targets.length ? '✓' : i + 1}
                </span>
                <span className="grow">
                  <strong>{exercise.name}</strong>
                  <span className="caption block">{exercise.equipment}</span>
                  <span className="caption block">
                    {done} / {exercise.targets.length} рабочих
                    {done > exercise.targets.length
                      ? ` · +${done - exercise.targets.length} дополнительно`
                      : ''}
                  </span>
                </span>
                <span className="chevron">›</span>
              </button>
            );
          })}
        </div>
        {current.status === 'active' && (
          <button
            className="primary wide finish"
            disabled={busy}
            onClick={() => {
              if (!hasResults) {
                if (window.confirm('Отменить пустую тренировку? Сохранённых подходов в ней нет.')) {
                  void execute({ type: 'cancelEmpty' }, current.id, current.revision, { kind: 'home' });
                }
              } else if (
                window.confirm(
                  `Завершить тренировку? Записано ${count} из ${plan} рабочих подходов. Невыполненные подходы не будут записаны как нули. Результаты можно исправить в истории.`,
                )
              ) {
                void execute({ type: 'complete' }, current.id, current.revision, { kind: 'history' });
              }
            }}
          >
            {hasResults ? 'Завершить тренировку' : 'Отменить пустую тренировку'}
          </button>
        )}
        {current.status === 'completed' && (
          <p className="caption">Нажми на упражнение, чтобы просмотреть или исправить результат.</p>
        )}
      </>
    );
  }

  function exerciseEditor(current: Session, exercise: Exercise, editSetId?: string) {
    const sets = exerciseSets(current, exercise.id);
    const editing = sets.find((s) => s.id === editSetId);
    const removedWhileEditing = Boolean(editSetId && !editing);
    const next = nextTarget(current, exercise.id);
    const defaults = newSetDefaults(current, exercise.id, 'working');
    const key = `${current.id}:${exercise.id}:${editSetId ?? 'new'}`;
    const draft = drafts[key] ?? {
      weight: String(editing?.weightKg ?? defaults.weightKg),
      reps: String(editing?.reps ?? defaults.reps),
      kind: editing?.kind ?? 'working',
      revision: current.revision,
    };
    const update = (value: Partial<Draft>) =>
      setDrafts((previous) => ({ ...previous, [key]: { ...draft, ...value } }));
    const goBack: View = { kind: 'session', sessionId: current.id };
    const newSetView: View = { kind: 'exercise', sessionId: current.id, exerciseId: exercise.id };
    const workingCount = sets.filter((s) => s.kind === 'working').length;
    const weightText = draft.weight.trim().replace(',', '.');
    const weightNumber = /^(?:\d+(?:\.\d{1,3})?|\.\d{1,3})$/.test(weightText) ? Number(weightText) : NaN;
    const repsNumber = /^\d+$/.test(draft.reps.trim()) ? Number(draft.reps) : NaN;
    return (
      <>
        <button className="back" onClick={() => setView(goBack)}>
          ‹ К упражнениям
        </button>
        <p className="eyebrow">{current.snapshot.name}</p>
        <h1>{exercise.name}</h1>
        <p className="subheading">{exercise.equipment}</p>
        <section className="card plan">
          <span className="eyebrow">План упражнения</span>
          <strong>{exercise.targets.map((t) => `${format(t.weightKg)} × ${t.reps}`).join(' / ')}</strong>
          <p className="caption">
            Вес в кг · {workingCount} / {exercise.targets.length} рабочих подходов
          </p>
        </section>
        {removedWhileEditing && (
          <section className="error" role="alert">
            <p>
              Этот подход удалён на другом устройстве. Незаписанный ввод оставлен; исправление не превратится
              в новый подход автоматически.
            </p>
            <button
              className="secondary"
              type="button"
              onClick={() => {
                if (window.confirm('Сбросить незаписанный ввод удалённого подхода и открыть новую форму?')) {
                  setDrafts((previous) => {
                    const copy = { ...previous };
                    delete copy[key];
                    return copy;
                  });
                  setView(newSetView);
                }
              }}
            >
              Сбросить ввод удалённого подхода
            </button>
          </section>
        )}
        <form
          className="card editor"
          onSubmit={(event) => {
            event.preventDefault();
            const values = {
              kind: draft.kind,
              weightKg: weightNumber,
              reps: repsNumber,
            };
            const mutation: Mutation = editSetId
              ? { type: 'editSet', setId: editSetId, values }
              : { type: 'addSet', exerciseId: exercise.id, setId: crypto.randomUUID(), values };
            void execute(mutation, current.id, draft.revision, newSetView, key);
          }}
        >
          <h2>
            {editing
              ? 'Исправление подхода'
              : draft.kind === 'warmup'
                ? 'Новый разминочный подход'
                : `Рабочий подход ${workingCount + 1}`}
          </h2>
          {!editing && draft.kind === 'working' && !next.plannedTargetId && (
            <p className="caption">Дополнительный · цель последнего планового подхода</p>
          )}
          <div className="kind-switch" aria-label="Тип подхода">
            {(['working', 'warmup'] as const).map((kind) => (
              <button
                key={kind}
                type="button"
                disabled={busy}
                aria-pressed={draft.kind === kind}
                onClick={() => {
                  if (kind === draft.kind) return;
                  const values = newSetDefaults(current, exercise.id, kind);
                  update({
                    kind,
                    ...(!editSetId
                      ? {
                          weight: String(values.weightKg),
                          reps: String(values.reps),
                        }
                      : {}),
                  });
                }}
              >
                {kind === 'working' ? 'Рабочий' : 'Разминка'}
              </button>
            ))}
          </div>
          {!editSetId && draft.kind === 'warmup' && (
            <p className="caption">
              По умолчанию: 70% рабочего веса и повторения × 1,5, округлённые вверх (до 1000). Значения можно
              изменить.
            </p>
          )}
          {!editSetId &&
            draft.kind === 'working' &&
            (defaults.weightKg !== next.target.weightKg || defaults.reps !== next.target.reps) && (
              <p className="caption">
                Ввод перенесён из предыдущего рабочего подхода. Сравнение с планом остаётся прежним.
              </p>
            )}
          <div className="input-row">
            <div className="input-column">
              <label>
                Вес, кг
                <input
                  inputMode="decimal"
                  aria-label="Вес, кг"
                  disabled={busy}
                  value={draft.weight}
                  onChange={(e) => update({ weight: e.target.value })}
                />
              </label>
              <div className="stepper">
                <button
                  type="button"
                  aria-label="Уменьшить вес"
                  disabled={
                    !Number.isFinite(weightNumber) ||
                    weightNumber > 2000 ||
                    weightNumber < exercise.weightStepKg ||
                    !/^\d+(\.\d{1,3})?$/.test(String(weightNumber))
                  }
                  onClick={() =>
                    update({ weight: String((grams(weightNumber) - grams(exercise.weightStepKg)) / 1000) })
                  }
                >
                  −
                </button>
                <span>{format(exercise.weightStepKg)} кг</span>
                <button
                  type="button"
                  aria-label="Увеличить вес"
                  disabled={
                    !Number.isFinite(weightNumber) ||
                    weightNumber > 2000 - exercise.weightStepKg ||
                    !/^\d+(\.\d{1,3})?$/.test(String(weightNumber))
                  }
                  onClick={() =>
                    update({ weight: String((grams(weightNumber) + grams(exercise.weightStepKg)) / 1000) })
                  }
                >
                  +
                </button>
              </div>
            </div>
            <span className="multiply">×</span>
            <div className="input-column">
              <label>
                Повторения
                <input
                  inputMode="numeric"
                  aria-label="Повторения"
                  disabled={busy}
                  value={draft.reps}
                  onChange={(e) => update({ reps: e.target.value })}
                />
              </label>
              <div className="stepper">
                <button
                  type="button"
                  aria-label="Уменьшить повторения"
                  disabled={!Number.isSafeInteger(repsNumber) || repsNumber <= 1}
                  onClick={() => update({ reps: String(repsNumber - 1) })}
                >
                  −
                </button>
                <span>1 повт.</span>
                <button
                  type="button"
                  aria-label="Увеличить повторения"
                  disabled={!Number.isSafeInteger(repsNumber) || repsNumber >= 1000}
                  onClick={() => update({ reps: String(repsNumber + 1) })}
                >
                  +
                </button>
              </div>
            </div>
          </div>
          <button className="primary wide" disabled={busy || removedWhileEditing}>
            {busy ? 'Сохраняем…' : editing ? 'Сохранить изменения' : 'Записать подход'}
          </button>
          {!editing && drafts[key] && (
            <button
              type="button"
              className="back"
              onClick={() => {
                if (window.confirm('Сбросить только незаписанный ввод? Сохранённые подходы останутся.'))
                  setDrafts((previous) => {
                    const copy = { ...previous };
                    delete copy[key];
                    return copy;
                  });
              }}
            >
              Сбросить ввод
            </button>
          )}
          {editing && (
            <div className="between edit-actions">
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => {
                  if (
                    window.confirm(
                      `Удалить подход ${format(editing.weightKg)} кг × ${editing.reps}? Остальные результаты останутся.`,
                    )
                  ) {
                    void execute(
                      { type: 'deleteSet', setId: editing.id },
                      current.id,
                      draft.revision,
                      newSetView,
                      key,
                    );
                  }
                }}
              >
                Удалить подход
              </button>
              <button
                type="button"
                className="back"
                onClick={() => {
                  setDrafts((previous) => {
                    const copy = { ...previous };
                    delete copy[key];
                    return copy;
                  });
                  setView(newSetView);
                }}
              >
                Отмена
              </button>
            </div>
          )}
        </form>
        <div className="section-title">
          Записанные подходы <span>{sets.length}</span>
        </div>
        {!sets.length && <div className="empty">Пока нет записанных подходов</div>}
        {sets.map((set, index) => (
          <article className="card record" key={set.id}>
            <button
              className="record-button"
              aria-label={`Редактировать подход ${index + 1}`}
              onClick={() =>
                setView({
                  kind: 'exercise',
                  sessionId: current.id,
                  exerciseId: exercise.id,
                  editSetId: set.id,
                })
              }
            >
              <div className="between">
                <span>
                  <strong>{index + 1}.</strong>{' '}
                  {set.kind === 'warmup' ? 'Разминка' : set.plannedTargetId ? 'Рабочий' : 'Дополнительный'}
                </span>
                <strong>
                  {format(set.weightKg)} кг × {set.reps} <span className="chevron">›</span>
                </strong>
              </div>
              <Volume set={set} />
            </button>
          </article>
        ))}
      </>
    );
  }

  let content;
  if (loading) content = <div className="empty">Открываем сохранённые тренировки…</div>;
  else if (view.kind === 'exercise' && session) {
    const exercise = session.snapshot.exercises.find((e) => e.id === view.exerciseId);
    content = exercise ? exerciseEditor(session, exercise, view.editSetId) : <p>Упражнение не найдено.</p>;
  } else if (view.kind === 'session' && session) content = sessionOverview(session);
  else if (view.kind === 'preview' && program.templates.some((t) => t.id === view.templateId)) {
    const template = program.templates.find((t) => t.id === view.templateId)!;
    content = (
      <>
        <button className="back" onClick={() => setView({ kind: 'home' })}>
          ‹ К шаблонам
        </button>
        <p className="eyebrow">Предпросмотр</p>
        <h1>{template.name}</h1>
        <p className="subheading">{template.description}</p>
        <div className="card">
          {template.exercises.map((exercise) => (
            <div className="preview-row" key={exercise.id}>
              <strong>{exercise.name}</strong>
              <span className="caption block">
                {exercise.targets.length} × {exercise.targets[0].reps} ·{' '}
                {format(exercise.targets[0].weightKg)} кг
              </span>
            </div>
          ))}
        </div>
        <button
          className="primary wide finish"
          disabled={busy || !!active}
          onClick={() => start(template.id)}
        >
          Начать тренировку
        </button>
      </>
    );
  } else if (view.kind === 'history') {
    const history = sessions.filter((s) => s.status === 'completed');
    content = (
      <>
        <p className="eyebrow">Твои записи на этом устройстве</p>
        <h1>История тренировок</h1>
        {!history.length && <div className="card empty">Завершённые тренировки появятся здесь</div>}
        {history.map((current) => (
          <button
            className="card history-card"
            key={current.id}
            onClick={() => setView({ kind: 'session', sessionId: current.id })}
          >
            <span className="caption">{date(current.startedAt)}</span>
            <strong>{current.snapshot.name}</strong>
            <span className="caption">
              {current.sets.filter((s) => !s.deletedAt && s.kind === 'working').length}{' '}
              {plural(current.sets.filter((s) => !s.deletedAt && s.kind === 'working').length, [
                'рабочий подход',
                'рабочих подхода',
                'рабочих подходов',
              ])}{' '}
              · сохранено на устройстве
            </span>
            <span className="chevron">›</span>
          </button>
        ))}
      </>
    );
  } else
    content = (
      <>
        <p className="eyebrow">Тренируйся в своём ритме</p>
        <h1>Твоя тренировка</h1>
        <p className="subheading">
          {active || program.templates.length
            ? 'Выбери шаблон или продолжи начатое'
            : 'Здесь будет твой план и история занятий'}
        </p>
        {sessions
          .filter((s) => s.status === 'active')
          .map((active) => (
            <section className="card active-card" key={active.id}>
              <span className="eyebrow">Уже начата</span>
              <h2>{active.snapshot.name}</h2>
              <p className="caption">Начата: {new Date(active.startedAt).toLocaleString('ru-RU')}</p>
              <button
                className="primary wide"
                onClick={() => setView({ kind: 'session', sessionId: active.id })}
              >
                Продолжить тренировку
              </button>
            </section>
          ))}
        <div className="section-title">
          {demo ? 'Тестовая программа' : 'Твоя программа'}{' '}
          <span>
            {program.templates.length
              ? `${program.templates.length} ${plural(program.templates.length, ['шаблон', 'шаблона', 'шаблонов'])}`
              : 'Пока пусто'}
          </span>
        </div>
        {!demo && !!program.templates.length && (
          <p className="caption">
            План от {date(program.templates[0].planSnapshot!.calculated_at)} · версия{' '}
            {program.versionId.slice(0, 12)}
            {!data?.state.refreshedAt || Date.now() - Date.parse(data.state.refreshedAt) > 7 * 86400_000
              ? ' · давно не обновлялся, цели могут быть устаревшими'
              : ''}
            {data?.state.program?.version &&
            data.state.program.version.program_version_id !== program.versionId
              ? ' · опубликована новая программа, сохранился прежний план'
              : ''}
          </p>
        )}
        {!demo && program.templates[0]?.planSnapshot?.calculator_version === 'fixed-v1' && (
          <p className="caption">
            Нагрузка задана в одобренной программе без округления. Выполнение, пропуск или перерыв сами не
            меняют цели. Для изменения веса и повторений одобри новую программу.
          </p>
        )}
        {program.templates.map((template, i) => (
          <button
            className="card template-card"
            key={template.id}
            disabled={!!active}
            onClick={() => setView({ kind: 'preview', templateId: template.id })}
          >
            <span className={`template-icon icon-${i}`}>{i === 0 ? '◈' : '↟'}</span>
            <span className="grow">
              <strong>{template.name}</strong>
              <span className="caption block">{template.description}</span>
              <span className="caption block">
                {template.exercises.length} упражнения ·{' '}
                {template.exercises.reduce((n, e) => n + e.targets.length, 0)} подходов
              </span>
            </span>
            <span className="chevron">›</span>
          </button>
        ))}
        {demo && (
          <p className="footnote">Цели в этой программе — синтетические примеры для проверки приложения.</p>
        )}
        {!demo && program.templates.length === 0 && (
          <section className="card empty-program">
            <span className="template-icon icon-0" aria-hidden="true">
              ◈
            </span>
            <h2>
              {suppliedProgram.templates.length
                ? 'Нужно загрузить планы тренировок'
                : 'Программа ещё не опубликована'}
            </h2>
            <p className="subheading">
              {suppliedProgram.templates.length
                ? 'Для нового занятия сначала подключись к сети и синхронизируй планы. Уже начатые тренировки можно продолжить.'
                : 'После публикации здесь появятся твои шаблоны тренировок.'}
            </p>
            <p className="caption">
              Когда программа будет готова, нажми «Обновить программу» в блоке аккаунта.
            </p>
          </section>
        )}
      </>
    );

  return (
    <div
      className={`app-shell ${view.kind === 'exercise' ? 'exercise-screen' : ''}`}
      data-unsaved={Object.keys(drafts).length > 0}
      data-saving={busy}
    >
      <header className="app-header">
        <a href="/" aria-label="Gym — на главную" className="brand">
          gym<span>↗</span>
        </a>
        <span className="mode-badge">{demo ? 'Локальная версия' : 'Личный профиль'}</span>
      </header>
      <div className="storage-status" role="status">
        <span className="status-dot" />
        Сохранено на устройстве · {pending} {plural(pending, ['изменение', 'изменения', 'изменений'])}
        {offline ? ' · без сети' : ''}
      </div>
      <main>
        {error && (
          <section className="error" role="alert">
            <strong>{error}</strong>
            <button onClick={() => void load(true)} disabled={busy}>
              Обновить данные
            </button>
          </section>
        )}
        {content}
        {!demo && (
          <SyncPanel
            owner={ownerId}
            data={data}
            repository={repository}
            coordinator={coordinator}
            notice={notice}
            changed={() => void load()}
          />
        )}
      </main>
      <footer className="app-footer">
        <p>
          {demo
            ? 'Демо хранится только на устройстве'
            : pending
              ? 'Есть записи, ещё не доставленные на сервер'
              : data
                ? 'Очередь отправки пуста'
                : 'Проверяем локальные записи'}
        </p>
        <p>{offlineReady ? 'Можно открыть без сети' : 'Офлайн-запуск будет готов после загрузки сборки'}</p>
      </footer>
      <nav className="bottom-nav" aria-label="Разделы">
        <button
          aria-current={view.kind !== 'history' ? 'page' : undefined}
          onClick={() => setView({ kind: 'home' })}
        >
          ◈ <span>Тренировка</span>
        </button>
        <button
          aria-current={view.kind === 'history' ? 'page' : undefined}
          onClick={() => setView({ kind: 'history' })}
        >
          ◷ <span>История</span>
        </button>
      </nav>
    </div>
  );
}
