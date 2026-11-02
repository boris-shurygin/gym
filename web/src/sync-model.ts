import type { Content, ContentV2, ProgramView } from './account';
import { exerciseFromContent } from './account';
import {
  applyMutation,
  grams,
  scaleMilli,
  validateValues,
  WorkoutError,
  type Mutation,
  type Session,
  type Program,
  type Command,
} from './model';

interface PlanSnapshotBase {
  plan_snapshot_id: string;
  owner_id: string;
  program_version_id: string;
  calculated_at: string;
  history_revision: number;
  calculator_version: string;
}
export type PlanSnapshot = PlanSnapshotBase &
  (
    | { snapshot_schema_version: 1; template: Content['templates'][number] }
    | { snapshot_schema_version: 2; template: ContentV2['templates'][number] }
  );
export interface WorkoutSetV1 {
  set_id: string;
  session_exercise_id: string;
  kind: 'working' | 'warmup';
  weight_g: number;
  reps: number;
  planned_target_id: string | null;
  comparison_target_id: string | null;
  deleted_at: string | null;
}
export interface WorkoutSetV2 extends Omit<WorkoutSetV1, 'weight_g' | 'reps'> {
  weight_g: number | null;
  reps: number | null;
  scale_milli: number | null;
  duration_seconds: number | null;
  side: 'left' | 'right' | null;
}
export interface Workout {
  workout_id: string;
  owner_id: string;
  server_revision: number;
  status: Session['status'];
  plan: PlanSnapshot;
  started_at: string;
  completed_at: string | null;
  updated_at: string;
  sets: (WorkoutSetV1 | WorkoutSetV2)[];
}

type WireCommandV1 =
  | { type: 'start_workout'; plan_snapshot_id: string }
  | {
      type: 'add_set';
      session_exercise_id: string;
      set_id: string;
      kind: 'working' | 'warmup';
      weight_g: number;
      reps: number;
      planned_target_id: string | null;
      comparison_target_id: string | null;
    }
  | {
      type: 'edit_set';
      set_id: string;
      kind: 'working' | 'warmup';
      weight_g: number;
      reps: number;
      planned_target_id: string | null;
      comparison_target_id: string | null;
    }
  | { type: 'delete_set'; set_id: string }
  | { type: 'complete_workout' }
  | { type: 'cancel_empty_workout' };
type V2SetValues = Omit<WorkoutSetV2, 'session_exercise_id' | 'deleted_at'>;
type WireCommandV2 =
  | Exclude<WireCommandV1, { type: 'add_set' | 'edit_set' }>
  | ({ type: 'add_set'; session_exercise_id: string } & V2SetValues)
  | ({ type: 'edit_set' } & V2SetValues);
interface SyncOperationBase {
  operation_id: string;
  expected_owner_id: string;
  device_id: string;
  workout_id: string;
  precondition: { server_revision: number } | { after_operation_id: string };
  client_occurred_at: string;
}
export type SyncOperation = SyncOperationBase &
  ({ schema_version: 1; command: WireCommandV1 } | { schema_version: 2; command: WireCommandV2 });

export type Receipt =
  | {
      operation_id: string;
      workout_id: string;
      outcome: 'applied';
      server_revision: number;
      change_seq: number;
      accepted_at: string;
    }
  | {
      operation_id: string;
      workout_id: string;
      outcome: 'rejected';
      code: string;
      observed_server_revision: number | null;
      rejected_at: string;
    };
export interface BootstrapPage {
  high_watermark: number;
  bootstrap_id: string;
  checkpoint_cursor: string;
  next_page_cursor: string | null;
  items: (
    | { kind: 'program'; data: ProgramView }
    | { kind: 'plan'; data: PlanSnapshot }
    | { kind: 'workout'; data: Workout }
  )[];
}
export interface ChangePage {
  high_watermark: number;
  next_cursor: string;
  has_more: boolean;
  items: {
    seq: number;
    kind: string;
    workout?: Workout;
    plans?: PlanSnapshot[];
    program_version?: ProgramView['version'];
  }[];
}

export function programFromPlans(owner: string, plans: PlanSnapshot[]): Program {
  if (plans.some((p) => p.owner_id !== owner || ![1, 2].includes(p.snapshot_schema_version)))
    throw new WorkoutError('План другого аккаунта или неподдерживаемой версии.');
  return {
    versionId: plans[0]?.program_version_id ?? 'unpublished',
    templates: plans.map((plan) => ({
      id: plan.template.template_id,
      name: plan.template.name,
      description: plan.template.description,
      planSnapshot: structuredClone(plan),
      exercises: plan.template.exercises.map(exerciseFromContent),
    })),
  };
}

export function sessionFromWorkout(workout: Workout, localRevision = 0): Session {
  if (workout.owner_id !== workout.plan.owner_id) throw new WorkoutError('Владелец тренировки не совпал.');
  const template = programFromPlans(workout.owner_id, [workout.plan]).templates[0];
  return {
    id: workout.workout_id,
    ownerId: workout.owner_id,
    programVersionId: workout.plan.program_version_id,
    snapshot: template,
    serverRevision: workout.server_revision,
    revision: localRevision + 1,
    status: workout.status,
    startedAt: workout.started_at,
    ...(workout.completed_at ? { completedAt: workout.completed_at } : {}),
    sets: workout.sets.map((s) => {
      const isV2 = 'scale_milli' in s;
      if (isV2 !== (workout.plan.snapshot_schema_version === 2))
        throw new WorkoutError('Версия результата не совпала со снимком.');
      const exercise = template.exercises.find((e) => e.id === s.session_exercise_id);
      if (!exercise) throw new WorkoutError('Результат относится к неизвестному упражнению.');
      const values = {
        kind: s.kind,
        weightKg: s.weight_g === null ? null : s.weight_g / 1000,
        reps: s.reps,
        ...(isV2
          ? {
              side: s.side,
              scaleValue: s.scale_milli === null ? null : s.scale_milli / 1000,
              durationSeconds: s.duration_seconds,
            }
          : {}),
      };
      validateValues(values, exercise);
      return {
        id: s.set_id,
        exerciseId: s.session_exercise_id,
        ...values,
        plannedTargetId: s.planned_target_id,
        comparisonTarget: exercise.targets.find((t) => t.id === s.comparison_target_id) ?? null,
        ...(s.deleted_at ? { deletedAt: s.deleted_at } : {}),
      };
    }),
  };
}

export function operationFor(
  command: Command,
  saved: Session,
  deviceId: string,
  precondition: SyncOperation['precondition'],
): SyncOperation | undefined {
  const mutation = command.mutation;
  const plan = saved.snapshot.planSnapshot;
  if (!plan || saved.localOnly) return; // Preserve pre-P08/demo or explicitly archived local-only records.
  if (plan.owner_id !== command.ownerId) throw new WorkoutError('План принадлежит другому аккаунту.');
  let wire: WireCommandV1 | WireCommandV2;
  if (mutation.type === 'start') wire = { type: 'start_workout', plan_snapshot_id: plan.plan_snapshot_id };
  else if (mutation.type === 'addSet' || mutation.type === 'editSet') {
    const set = saved.sets.find((s) => s.id === mutation.setId)!;
    const values =
      plan.snapshot_schema_version === 1
        ? {
            set_id: set.id,
            kind: set.kind,
            weight_g: grams(set.weightKg),
            reps: set.reps!,
            planned_target_id: set.plannedTargetId,
            comparison_target_id: set.comparisonTarget?.id ?? null,
          }
        : {
            set_id: set.id,
            kind: set.kind,
            weight_g: set.weightKg === null ? null : grams(set.weightKg),
            reps: set.reps,
            planned_target_id: set.plannedTargetId,
            comparison_target_id: set.comparisonTarget?.id ?? null,
            scale_milli: set.scaleValue == null ? null : scaleMilli(set.scaleValue),
            duration_seconds: set.durationSeconds ?? null,
            side: set.side ?? null,
          };
    wire =
      mutation.type === 'addSet'
        ? { type: 'add_set', session_exercise_id: set.exerciseId, ...values }
        : { type: 'edit_set', ...values };
  } else if (mutation.type === 'deleteSet') wire = { type: 'delete_set', set_id: mutation.setId };
  else wire = { type: mutation.type === 'complete' ? 'complete_workout' : 'cancel_empty_workout' };
  return {
    schema_version: plan.snapshot_schema_version,
    operation_id: command.id,
    expected_owner_id: command.ownerId,
    device_id: deviceId,
    workout_id: command.sessionId,
    precondition,
    client_occurred_at: mutation.type === 'start' ? saved.startedAt : command.createdAt,
    command: wire,
  } as SyncOperation;
}

export interface RebaseResult {
  session: Session;
  mutations: Exclude<Mutation, { type: 'start' }>[];
  times: string[];
  warnings: string[];
}
// Build a preview of the entire undelivered suffix, never PUT a stale full workout.
export function rebase(server: Workout, local: Session, commands: Command[], now: string): RebaseResult {
  const originalPlan = local.snapshot.planSnapshot;
  if (
    originalPlan &&
    (originalPlan.plan_snapshot_id !== server.plan.plan_snapshot_id ||
      originalPlan.snapshot_schema_version !== server.plan.snapshot_schema_version)
  )
    throw new WorkoutError('Исходный снимок тренировки изменился. Автоматический перенос запрещён.');
  let session = sessionFromWorkout(server, local.revision);
  const mutations: RebaseResult['mutations'] = [];
  const warnings: string[] = [];
  const times: string[] = [];
  const remapped = new Map<string, string>();
  for (const command of commands) {
    let mutation = structuredClone(command.mutation);
    if (mutation.type === 'start') continue;
    if ('setId' in mutation) mutation.setId = remapped.get(mutation.setId) ?? mutation.setId;
    const targetId = 'setId' in mutation ? mutation.setId : '';
    if (mutation.type === 'editSet' && !session.sets.some((s) => s.id === targetId && !s.deletedAt)) {
      const original = local.sets.find(
        (s) => s.id === ('setId' in command.mutation ? command.mutation.setId : ''),
      );
      if (!original) throw new WorkoutError('Нет исходного подхода для разбора конфликта.');
      const id = crypto.randomUUID();
      remapped.set(mutation.setId, id);
      mutation = { type: 'addSet', setId: id, exerciseId: original.exerciseId, values: mutation.values };
      warnings.push(
        'Удалённый на сервере подход будет записан новым подходом; исходное удаление останется в истории.',
      );
    }
    if (mutation.type === 'deleteSet' && !session.sets.some((s) => s.id === targetId && !s.deletedAt))
      continue;
    if (mutation.type === 'complete' && session.status === 'completed') continue;
    if (mutation.type === 'cancelEmpty' && session.status === 'cancelled') continue;
    session = applyMutation(session, mutation, command.createdAt || now);
    if ('setId' in mutation && (mutation.type === 'addSet' || mutation.type === 'editSet')) {
      const before = local.sets.find(
        (s) => s.id === ('setId' in command.mutation ? command.mutation.setId : ''),
      );
      const after = session.sets.find((s) => s.id === mutation.setId)!;
      if (
        before &&
        (before.plannedTargetId !== after.plannedTargetId ||
          before.comparisonTarget?.id !== after.comparisonTarget?.id ||
          (before.side ?? null) !== (after.side ?? null))
      )
        warnings.push('Плановая цель подхода изменится. Проверь предпросмотр перед подтверждением.');
    }
    mutations.push(mutation);
    times.push(command.createdAt || now);
  }
  return { session, mutations, times, warnings: [...new Set(warnings)] };
}
