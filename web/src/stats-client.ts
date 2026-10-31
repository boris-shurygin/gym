import { api, type SessionInfo } from './account';
import type { Workout } from './sync-model';

export interface Report {
  history_revision: number;
  analytics_version: string;
  freshness: 'fresh' | 'stale' | 'recalculating';
  generated_at: string;
  period_days: number;
  timezone: string;
  start_date: string;
  end_date_exclusive: string;
  complete: boolean;
}
export interface Summary extends Report {
  workout_count: number;
  working_set_count: number;
  rep_count: number;
  activity: { date: string; workout_count: number }[];
}

export function activityPoints(report: Summary): { date: string; value: number }[] {
  const counts = new Map(report.activity.map((point) => [point.date, point.workout_count]));
  const result: { date: string; value: number }[] = [];
  // Это перечисление календарных меток сервера, а не перевод времени занятия.
  const current = new Date(report.start_date + 'T00:00:00Z');
  for (let i = 0; i < 90; i++) {
    const date = current.toISOString().slice(0, 10);
    if (date >= report.end_date_exclusive) break;
    result.push({ date, value: counts.get(date) ?? 0 });
    current.setUTCDate(current.getUTCDate() + 1);
  }
  return result;
}
export interface Series extends Report {
  points: {
    workout_id: string;
    started_at: string;
    max_weight_g: number;
    max_weight_reps: number;
    volume_g_reps: number;
  }[];
  next_cursor: string | null;
}
export interface ExerciseKey {
  exercise_id: string;
  equipment_id: string;
  weight_convention: string;
  name: string;
}
export interface HistoryPage {
  items: Workout[];
  next_cursor: string | null;
}
export const seriesKey = (exercise: ExerciseKey) =>
  [exercise.exercise_id, exercise.equipment_id, exercise.weight_convention].join('|');

interface Cached {
  owner: string;
  key: string;
  saved: number;
  value: unknown;
  storageKey: string;
}
async function cache(owner: string, key: string, value?: unknown): Promise<unknown> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open('gym-statistics-v1', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('reports');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('reports', value === undefined ? 'readonly' : 'readwrite');
      const store = tx.objectStore('reports');
      const report = value as Partial<Report> | undefined;
      const id = JSON.stringify([
        owner,
        key,
        report?.analytics_version ?? null,
        report?.history_revision ?? null,
        report?.start_date ?? null,
        report?.end_date_exclusive ?? null,
      ]);
      const request =
        value === undefined
          ? store.getAll()
          : store.put({ owner, key, storageKey: id, saved: Date.now(), value }, id);
      let result: unknown;
      request.onsuccess = () => {
        const row =
          value === undefined
            ? (request.result as Cached[])
                .filter((item) => item.owner === owner && item.key === key)
                .sort((a, b) => b.saved - a.saved)[0]
            : undefined;
        if (value === undefined && row?.owner === owner && row.key === key) result = row.value;
      };
      if (value !== undefined) {
        const all = store.getAll();
        all.onsuccess = () => {
          (all.result as Cached[])
            .filter((row) => row.owner === owner && row.storageKey !== id)
            .sort((a, b) => b.saved - a.saved)
            .forEach((row, index) => {
              if (row.key === key || index >= 39) store.delete(row.storageKey ?? owner + ':' + row.key);
            });
        };
      }
      tx.oncomplete = () => resolve(value ?? result);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function savedReport<T>(owner: string, key: string): Promise<T | undefined> {
  return (await cache(owner, key).catch(() => undefined)) as T | undefined;
}
export async function loadReport<T>(
  owner: string,
  path: string,
  session: SessionInfo,
  signal: AbortSignal,
): Promise<T> {
  if (owner !== session.user_id) throw new Error('account_mismatch');
  const result = await api<T>(path, undefined, session, signal);
  if (signal.aborted) throw new Error('cancelled');
  await cache(owner, path, result).catch(() => undefined);
  return result;
}

export async function exerciseSeries(
  path: string,
  session: SessionInfo,
  signal: AbortSignal,
): Promise<Series> {
  let result = await api<Series>(path, undefined, session, signal);
  const seen = new Set<string>();
  while (result.next_cursor) {
    const cursor = result.next_cursor;
    if (seen.has(cursor) || seen.size >= 100) throw new Error('incomplete_report');
    seen.add(cursor);
    const page = await api<Series>(
      path + '&cursor=' + encodeURIComponent(cursor),
      undefined,
      session,
      signal,
    );
    if (
      page.history_revision !== result.history_revision ||
      page.generated_at !== result.generated_at ||
      page.freshness !== 'fresh'
    )
      throw new Error('history_changed');
    result = { ...result, points: [...result.points, ...page.points], next_cursor: page.next_cursor };
  }
  if (signal.aborted) throw new Error('cancelled');
  await cache(session.user_id, path, result).catch(() => undefined);
  return result;
}
