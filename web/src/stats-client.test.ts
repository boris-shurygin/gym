import 'fake-indexeddb/auto';
import { afterEach, expect, it, vi } from 'vitest';
import { activityPoints, exerciseSeries, loadReport, savedReport, type Summary } from './stats-client';
import type { SessionInfo } from './account';

const session: SessionInfo = {
  user_id: 'stats-alice',
  csrf_token: 'synthetic',
  expires_at: '2026-11-01T00:00:00Z',
  analytics_timezone: 'UTC',
};
afterEach(() => vi.restoreAllMocks());

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
