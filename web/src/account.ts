import type { Program } from './model';

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
export interface Version {
  program_version_id: string;
  owner_id: string;
  content: Content;
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
  ) {
    super(code);
  }
}

export async function api<T>(path: string, input?: object, session?: SessionInfo): Promise<T> {
  const response = await fetch(path, {
    method: input ? 'POST' : 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: input ? { 'Content-Type': 'application/json', 'X-CSRF-Token': session?.csrf_token ?? '' } : {},
    body: input ? JSON.stringify(input) : undefined,
  });
  if (response.status === 204) return undefined as T;
  const value = await response.json();
  if (!response.ok) throw new ApiError(value.error?.code ?? 'unavailable', value.error?.trace_id ?? '');
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
      exercises: t.exercises.map((e) => ({
        id: e.template_exercise_id,
        name: e.name,
        equipment: e.equipment_id,
        weightStepKg: e.weight_step_g / 1000,
        targets: e.targets.map((target) => ({
          id: target.target_id,
          weightKg: target.weight_g / 1000,
          reps: target.suggested_reps,
        })),
      })),
    })),
  };
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
