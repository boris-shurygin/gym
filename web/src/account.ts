import { exerciseLoadDescription, type Exercise, type Program } from './model';
import type { Receipt } from './sync-model';
import { recordDiagnostic } from './telemetry';

export interface SessionInfo {
  user_id: string;
  yandex_login?: string | null;
  csrf_token: string;
  expires_at: string;
  analytics_timezone: string;
}
export interface Content {
  name: string;
  templates: {
    template_id: string;
    name: string;
    description: string;
    exercises: {
      template_exercise_id: string;
      exercise_id: string;
      equipment_id: string;
      name: string;
      weight_convention: 'total_external_kg' | 'machine_marking_kg' | 'per_implement_kg';
      weight_step_g: number;
      rule: 'fixed';
      targets: {
        target_id: string;
        weight_g: number;
        suggested_reps: number;
        rep_min: number | null;
        rep_max: number | null;
      }[];
    }[];
  }[];
}
export type WeightConvention = Content['templates'][number]['exercises'][number]['weight_convention'];
export interface TargetV2 {
  target_id: string;
  weight_g: number | null;
  scale_milli: number | null;
  suggested_reps: number | null;
  duration_seconds: number | null;
  rep_min: number | null;
  rep_max: number | null;
}
export interface ExerciseV2 {
  template_exercise_id: string;
  exercise_id: string;
  equipment_id: string;
  name: string;
  measurement: 'reps' | 'duration';
  load:
    | { kind: 'bodyweight' }
    | { kind: 'external'; weight_convention: WeightConvention; implement_count: 1 | 2; step_g: number }
    | { kind: 'machine_scale'; step_milli: number };
  sides: 'single' | 'each';
  set_structure: 'straight' | 'myo';
  rest_seconds: { min: number; max: number } | null;
  rule: 'fixed';
  targets: TargetV2[];
}
export interface ContentV2 {
  content_schema_version: 2;
  name: string;
  templates: { template_id: string; name: string; description: string; exercises: ExerciseV2[] }[];
}
export type ProgramContent = Content | ContentV2;
export type ProgramExercise = Content['templates'][number]['exercises'][number] | ExerciseV2;

export function exerciseFromContent(e: ProgramExercise): Exercise {
  if (!('measurement' in e))
    return {
      id: e.template_exercise_id,
      name: e.name,
      equipment: weightDescription(e.weight_convention),
      weightStepKg: e.weight_step_g / 1000,
      targets: e.targets.map((t) => ({
        id: t.target_id,
        weightKg: t.weight_g / 1000,
        reps: t.suggested_reps,
      })),
    };
  const load: Exercise['load'] =
    e.load.kind === 'external'
      ? {
          kind: 'external',
          weightConvention: e.load.weight_convention,
          implementCount: e.load.implement_count,
          stepKg: e.load.step_g / 1000,
        }
      : e.load.kind === 'machine_scale'
        ? { kind: 'machine_scale', step: e.load.step_milli / 1000 }
        : { kind: 'bodyweight' };
  const exercise: Exercise = {
    id: e.template_exercise_id,
    name: e.name,
    equipment: '',
    measurement: e.measurement,
    load,
    weightStepKg: load.kind === 'external' ? load.stepKg : null,
    sides: e.sides,
    setStructure: e.set_structure,
    restSeconds: e.rest_seconds,
    targets: e.targets.map((t) => ({
      id: t.target_id,
      weightKg: t.weight_g === null ? null : t.weight_g / 1000,
      reps: t.suggested_reps,
      scaleValue: t.scale_milli === null ? null : t.scale_milli / 1000,
      durationSeconds: t.duration_seconds,
    })),
  };
  exercise.equipment = exerciseLoadDescription(exercise);
  return exercise;
}
export interface Version {
  program_version_id: string;
  owner_id: string;
  content: ProgramContent;
}
export interface ProgramView {
  owner_id: string;
  version: Version | null;
}
export interface CachedProfile {
  owner: string;
  program: ProgramView;
}

export class ApiError extends Error {
  constructor(
    public code: string,
    public trace = '',
    public status = 0,
    public receipt?: Receipt,
    public retryAfter = 0,
  ) {
    super(code);
  }
}

export async function api<T>(
  path: string,
  input?: object,
  session?: SessionInfo,
  signal?: AbortSignal,
  method: 'POST' | 'PUT' = 'POST',
): Promise<T> {
  const started = performance.now();
  const response = await fetch(path, {
    method: input ? method : 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    signal: signal ?? AbortSignal.timeout(15_000),
    headers: {
      'X-Gym-Schema-Version': '2',
      ...(input ? { 'Content-Type': 'application/json', 'X-CSRF-Token': session?.csrf_token ?? '' } : {}),
      ...(session ? { 'X-Gym-Expected-Owner': session.user_id } : {}),
    },
    body: input ? JSON.stringify(input) : undefined,
  });
  if (performance.now() - started > 3000)
    recordDiagnostic('api_request_slow', 'server', performance.now() - started);
  if (response.status === 204) return undefined as T;
  const header = response.headers.get('Retry-After');
  const retry =
    header && /^\d+$/.test(header)
      ? Number(header)
      : header
        ? Math.max(0, (Date.parse(header) - Date.now()) / 1000)
        : 0;
  let value;
  try {
    value = await response.json();
  } catch {
    if (response.ok) throw new ApiError('unavailable', '', 502, undefined, retry || 0);
  }
  if (!response.ok) {
    throw new ApiError(
      value?.error?.code ?? 'unavailable',
      value?.error?.trace_id ?? '',
      response.status,
      value?.error?.receipt ?? undefined,
      Math.max(retry || 0, value?.error?.retry_after_seconds ?? 0),
    );
  }
  return value as T;
}

export function toProgram(view: ProgramView): Program {
  if (view.version && view.version.owner_id !== view.owner_id) throw new Error('Владелец плана не совпал');
  return {
    versionId: view.version?.program_version_id ?? 'unpublished',
    templates: (view.version?.content.templates ?? []).map((t) => ({
      id: t.template_id,
      name: t.name,
      description: t.description,
      exercises: t.exercises.map(exerciseFromContent),
    })),
  };
}

export function weightDescription(
  convention: Content['templates'][number]['exercises'][number]['weight_convention'],
): string {
  return {
    total_external_kg: 'Общий внешний вес, кг',
    machine_marking_kg: 'Вес по маркировке тренажёра, кг',
    per_implement_kg: 'Вес одного снаряда, кг',
  }[convention];
}

// Отдельный DB хранит только привязку владельца/план. Сессии, CSRF и
// ответы /auth/session сюда не попадают. local-demo остаётся в прежнем DB.
function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('gym-profiles-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('profiles');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function profileCache(owner: string, value?: CachedProfile): Promise<CachedProfile | undefined> {
  const db = await database();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('profiles', value ? 'readwrite' : 'readonly');
      const request = value
        ? tx.objectStore('profiles').put(value, owner)
        : tx.objectStore('profiles').get(owner);
      let result: CachedProfile | undefined;
      request.onsuccess = () => {
        if (!value) result = request.result;
      };
      tx.oncomplete = () => resolve(value ?? result);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}
