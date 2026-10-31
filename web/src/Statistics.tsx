import { useEffect, useState } from 'react';
import { api, type ProgramView, type SessionInfo, weightDescription } from './account';
import {
  exerciseSeries,
  loadReport,
  savedReport,
  seriesKey,
  activityPoints,
  type ExerciseKey,
  type HistoryPage,
  type Series,
  type Summary,
} from './stats-client';

const number = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 });
const day = (value: string, timezone = 'UTC') =>
  /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? value.split('-').reverse().join('.')
    : new Date(value).toLocaleDateString('ru-RU', { timeZone: timezone });

function LineChart({
  points,
  unit,
  label,
  timezone = 'UTC',
}: {
  points: { date: string; value: number; note?: string }[];
  unit: string;
  label: string;
  timezone?: string;
}) {
  const maximum = Math.max(1, ...points.map((p) => p.value));
  const coordinates = points.map((point, i) => ({
    ...point,
    x: 35 + (i * 520) / Math.max(1, points.length - 1),
    y: 155 - (point.value / maximum) * 125,
  }));
  return (
    <figure className="chart card">
      <figcaption>
        <strong>{label}</strong>
        <span className="caption">{unit}</span>
      </figcaption>
      {!points.length ? (
        <p className="caption">За этот период нет завершённых рабочих подходов.</p>
      ) : (
        <>
          <svg viewBox="0 0 590 190" role="img" aria-label={`${label}, ${unit}`}>
            <text x="5" y="20">
              {number.format(maximum)}
            </text>
            <text x="5" y="160">
              0
            </text>
            <path d="M30 25V160H565" className="chart-axis" />
            <polyline points={coordinates.map((p) => `${p.x},${p.y}`).join(' ')} className="chart-line" />
            {coordinates.map((p, i) => (
              <circle key={i} cx={p.x} cy={p.y} r="4" className="chart-dot">
                <title>
                  {day(p.date, timezone)}: {number.format(p.value)} {unit}
                  {p.note ? ` · ${p.note}` : ''}
                </title>
              </circle>
            ))}
            <text x="35" y="183">
              {day(points[0].date, timezone)}
            </text>
            <text x="555" y="183" textAnchor="end">
              {day(points.at(-1)!.date, timezone)}
            </text>
          </svg>
          <details>
            <summary>Точные значения</summary>
            <table>
              <thead>
                <tr>
                  <th>Дата</th>
                  <th>
                    {label}, {unit}
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((p, i) => (
                  <tr key={i}>
                    <td>{day(p.date, timezone)}</td>
                    <td>
                      {number.format(p.value)} {p.note}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </figure>
  );
}

export default function Statistics({
  owner,
  session: provided,
  program,
  pending = 0,
}: {
  owner: string;
  session?: SessionInfo;
  program?: ProgramView;
  pending?: number;
}) {
  const [period, setPeriod] = useState(30);
  const [timezone, setTimezone] = useState(provided?.analytics_timezone ?? 'UTC');
  const [zoneDraft, setZoneDraft] = useState(provided?.analytics_timezone ?? 'UTC');
  const [summary, setSummary] = useState<Summary>();
  const [history, setHistory] = useState<HistoryPage>();
  const [series, setSeries] = useState<Series>();
  const [session, setSession] = useState(provided);
  const [stale, setStale] = useState(true);
  const [message, setMessage] = useState('');
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState('');
  const [seriesPath, setSeriesPath] = useState('');
  const criteria = new URLSearchParams({ period_days: String(period), timezone }).toString();
  const summaryPath = '/api/v1/stats/summary?' + criteria;
  const historyPath = '/api/v1/workouts?limit=20';
  const catalog = new Map<string, ExerciseKey>();
  for (const template of [
    ...(program?.version?.content.templates ?? []),
    ...(history?.items ?? []).map((w) => w.plan.template),
  ])
    for (const e of template.exercises) catalog.set(seriesKey(e), e);
  const options = [...catalog.entries()];

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setSummary(undefined);
    setHistory(undefined);
    setSeries(undefined);
    setSeriesPath('');
    setStale(true);
    setBusy(true);
    async function load() {
      const cached = await savedReport<Summary>(owner, summaryPath);
      const oldHistory = await savedReport<HistoryPage>(owner, historyPath);
      if (!active) return;
      setSummary(cached);
      setHistory(oldHistory);
      try {
        const current = await api<SessionInfo>('/auth/session', undefined, undefined, controller.signal);
        if (current.user_id !== owner) {
          setSummary(undefined);
          setHistory(undefined);
          throw new Error('account_mismatch');
        }
        if (!active) return;
        setSession(current);
        if (!provided && timezone === 'UTC' && current.analytics_timezone !== timezone) {
          setTimezone(current.analytics_timezone);
          setZoneDraft(current.analytics_timezone);
          return;
        }
        const report = await loadReport<Summary>(owner, summaryPath, current, controller.signal);
        const workouts = await loadReport<HistoryPage>(owner, historyPath, current, controller.signal);
        if (!active) return;
        setSummary(report);
        setHistory(workouts);
        setStale(false);
        setMessage('');
      } catch {
        if (active) {
          setStale(true);
          setMessage(
            'Нет свежего серверного отчёта. Сохранённые данные могут быть устаревшими; локальная очередь в них не включена.',
          );
        }
      } finally {
        if (active) setBusy(false);
      }
    }
    void load();
    const refresh = () => setRevision((r) => r + 1);
    const offline = () => setStale(true);
    window.addEventListener('online', refresh);
    window.addEventListener('offline', offline);
    return () => {
      active = false;
      controller.abort();
      window.removeEventListener('online', refresh);
      window.removeEventListener('offline', offline);
    };
  }, [owner, summaryPath, timezone, provided, revision]);

  useEffect(() => {
    if (!seriesPath) return;
    const controller = new AbortController();
    let active = true;
    setSeries(undefined);
    void (async () => {
      const cached = await savedReport<Series>(owner, seriesPath);
      if (!active) return;
      setSeries(cached ? { ...cached, freshness: 'stale' } : undefined);
      try {
        if (!session || session.user_id !== owner) throw new Error('session_required');
        const report = await exerciseSeries(seriesPath, session, controller.signal);
        if (active) setSeries(report);
      } catch {
        if (active)
          setMessage('График не обновлён. Если сохранённый отчёт есть, показана его прежняя версия.');
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [owner, seriesPath, session]);

  async function moreHistory() {
    if (!session || !history?.next_cursor || busy) return;
    setBusy(true);
    try {
      const page = await api<HistoryPage>(
        historyPath + '&cursor=' + encodeURIComponent(history.next_cursor),
        undefined,
        session,
      );
      setHistory((previous) =>
        previous ? { items: [...previous.items, ...page.items], next_cursor: page.next_cursor } : page,
      );
    } catch {
      setMessage('Страница истории недоступна. Обнови историю для получения нового курсора.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="statistics" aria-label="Личная статистика">
      <details className="card timezone-settings">
        <summary>Часовой пояс статистики</summary>
        <p className="caption">
          Выбери пояс для календарных границ отчётов. Пока не выбрал, используется UTC.
        </p>
        <label>
          Часовой пояс IANA
          <input
            value={zoneDraft}
            onChange={(e) => setZoneDraft(e.target.value)}
            placeholder="Europe/Moscow"
          />
        </label>
        <div className="account-actions">
          <button
            className="secondary"
            onClick={() => setZoneDraft(Intl.DateTimeFormat().resolvedOptions().timeZone)}
          >
            Использовать пояс браузера
          </button>
          <button
            className="secondary"
            disabled={!session || busy}
            onClick={() =>
              void (async () => {
                if (!session) return;
                setBusy(true);
                try {
                  const result = await api<{ analytics_timezone: string }>(
                    '/api/v1/profile',
                    {
                      request_id: crypto.randomUUID(),
                      expected_owner_id: owner,
                      analytics_timezone: zoneDraft,
                    },
                    session,
                    undefined,
                    'PUT',
                  );
                  setTimezone(result.analytics_timezone);
                  setSession({ ...session, analytics_timezone: result.analytics_timezone });
                  setMessage('Часовой пояс сохранён. Отчёты обновляются.');
                } catch {
                  setMessage('Часовой пояс не сохранён. Укажи допустимый пояс IANA и проверь связь.');
                } finally {
                  setBusy(false);
                }
              })()
            }
          >
            Сохранить часовой пояс
          </button>
        </div>
      </details>
      <div className="between">
        <div>
          <p className="eyebrow">Твои результаты</p>
          <h2>Статистика и история</h2>
        </div>
        <button className="secondary" disabled={busy} onClick={() => setRevision((r) => r + 1)}>
          Обновить статистику
        </button>
      </div>
      <div className="stats-filters">
        <label>
          Период
          <select value={period} onChange={(e) => setPeriod(Number(e.target.value))}>
            {[7, 30, 90].map((n) => (
              <option key={n} value={n}>
                {n} дней
              </option>
            ))}
          </select>
        </label>
        <span className="caption">Часовой пояс: {timezone}</span>
      </div>
      {message && (
        <p role="status" className="caption">
          {message}
        </p>
      )}
      {pending > 0 && (
        <p className="caption">
          На устройстве есть изменения в очереди: {pending}. Они ещё не включены в серверные показатели.
        </p>
      )}
      {summary ? (
        <>
          <p className="caption">
            {stale || summary.freshness !== 'fresh'
              ? 'Сохранённый отчёт · может быть устаревшим'
              : 'Серверные данные'}{' '}
            · обновлено {new Date(summary.generated_at).toLocaleString('ru-RU')} · ревизия{' '}
            {summary.history_revision} · {summary.complete ? 'Полный период' : 'Неполное покрытие'}
          </p>
          <div className="stats-totals">
            {[
              [summary.workout_count, 'тренировок'],
              [summary.working_set_count, 'рабочих подходов'],
              [summary.rep_count, 'повторений'],
            ].map(([value, label]) => (
              <div className="card" key={label}>
                <strong>{value}</strong>
                <span>{label}</span>
              </div>
            ))}
          </div>
          <LineChart label="Тренировки по дням" unit="занятий" points={activityPoints(summary)} />
        </>
      ) : (
        <p className="card empty">
          {busy
            ? 'Загружаем серверные показатели…'
            : 'Отчёт ещё не загружен. Локальная история не заменяет полный серверный период.'}
        </p>
      )}
      {!!options.length && (
        <div className="card">
          <label>
            Упражнение и оборудование
            <select
              value={selected || options[0][0]}
              onChange={(e) => {
                setSelected(e.target.value);
                setSeries(undefined);
                setSeriesPath('');
              }}
            >
              {options.map(([key, e]) => (
                <option value={key} key={key}>
                  {e.name} · {e.equipment_id} ·{' '}
                  {weightDescription(e.weight_convention as Parameters<typeof weightDescription>[0])}
                </option>
              ))}
            </select>
          </label>
          <button
            className="secondary"
            onClick={() => {
              const e = catalog.get(selected || options[0][0])!;
              setSeriesPath(
                `/api/v1/stats/exercises/${encodeURIComponent(e.exercise_id)}?${criteria}&equipment_id=${encodeURIComponent(e.equipment_id)}&weight_convention=${e.weight_convention}`,
              );
            }}
          >
            Показать графики
          </button>
        </div>
      )}
      {series && (
        <>
          <p className="caption">
            Графики:{' '}
            {series.freshness === 'fresh' && !stale
              ? 'серверные данные'
              : 'сохранённые данные, проверь актуальность'}{' '}
            · ревизия {series.history_revision} · разминка исключена
          </p>
          <LineChart
            label="Наибольший рабочий вес"
            timezone={series.timezone}
            unit="кг"
            points={series.points.map((p) => ({
              date: p.started_at,
              value: p.max_weight_g / 1000,
              note: `× ${p.max_weight_reps} повт.`,
            }))}
          />
          <LineChart
            label="Рабочий объём упражнения"
            timezone={series.timezone}
            unit="кг·повт."
            points={series.points.map((p) => ({ date: p.started_at, value: p.volume_g_reps / 1000 }))}
          />
        </>
      )}
      <h3>История на сервере</h3>
      <p className="caption">
        Сохранённые серверные снимки. Записи и очередь этого устройства доступны отдельно в приложении.
      </p>
      {!history?.items.length && (
        <p className="caption">Серверных занятий пока нет или история не загружена.</p>
      )}
      {history?.items.map((workout) => (
        <details key={workout.workout_id} className="card server-workout">
          <summary>
            {day(workout.started_at, timezone)} · {workout.plan.template.name} ·{' '}
            {{ active: 'идёт', completed: 'завершена', cancelled: 'отменена' }[workout.status]}
          </summary>
          <p className="caption">
            Сервер · редакция {workout.server_revision} · {day(workout.updated_at, timezone)}
          </p>
          {workout.plan.template.exercises.map((e) => (
            <div key={e.template_exercise_id}>
              <h4>{e.name}</h4>
              {workout.sets
                .filter((s) => s.session_exercise_id === e.template_exercise_id && !s.deleted_at)
                .map((s) => (
                  <p key={s.set_id}>
                    {s.kind === 'warmup' ? 'Разминка' : 'Рабочий'}: {number.format(s.weight_g / 1000)} кг ×{' '}
                    {s.reps}
                  </p>
                ))}
            </div>
          ))}
        </details>
      ))}
      {history?.next_cursor && (
        <button className="secondary wide" disabled={busy} onClick={() => void moreHistory()}>
          Более ранние тренировки
        </button>
      )}
    </section>
  );
}
