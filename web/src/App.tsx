import { useEffect, useRef, useState } from 'react';
import {
  createSession,
  exerciseSets,
  nextTarget,
  newSetDefaults,
  progressRatio,
  formatResult,
  formatDuration,
  parseDuration,
  exerciseLoadDescription,
  exerciseSetCounts,
  nextSide,
  WorkoutError,
  type Command,
  type Exercise,
  type Mutation,
  type Session,
  type SetKind,
  type SetResult,
  type SetValues,
  type Side,
  type Program,
} from './model';
import { WorkoutRepository } from './repository';
import type { LocalData } from './repository';
import { programFromPlans } from './sync-model';
import { SyncCoordinator, type SyncNotice } from './sync';
import SyncPanel from './SyncPanel';
import FinishExerciseButton from './ui/FinishExerciseButton';
import SetInput from './ui/SetInput';
import Statistics from './Statistics';
import type { ProgramView } from './account';
import { recordDiagnostic } from './telemetry';

type View =
  | { kind: 'home' }
  | { kind: 'history' }
  | { kind: 'statistics' }
  | { kind: 'preview'; templateId: string }
  | { kind: 'session'; sessionId: string }
  | { kind: 'exercise'; sessionId: string; exerciseId: string; editSetId?: string };
interface Draft {
  weight: string;
  reps: string;
  duration: string;
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

const sideName = (side: Side) => (side === 'left' ? 'Левая' : side === 'right' ? 'Правая' : '');
const decimalInput = (text: string) =>
  /^(?:\d+(?:\.\d{1,3})?|\.\d{1,3})$/.test(text.trim().replace(',', '.'))
    ? Number(text.trim().replace(',', '.'))
    : NaN;
const draftValues = (values: SetValues) => ({
  weight: String(values.scaleValue ?? values.weightKg ?? ''),
  reps: String(values.reps ?? ''),
  duration: values.durationSeconds == null ? '' : formatDuration(values.durationSeconds),
});
function progressText(current: Session, exercise: Exercise) {
  const count = exerciseSetCounts(current, exercise.id);
  const base =
    exercise.sides === 'each'
      ? `${count.pairs} / ${exercise.targets.length} пар · ${count.planned} / ${count.totalPlanned} сторон`
      : `${count.planned} / ${count.totalPlanned} ${exercise.setStructure === 'myo' ? 'подходов серии' : 'рабочих подходов'}`;
  return (
    base +
    (count.additional
      ? ` · +${count.additional} дополнительно${exercise.sides === 'each' ? ' (стороны)' : ''}`
      : '')
  );
}
function Volume({ set, exercise }: { set: SetResult; exercise: Exercise }) {
  const ratio = progressRatio(set, exercise);
  if (ratio === null)
    return (
      <p className="caption">
        {set.kind === 'warmup'
          ? 'Разминка · вне рабочего плана'
          : set.comparisonTarget && exercise.load?.kind === 'machine_scale'
            ? `План: ${formatResult(set.comparisonTarget, exercise)}`
            : 'Нет положительного планового объёма для сравнения'}
      </p>
    );
  const percent = ratio * 100;
  const target = set.comparisonTarget!;
  const duration = exercise.measurement === 'duration';
  const bodyweight = exercise.load?.kind === 'bodyweight';
  const factor = exercise.load?.kind === 'external' ? exercise.load.implementCount : 1;
  const metric = (value: typeof target | SetResult) =>
    duration ? value.durationSeconds! : bodyweight ? value.reps! : value.weightKg! * value.reps! * factor;
  const display = duration ? formatDuration : format;
  const unit = duration ? 'мм:сс' : bodyweight ? 'повт.' : 'кг·повт.';
  return (
    <div className="volume">
      <div
        className="volume-track"
        role="progressbar"
        aria-label={
          duration
            ? 'Время относительно плана'
            : bodyweight
              ? 'Повторения относительно плана'
              : 'Объём относительно плана'
        }
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
          {display(metric(set))} / {display(metric(target))} {unit}
        </span>
        <strong>
          {percentage.format(percent)}%{percent > 130 ? ' ↑' : ''}
        </strong>
      </div>
      <p className="caption">План: {formatResult(target, exercise)} · отметка 100%</p>
    </div>
  );
}

export default function App({
  ownerId,
  program: suppliedProgram,
  demo = false,
  programView,
}: {
  ownerId: string;
  program: Program;
  demo?: boolean;
  programView?: ProgramView;
}) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [pending, setPending] = useState(0);
  const [view, setView] = useState<View>({ kind: 'home' });
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [selectedSides, setSelectedSides] = useState<Record<string, Side>>({});
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
        setSelectedSides({});
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
      if (mutation.type === 'addSet' && mutation.values.kind === 'working' && mutation.values.side) {
        setSelectedSides((previous) => ({
          ...previous,
          [`${sessionId}:${mutation.exerciseId}`]: mutation.values.side === 'left' ? 'right' : 'left',
        }));
      }
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
      if (!(error instanceof WorkoutError)) recordDiagnostic('local_save_failed', 'storage');
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
    const counts = current.snapshot.exercises.map((exercise) => exerciseSetCounts(current, exercise.id));
    const plan = counts.reduce((sum, item) => sum + item.totalPlanned, 0);
    const planned = counts.reduce((sum, item) => sum + item.planned, 0);
    const hasSides = current.snapshot.exercises.some((exercise) => exercise.sides === 'each');
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
            <span>{hasSides ? 'Рабочие записи · стороны отдельно' : 'Рабочие подходы'}</span>
            <strong>
              {planned} / {plan}
            </strong>
          </div>
          <div className="count-track">
            <span style={{ width: `${plan ? Math.min(100, (planned / plan) * 100) : 0}%` }} />
          </div>
          <p className="caption">План и фактические результаты сохраняются отдельно</p>
        </section>
        <div className="section-title">
          Упражнения <span>{current.snapshot.exercises.length}</span>
        </div>
        <div className="exercise-list">
          {current.snapshot.exercises.map((exercise, i) => {
            const done = exerciseSetCounts(current, exercise.id);
            return (
              <button
                key={exercise.id}
                className="exercise-row"
                onClick={() => setView({ kind: 'exercise', sessionId: current.id, exerciseId: exercise.id })}
              >
                <span className={`exercise-number ${done.planned >= done.totalPlanned ? 'done' : ''}`}>
                  {done.planned >= done.totalPlanned ? '✓' : i + 1}
                </span>
                <span className="grow">
                  <strong>{exercise.name}</strong>
                  <span className="caption block">{exercise.equipment}</span>
                  <span className="caption block">{progressText(current, exercise)}</span>
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
                  `Завершить тренировку? По плану записано ${planned} из ${plan}${hasSides ? ' рабочих записей (стороны отдельно)' : ' рабочих подходов'}.${count > planned ? ` Дополнительно: ${count - planned}.` : ''} Невыполненные подходы не будут записаны как нули. Результаты можно исправить в истории.`,
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
    const sideKey = `${current.id}:${exercise.id}`;
    const side = editing?.side ?? selectedSides[sideKey] ?? nextSide(current, exercise.id);
    const next = nextTarget(current, exercise.id, undefined, side);
    const defaults = newSetDefaults(current, exercise.id, 'working', side);
    const key = `${sideKey}:${editSetId ?? `new:${side ?? 'single'}`}`;
    const draft = drafts[key] ?? {
      ...draftValues(editing ?? defaults),
      kind: editing?.kind ?? 'working',
      revision: current.revision,
    };
    const update = (value: Partial<Draft>) =>
      setDrafts((previous) => ({ ...previous, [key]: { ...draft, ...value } }));
    const goBack: View = { kind: 'session', sessionId: current.id };
    const newSetView: View = { kind: 'exercise', sessionId: current.id, exerciseId: exercise.id };
    const workingCount = sets.filter((s) => s.kind === 'working').length;
    const workingSideCount = sets.filter((s) => s.kind === 'working' && (s.side ?? null) === side).length;
    const position = next.plannedTargetId
      ? exercise.targets.findIndex((target) => target.id === next.plannedTargetId) + 1
      : workingSideCount + 1;
    const duration = exercise.measurement === 'duration';
    const scale = exercise.load?.kind === 'machine_scale';
    const hasLoad = exercise.load?.kind !== 'bodyweight';
    const loadStep =
      exercise.load?.kind === 'machine_scale' ? exercise.load.step : (exercise.weightStepKg ?? 1);
    const weightNumber = decimalInput(draft.weight);
    const repsNumber = /^\d+$/.test(draft.reps.trim()) ? Number(draft.reps) : NaN;
    const durationNumber = parseDuration(draft.duration) ?? NaN;
    const loadLabel = scale
      ? 'Показание шкалы'
      : exercise.load?.kind === 'external' && exercise.load.weightConvention === 'per_implement_kg'
        ? 'На один снаряд, кг'
        : 'Вес, кг';
    const sameTargetSides = sets.filter(
      (set) => set.kind === 'working' && next.plannedTargetId && set.plannedTargetId === next.plannedTargetId,
    ).length;
    return (
      <>
        <button className="back" onClick={() => setView(goBack)}>
          ‹ К упражнениям
        </button>
        <p className="eyebrow">{current.snapshot.name}</p>
        <h1>{exercise.name}</h1>
        <p className="subheading">{exercise.equipment}</p>
        {exercise.measurement && exercise.equipment !== exerciseLoadDescription(exercise) && (
          <p className="caption">{exerciseLoadDescription(exercise)}</p>
        )}
        <section className="card plan">
          <span className="eyebrow">План упражнения</span>
          <strong>
            {exercise.targets.map((target) => formatResult(target, exercise)).join(' / ')}
            {exercise.sides === 'each' ? ' на каждую сторону' : ''}
          </strong>
          <p className="caption">
            {exercise.measurement
              ? progressText(current, exercise)
              : `Вес в кг · ${workingCount} / ${exercise.targets.length} рабочих подходов`}
          </p>
          {exercise.setStructure === 'myo' && (
            <p className="caption">
              Активация + {exercise.targets.length - 1} коротких подхода
              {exercise.restSeconds
                ? ` · отдых ${exercise.restSeconds.min}–${exercise.restSeconds.max} с`
                : ''}
            </p>
          )}
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
            const values: SetValues = {
              kind: draft.kind,
              weightKg: hasLoad && !scale ? weightNumber : null,
              reps: duration ? null : repsNumber,
              ...(exercise.measurement
                ? {
                    side,
                    scaleValue: scale ? weightNumber : null,
                    durationSeconds: duration ? durationNumber : null,
                  }
                : {}),
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
                : `Рабочий подход ${exercise.measurement ? position : workingCount + 1}${exercise.setStructure === 'myo' && position === 1 ? ' · активация' : ''}`}
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
                  const values = newSetDefaults(current, exercise.id, kind, side);
                  update({
                    kind,
                    ...(!editSetId ? draftValues(values) : {}),
                  });
                }}
              >
                {kind === 'working' ? 'Рабочий' : 'Разминка'}
              </button>
            ))}
          </div>
          {exercise.sides === 'each' && (
            <div className="side-switch" aria-label="Сторона">
              {(['left', 'right'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  disabled={busy || Boolean(editSetId)}
                  aria-pressed={side === value}
                  onClick={() => setSelectedSides((previous) => ({ ...previous, [sideKey]: value }))}
                >
                  {sideName(value)}
                </button>
              ))}
            </div>
          )}
          {!editing && exercise.sides === 'each' && draft.kind === 'working' && next.plannedTargetId && (
            <p className="caption">
              {sameTargetSides ? 'Записана 1 из 2 сторон' : 'Обе стороны ещё не записаны'}
            </p>
          )}
          {!editing && exercise.measurement && draft.kind === 'working' && (
            <p className="caption">
              Цель: {formatResult(next.target, exercise)}
              {exercise.sides === 'each' ? ' на эту сторону' : ''}
            </p>
          )}
          {!editSetId && draft.kind === 'warmup' && (
            <p className="caption">
              {duration
                ? 'Укажи время разминки вручную.'
                : scale
                  ? 'Показание шкалы укажи вручную. Повторения × 1,5, округлённые вверх (до 1000).'
                  : !hasLoad
                    ? 'Повторения × 1,5, округлённые вверх (до 1000). Значение можно изменить.'
                    : 'По умолчанию: 70% рабочего веса и повторения × 1,5, округлённые вверх (до 1000). Значения можно изменить.'}
            </p>
          )}
          {!editSetId &&
            draft.kind === 'working' &&
            (defaults.weightKg !== next.target.weightKg ||
              defaults.reps !== next.target.reps ||
              defaults.durationSeconds !== next.target.durationSeconds ||
              defaults.scaleValue !== next.target.scaleValue) && (
              <p className="caption">
                Ввод перенесён из предыдущего рабочего подхода. Сравнение с планом остаётся прежним.
              </p>
            )}
          <div className={`input-row ${hasLoad ? '' : 'single'}`}>
            {hasLoad && (
              <>
                <SetInput
                  label={loadLabel}
                  value={draft.weight}
                  onChange={(weight) => update({ weight })}
                  parsed={weightNumber}
                  step={loadStep}
                  unit={`${format(loadStep)}${scale ? ' дел.' : ' кг'}`}
                  min={0}
                  max={2000}
                  disabled={busy}
                  inputMode="decimal"
                  decrease={scale ? 'Уменьшить показание' : 'Уменьшить вес'}
                  increase={scale ? 'Увеличить показание' : 'Увеличить вес'}
                />
                <span className="multiply">{duration ? '·' : '×'}</span>
              </>
            )}
            {duration ? (
              <SetInput
                label="Время, мм:сс"
                value={draft.duration}
                onChange={(duration) => update({ duration })}
                parsed={durationNumber}
                step={5}
                unit="5 с"
                min={1}
                max={86400}
                disabled={busy}
                inputMode="text"
                print={formatDuration}
                decrease="Уменьшить время"
                increase="Увеличить время"
              />
            ) : (
              <SetInput
                label="Повторения"
                value={draft.reps}
                onChange={(reps) => update({ reps })}
                parsed={repsNumber}
                step={1}
                unit="1 повт."
                min={1}
                max={1000}
                disabled={busy}
                decrease="Уменьшить повторения"
                increase="Увеличить повторения"
              />
            )}
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
                      `Удалить подход ${formatResult(editing, exercise)}${editing.side ? ` · ${sideName(editing.side)}` : ''}? Остальные результаты останутся.`,
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
                  {set.side && <span className="record-context">{sideName(set.side)}</span>}
                  {exercise.setStructure === 'myo' && set.kind === 'working' && (
                    <span className="record-context">
                      {set.plannedTargetId === exercise.targets[0].id ? 'Активация' : 'После активации'}
                    </span>
                  )}
                </span>
                <strong>
                  {formatResult(set, exercise)} <span className="chevron">›</span>
                </strong>
              </div>
              <Volume set={set} exercise={exercise} />
            </button>
          </article>
        ))}
        <FinishExerciseButton disabled={busy} onFinish={() => setView(goBack)} />
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
                {exercise.targets.map((target) => formatResult(target, exercise)).join(' / ')}
                {exercise.sides === 'each' ? ' на каждую сторону' : ''}
                {exercise.setStructure === 'myo' ? ' · активация и короткие подходы' : ''}
              </span>
              <span className="caption block">{exerciseLoadDescription(exercise)}</span>
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
  } else if (view.kind === 'statistics') {
    content = (
      <>
        <button className="back" onClick={() => setView({ kind: 'history' })}>
          ← К истории
        </button>
        <Statistics owner={ownerId} pending={pending} program={programView} />
      </>
    );
  } else if (view.kind === 'history') {
    const history = sessions.filter((s) => s.status === 'completed');
    content = (
      <>
        <p className="eyebrow">Твои записи на этом устройстве</p>
        <h1>История тренировок</h1>
        {!demo && (
          <button className="secondary wide" onClick={() => setView({ kind: 'statistics' })}>
            Серверная история и графики
          </button>
        )}
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
              {current.snapshot.exercises.some((exercise) => exercise.sides === 'each')
                ? 'рабочих записей · стороны отдельно'
                : plural(current.sets.filter((s) => !s.deletedAt && s.kind === 'working').length, [
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
                {template.exercises.reduce((n, e) => n + e.targets.length * (e.sides === 'each' ? 2 : 1), 0)}{' '}
                {template.exercises.some((exercise) => exercise.sides === 'each')
                  ? 'записей · стороны отдельно'
                  : 'подходов'}
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
          aria-current={!['history', 'statistics'].includes(view.kind) ? 'page' : undefined}
          onClick={() => setView({ kind: 'home' })}
        >
          ◈ <span>Тренировка</span>
        </button>
        <button
          aria-current={['history', 'statistics'].includes(view.kind) ? 'page' : undefined}
          onClick={() => setView({ kind: 'history' })}
        >
          ◷ <span>История</span>
        </button>
      </nav>
    </div>
  );
}
