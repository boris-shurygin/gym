import type { PlanSnapshot, Receipt, SyncOperation } from './sync-model';

export type SetKind = 'working' | 'warmup';
export type Side = 'left' | 'right' | null;
export type ExerciseLoad =
  | { kind: 'bodyweight' }
  | {
      kind: 'external';
      weightConvention: 'total_external_kg' | 'machine_marking_kg' | 'per_implement_kg';
      implementCount: 1 | 2;
      stepKg: number;
    }
  | { kind: 'machine_scale'; step: number };
export interface Target {
  id: string;
  weightKg: number | null;
  reps: number | null;
  scaleValue?: number | null;
  durationSeconds?: number | null;
}
export interface Exercise {
  id: string;
  name: string;
  equipment: string;
  weightStepKg: number | null;
  measurement?: 'reps' | 'duration';
  load?: ExerciseLoad;
  sides?: 'single' | 'each';
  setStructure?: 'straight' | 'myo';
  restSeconds?: { min: number; max: number } | null;
  targets: Target[];
}
export interface Template {
  planSnapshot?: PlanSnapshot;
  id: string;
  name: string;
  description: string;
  exercises: Exercise[];
}
export interface Program {
  versionId: string;
  templates: Template[];
}
export interface SetValues {
  kind: SetKind;
  side?: Side;
  weightKg: number | null;
  reps: number | null;
  scaleValue?: number | null;
  durationSeconds?: number | null;
}
export interface SetResult extends SetValues {
  id: string;
  exerciseId: string;
  plannedTargetId: string | null;
  comparisonTarget: Target | null;
  deletedAt?: string;
}
export interface Session {
  localOnly?: boolean;
  serverRevision?: number;
  id: string;
  ownerId: string;
  programVersionId: string;
  snapshot: Template;
  revision: number;
  status: 'active' | 'completed' | 'cancelled';
  startedAt: string;
  completedAt?: string;
  sets: SetResult[];
}
export type Mutation =
  | { type: 'start'; session: Session }
  | { type: 'addSet'; exerciseId: string; setId: string; values: SetValues }
  | { type: 'editSet'; setId: string; values: SetValues }
  | { type: 'deleteSet'; setId: string }
  | { type: 'complete' }
  | { type: 'cancelEmpty' };
export interface Command {
  id: string;
  ownerId: string;
  sessionId: string;
  expectedRevision: number;
  createdAt: string;
  mutation: Mutation;
}
export interface PendingOperation extends Command {
  fingerprint: string;
  status: 'pending' | 'sending' | 'acked' | 'blocked_auth' | 'conflict' | 'rejected' | 'superseded';
  wire?: SyncOperation;
  sequence?: number;
  attempts?: number;
  nextAttemptAt?: number;
  receipt?: Receipt;
  reason?: string;
}

export class WorkoutError extends Error {}
export class ConflictError extends WorkoutError {
  constructor() {
    super('Тренировка изменилась в другой вкладке. Обнови данные перед новой записью.');
  }
}

export function grams(value: number | null): number {
  const match = /^(\d+)(?:\.(\d{1,3}))?$/.exec(String(value));
  if (!match) throw new WorkoutError('Вес: от 0 до 2000 кг, не больше трёх знаков после запятой.');
  const result = Number(match[1]) * 1000 + Number((match[2] ?? '').padEnd(3, '0'));
  if (!Number.isSafeInteger(result) || result > 2_000_000)
    throw new WorkoutError('Вес должен быть от 0 до 2000 кг.');
  return result;
}

export function scaleMilli(value: number | null): number {
  if (value === null) throw new WorkoutError('Укажи показание шкалы.');
  try {
    return grams(value);
  } catch {
    throw new WorkoutError('Шкала: от 0 до 2000, не больше трёх знаков после запятой.');
  }
}

export function validateValues(values: SetValues, exercise?: Exercise): void {
  const fail = () => {
    throw new WorkoutError('Результат не соответствует измерению и нагрузке упражнения.');
  };
  if (!['working', 'warmup'].includes(values.kind)) fail();
  if (!exercise?.measurement) {
    grams(values.weightKg);
    if (
      values.weightKg === null ||
      values.reps === null ||
      !Number.isSafeInteger(values.reps) ||
      values.reps < 1 ||
      values.reps > 1000 ||
      values.side != null ||
      values.scaleValue != null ||
      values.durationSeconds != null
    )
      fail();
    return;
  }
  if (exercise.sides === 'each' ? !['left', 'right'].includes(values.side ?? '') : values.side != null)
    fail();
  if (exercise.measurement === 'duration') {
    if (
      values.reps !== null ||
      !Number.isSafeInteger(values.durationSeconds) ||
      values.durationSeconds! < 1 ||
      values.durationSeconds! > 86400
    )
      fail();
  } else if (
    values.durationSeconds != null ||
    values.reps === null ||
    !Number.isSafeInteger(values.reps) ||
    values.reps < 1 ||
    values.reps > 1000
  )
    fail();
  if (exercise.load?.kind === 'external') {
    if (values.weightKg === null || values.scaleValue != null) fail();
    grams(values.weightKg);
  } else if (exercise.load?.kind === 'machine_scale') {
    if (values.weightKg !== null || values.scaleValue == null) fail();
    scaleMilli(values.scaleValue ?? null);
  } else if (exercise.load?.kind !== 'bodyweight' || values.weightKg !== null || values.scaleValue != null)
    fail();
}

export function createSession(
  program: Program,
  templateId: string,
  ownerId: string,
  id: string,
  now: string,
): Session {
  const template = program.templates.find((t) => t.id === templateId);
  if (!template || !ownerId || !id) throw new WorkoutError('Шаблон недоступен.');
  for (const exercise of template.exercises) {
    if (
      !exercise.targets.length ||
      (!exercise.measurement &&
        (exercise.weightStepKg === null ||
          !Number.isFinite(exercise.weightStepKg) ||
          exercise.weightStepKg <= 0))
    )
      throw new WorkoutError('У упражнения нет готовых плановых целей или шага веса.');
    if (exercise.measurement) {
      if (
        !['reps', 'duration'].includes(exercise.measurement) ||
        !['single', 'each'].includes(exercise.sides ?? '') ||
        !['straight', 'myo'].includes(exercise.setStructure ?? '')
      )
        throw new WorkoutError('Неизвестная модель упражнения.');
      if (
        exercise.load?.kind === 'external' &&
        (exercise.load.stepKg <= 0 || grams(exercise.load.stepKg) <= 0)
      )
        throw new WorkoutError('Некорректный шаг веса.');
      if (
        exercise.load?.kind === 'machine_scale' &&
        (exercise.load.step <= 0 || scaleMilli(exercise.load.step) <= 0)
      )
        throw new WorkoutError('Некорректный шаг шкалы.');
    }
    for (const target of exercise.targets)
      validateValues(
        {
          ...target,
          kind: 'working',
          ...(exercise.measurement ? { side: exercise.sides === 'each' ? 'left' : null } : {}),
        },
        exercise,
      );
  }
  return {
    id,
    ownerId,
    programVersionId: program.versionId,
    snapshot: structuredClone(template),
    revision: 0,
    status: 'active',
    startedAt: now,
    sets: [],
  };
}

export function exerciseSets(session: Session, exerciseId: string): SetResult[] {
  return session.sets.filter((s) => s.exerciseId === exerciseId && !s.deletedAt);
}

export function nextTarget(
  session: Session,
  exerciseId: string,
  excludingSetId?: string,
  side?: Side,
): { target: Target; plannedTargetId: string | null } {
  const exercise = session.snapshot.exercises.find((e) => e.id === exerciseId);
  if (!exercise?.targets.length) throw new WorkoutError('Упражнения нет в снимке выбранного шаблона.');
  const selectedSide = exercise.sides === 'each' ? (side ?? 'left') : null;
  const used = new Set(
    exerciseSets(session, exerciseId)
      .filter((s) => s.kind === 'working' && s.id !== excludingSetId && (s.side ?? null) === selectedSide)
      .map((s) => s.plannedTargetId),
  );
  const available = exercise.targets.find((t) => !used.has(t.id));
  return {
    target: structuredClone(available ?? exercise.targets.at(-1)!),
    plannedTargetId: available?.id ?? null,
  };
}

export function newSetDefaults(session: Session, exerciseId: string, kind: SetKind, side?: Side): SetValues {
  const exercise = session.snapshot.exercises.find((e) => e.id === exerciseId);
  if (!exercise) throw new WorkoutError('Упражнения нет в снимке.');
  const selectedSide = exercise.sides === 'each' ? (side ?? 'left') : null;
  const { target } = nextTarget(session, exerciseId, undefined, selectedSide);
  const previous = exerciseSets(session, exerciseId)
    .filter((set) => set.kind === 'working' && (set.side ?? null) === selectedSide)
    .at(-1);
  const changed =
    exercise.setStructure !== 'myo' &&
    previous?.comparisonTarget &&
    (previous.weightKg !== previous.comparisonTarget.weightKg ||
      previous.reps !== previous.comparisonTarget.reps ||
      (previous.scaleValue ?? null) !== (previous.comparisonTarget.scaleValue ?? null) ||
      (previous.durationSeconds ?? null) !== (previous.comparisonTarget.durationSeconds ?? null));
  const source = changed ? previous : target;
  const weightKg = source.weightKg,
    reps = source.reps;
  if (!exercise.measurement)
    return kind === 'warmup'
      ? {
          kind,
          weightKg: Math.round((grams(weightKg) * 7) / 10) / 1000,
          reps: Math.min(1000, Math.ceil((reps! * 3) / 2)),
        }
      : { kind, weightKg, reps };
  return {
    kind,
    side: selectedSide,
    weightKg:
      exercise.load?.kind === 'external'
        ? kind === 'warmup'
          ? Math.round((grams(weightKg) * 7) / 10) / 1000
          : weightKg
        : null,
    reps:
      exercise.measurement === 'reps'
        ? kind === 'warmup'
          ? Math.min(1000, Math.ceil((reps! * 3) / 2))
          : reps
        : null,
    scaleValue:
      exercise.load?.kind === 'machine_scale' && kind === 'working' ? (source.scaleValue ?? null) : null,
    durationSeconds:
      exercise.measurement === 'duration' && kind === 'working' ? (source.durationSeconds ?? null) : null,
  };
}

export function volumeRatio(result: SetResult): number | null {
  const target = result.comparisonTarget;
  if (
    result.kind !== 'working' ||
    !target ||
    target.weightKg === null ||
    target.reps === null ||
    result.weightKg === null ||
    result.reps === null ||
    target.weightKg * target.reps <= 0
  )
    return null;
  return (result.weightKg * result.reps) / (target.weightKg * target.reps);
}

export function progressRatio(result: SetResult, exercise?: Exercise): number | null {
  if (!exercise?.measurement || (exercise.measurement === 'reps' && exercise.load?.kind === 'external'))
    return volumeRatio(result);
  const target = result.comparisonTarget;
  if (result.kind !== 'working' || !target || exercise.load?.kind === 'machine_scale') return null;
  const actual = exercise.measurement === 'duration' ? result.durationSeconds : result.reps;
  const planned = exercise.measurement === 'duration' ? target.durationSeconds : target.reps;
  return actual != null && planned != null && planned > 0 ? actual / planned : null;
}

export function formatDuration(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
export function parseDuration(text: string): number | null {
  const match = /^(\d{1,4}):([0-5]\d)$/.exec(text.trim());
  if (!match) return null;
  const value = Number(match[1]) * 60 + Number(match[2]);
  return value > 0 && value <= 86400 ? value : null;
}
const formatNumber = (n: number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(n);
export function formatResult(
  value: Pick<SetValues, 'weightKg' | 'reps' | 'scaleValue' | 'durationSeconds'>,
  exercise?: Exercise,
): string {
  if (value.durationSeconds != null)
    return (
      (value.weightKg !== null
        ? `${formatNumber(value.weightKg)} кг · `
        : value.scaleValue != null
          ? `Шкала ${formatNumber(value.scaleValue)} · `
          : '') + formatDuration(value.durationSeconds)
    );
  if (value.scaleValue != null) return `Шкала ${formatNumber(value.scaleValue)} × ${value.reps ?? '—'}`;
  if (value.weightKg === null) return `${value.reps ?? '—'} повт.`;
  return `${formatNumber(value.weightKg)} кг${exercise?.load?.kind === 'external' && exercise.load.weightConvention === 'per_implement_kg' ? ' на снаряд' : ''} × ${value.reps ?? '—'}`;
}
export function exerciseLoadDescription(exercise: Exercise): string {
  const load = exercise.load;
  if (!load) return exercise.equipment;
  if (load.kind === 'bodyweight') return 'Свой вес';
  if (load.kind === 'machine_scale') return 'Показание шкалы · единица не подтверждена';
  if (load.weightConvention === 'per_implement_kg')
    return `На один снаряд · одновременно ${load.implementCount}`;
  return load.weightConvention === 'machine_marking_kg'
    ? 'Вес по маркировке тренажёра, кг'
    : 'Общий внешний вес, кг';
}
export function exerciseSetCounts(
  session: Session,
  exerciseId: string,
): { planned: number; totalPlanned: number; additional: number; pairs?: number } {
  const exercise = session.snapshot.exercises.find((e) => e.id === exerciseId);
  if (!exercise) throw new WorkoutError('Упражнения нет в снимке.');
  const sets = exerciseSets(session, exerciseId).filter((set) => set.kind === 'working');
  const occupied = new Set(
    sets.filter((set) => set.plannedTargetId).map((set) => `${set.plannedTargetId}|${set.side ?? ''}`),
  );
  return {
    planned: occupied.size,
    totalPlanned: exercise.targets.length * (exercise.sides === 'each' ? 2 : 1),
    additional: sets.filter((set) => !set.plannedTargetId).length,
    ...(exercise.sides === 'each'
      ? {
          pairs: exercise.targets.filter((target) =>
            ['left', 'right'].every((side) => occupied.has(`${target.id}|${side}`)),
          ).length,
        }
      : {}),
  };
}
export function nextSide(session: Session, exerciseId: string): Side {
  const exercise = session.snapshot.exercises.find((e) => e.id === exerciseId);
  if (exercise?.sides !== 'each') return null;
  const last = exerciseSets(session, exerciseId).at(-1);
  if (!last?.side) return 'left';
  return last.kind === 'warmup' ? last.side : last.side === 'left' ? 'right' : 'left';
}

export function applyMutation(
  current: Session,
  mutation: Exclude<Mutation, { type: 'start' }>,
  now: string,
): Session {
  const session = structuredClone(current);
  if (session.status === 'cancelled') throw new WorkoutError('Пустая тренировка уже отменена.');
  if (mutation.type === 'addSet') {
    if (session.sets.some((s) => s.id === mutation.setId))
      throw new WorkoutError('Этот подход уже существует.');
    const exercise = session.snapshot.exercises.find((e) => e.id === mutation.exerciseId);
    if (!exercise) throw new WorkoutError('Упражнения нет в снимке.');
    validateValues(mutation.values, exercise);
    const next = nextTarget(session, mutation.exerciseId, undefined, mutation.values.side);
    session.sets.push({
      id: mutation.setId,
      exerciseId: mutation.exerciseId,
      ...mutation.values,
      plannedTargetId: mutation.values.kind === 'working' ? next.plannedTargetId : null,
      comparisonTarget: mutation.values.kind === 'working' ? next.target : null,
    });
  } else if (mutation.type === 'editSet' || mutation.type === 'deleteSet') {
    const set = session.sets.find((s) => s.id === mutation.setId && !s.deletedAt);
    if (!set) throw new WorkoutError('Подход уже удалён или не найден.');
    if (mutation.type === 'deleteSet') set.deletedAt = now;
    else {
      const exercise = session.snapshot.exercises.find((e) => e.id === set.exerciseId);
      validateValues(mutation.values, exercise);
      if ((set.side ?? null) !== (mutation.values.side ?? null))
        throw new WorkoutError('Исправление не меняет сторону подхода.');
      if (set.kind !== mutation.values.kind) {
        const next = nextTarget(session, set.exerciseId, set.id, set.side);
        set.plannedTargetId = mutation.values.kind === 'working' ? next.plannedTargetId : null;
        set.comparisonTarget = mutation.values.kind === 'working' ? next.target : null;
      }
      Object.assign(set, mutation.values);
    }
  } else if (mutation.type === 'complete') {
    if (session.status !== 'active') throw new WorkoutError('Тренировка уже завершена.');
    if (!session.sets.some((s) => !s.deletedAt))
      throw new WorkoutError('Сначала запиши хотя бы один подход.');
    session.status = 'completed';
    session.completedAt = now;
  } else {
    if (session.status !== 'active' || session.sets.some((s) => !s.deletedAt)) {
      throw new WorkoutError('Отменить можно только пустую активную тренировку.');
    }
    session.status = 'cancelled';
  }
  session.revision += 1;
  return session;
}
