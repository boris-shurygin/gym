import type { PlanSnapshot, Receipt, SyncOperation } from './sync-model';

export type SetKind = 'working' | 'warmup';
export interface Target {
  id: string;
  weightKg: number;
  reps: number;
}
export interface Exercise {
  id: string;
  name: string;
  equipment: string;
  weightStepKg: number;
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
  weightKg: number;
  reps: number;
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

export function grams(value: number): number {
  const match = /^(\d+)(?:\.(\d{1,3}))?$/.exec(String(value));
  if (!match) throw new WorkoutError('Вес: от 0 до 2000 кг, не больше трёх знаков после запятой.');
  const result = Number(match[1]) * 1000 + Number((match[2] ?? '').padEnd(3, '0'));
  if (!Number.isSafeInteger(result) || result > 2_000_000)
    throw new WorkoutError('Вес должен быть от 0 до 2000 кг.');
  return result;
}

export function validateValues(values: SetValues): void {
  grams(values.weightKg);
  if (
    !['working', 'warmup'].includes(values.kind) ||
    !Number.isFinite(values.weightKg) ||
    !Number.isFinite(values.weightKg * values.reps) ||
    values.weightKg < 0 ||
    !Number.isSafeInteger(values.reps) ||
    values.reps <= 0 ||
    values.reps > 1000
  ) {
    throw new WorkoutError('Укажи вес от 0 до 2000 кг и целое число повторений от 1 до 1000.');
  }
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
    if (!exercise.targets.length || !Number.isFinite(exercise.weightStepKg) || exercise.weightStepKg <= 0)
      throw new WorkoutError('У упражнения нет готовых плановых целей или шага веса.');
    for (const target of exercise.targets) validateValues({ ...target, kind: 'working' });
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
): { target: Target; plannedTargetId: string | null } {
  const exercise = session.snapshot.exercises.find((e) => e.id === exerciseId);
  if (!exercise?.targets.length) throw new WorkoutError('Упражнения нет в снимке выбранного шаблона.');
  const used = new Set(
    exerciseSets(session, exerciseId)
      .filter((s) => s.kind === 'working' && s.id !== excludingSetId)
      .map((s) => s.plannedTargetId),
  );
  const available = exercise.targets.find((t) => !used.has(t.id));
  return {
    target: structuredClone(available ?? exercise.targets.at(-1)!),
    plannedTargetId: available?.id ?? null,
  };
}

export function newSetDefaults(session: Session, exerciseId: string, kind: SetKind): SetValues {
  const { target } = nextTarget(session, exerciseId);
  const previous = exerciseSets(session, exerciseId)
    .filter((set) => set.kind === 'working')
    .at(-1);
  const changed =
    previous?.comparisonTarget &&
    (grams(previous.weightKg) !== grams(previous.comparisonTarget.weightKg) ||
      previous.reps !== previous.comparisonTarget.reps);
  const weightKg = changed ? previous.weightKg : target.weightKg;
  const reps = changed ? previous.reps : target.reps;
  return kind === 'warmup'
    ? {
        kind,
        weightKg: Math.round((grams(weightKg) * 7) / 10) / 1000,
        reps: Math.min(1000, Math.ceil((reps * 3) / 2)),
      }
    : { kind, weightKg, reps };
}

export function volumeRatio(result: SetResult): number | null {
  const target = result.comparisonTarget;
  if (result.kind !== 'working' || !target || target.weightKg * target.reps <= 0) return null;
  return (result.weightKg * result.reps) / (target.weightKg * target.reps);
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
    validateValues(mutation.values);
    const next = nextTarget(session, mutation.exerciseId);
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
      validateValues(mutation.values);
      if (set.kind !== mutation.values.kind) {
        const next = nextTarget(session, set.exerciseId, set.id);
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
