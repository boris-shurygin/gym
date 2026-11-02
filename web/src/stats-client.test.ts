import 'fake-indexeddb/auto';
import { afterEach, expect, it, vi } from 'vitest';
import {
  activityPoints,
  exerciseOptions,
  exerciseSeries,
  loadReport,
  savedReport,
  seriesKey,
  type Summary,
} from './stats-client';
import type { ExerciseV2, SessionInfo } from './account';

const session: SessionInfo = {
  user_id: 'stats-alice',
  csrf_token: 'synthetic',
  expires_at: '2026-11-01T00:00:00Z',
  analytics_timezone: 'UTC',
};
afterEach(() => vi.restoreAllMocks());

it('ряды разделены по стороне, числу снарядов, измерению и прежней семантике', () => {
  const exercise: ExerciseV2 = {
    template_exercise_id: 'e',
    exercise_id: 'row',
    equipment_id: 'dumbbells',
    name: 'Пример',
    rule: 'fixed',
    measurement: 'reps',
    sides: 'each',
    set_structure: 'straight',
    rest_seconds: null,
    load: { kind: 'external', weight_convention: 'per_implement_kg', implement_count: 2, step_g: 500 },
    targets: [],
  };
  const options = exerciseOptions(exercise);
  expect(options.map((e) => e.series_key)).toEqual([
    'v2|reps|external|per_implement_kg|2|left|straight',
    'v2|reps|external|per_implement_kg|2|right|straight',
  ]);
  expect(seriesKey(options[0])).not.toBe(seriesKey(options[1]));
  const one = exerciseOptions({
    ...exercise,
    load: {
      ...exercise.load,
      kind: 'external',
      weight_convention: 'per_implement_kg',
      implement_count: 1,
      step_g: 500,
    },
  });
  expect(seriesKey(one[0])).not.toBe(seriesKey(options[0]));
  const timed = exerciseOptions({ ...exercise, measurement: 'duration', load: { kind: 'bodyweight' } });
  expect(timed[0].description).toContain('время');
  expect(timed[0].description).not.toContain('кг');
});

it('кэш отделён по владельцам, запрос привязан к ожидаемому аккаунту', async () => {
  const fetch = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValue(new Response(JSON.stringify({ history_revision: 4 })));
  const path = '/api/v1/stats/summary?period_days=7';
  await loadReport(session.user_id, path, session, new AbortController().signal);
  expect(fetch.mock.calls[0][1]?.headers).toMatchObject({ 'X-Gym-Expected-Owner': session.user_id });
  expect(await savedReport('stats-bob', path)).toBeUndefined();
  expect(await savedReport(session.user_id, path)).toEqual({ history_revision: 4 });
  await expect(loadReport('stats-bob', path, session, new AbortController().signal)).rejects.toThrow(
    'account_mismatch',
  );
});

it('страницы графика одной ревизии объединяются; смешанный снимок отклоняется', async () => {
  const page = {
    history_revision: 3,
    generated_at: '2026-10-31T00:00:00Z',
    freshness: 'fresh',
    points: [{ workout_id: 'first' }],
    next_cursor: 'cursor',
  };
  const fetch = vi
    .spyOn(globalThis, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify(page)))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ ...page, points: [{ workout_id: 'second' }], next_cursor: null })),
    );
  expect(
    (await exerciseSeries('/series?period_days=30', session, new AbortController().signal)).points,
  ).toHaveLength(2);
  fetch
    .mockResolvedValueOnce(new Response(JSON.stringify(page)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...page, history_revision: 4, next_cursor: null })));
  await expect(
    exerciseSeries('/series?period_days=7', session, new AbortController().signal),
  ).rejects.toThrow('history_changed');
});

it('пустые календарные дни не исчезают при переходе времени', () => {
  const report = {
    start_date: '2026-10-24',
    end_date_exclusive: '2026-10-27',
    activity: [{ date: '2026-10-25', workout_count: 2 }],
  } as Summary;
  expect(activityPoints(report)).toEqual([
    { date: '2026-10-24', value: 0 },
    { date: '2026-10-25', value: 2 },
    { date: '2026-10-26', value: 0 },
  ]);
});
