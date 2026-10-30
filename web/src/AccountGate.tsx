import { useEffect, useState } from 'react';
import App from './App';
import PwaUpdate from './PwaUpdate';
import { publishStagingProgram, stagingProgram } from './staging-program';
import { DEMO_OWNER, demoProgram } from './demo-program';
import {
  api,
  ApiError,
  profileCache,
  toProgram,
  type CachedProfile,
  type SessionInfo,
  type ProgramView,
  type Content,
} from './account';

interface Change {
  change_id: string;
  change_revision: number;
  content_digest: string;
  status: string;
  proposal: { base_program_version_id: string | null; content: Content; rationale: string };
}
const ownerKey = 'gym.active-owner';
const modeKey = 'gym.mode';

function Plan({ content }: { content: Content }) {
  return (
    <section className="card">
      <h3>{content.name}</h3>
      {content.templates.map((t) => (
        <div key={t.template_id}>
          <h4>{t.name}</h4>
          <p>{t.description}</p>
          {t.exercises.map((e) => (
            <p key={e.template_exercise_id}>
              <strong>{e.name}</strong> · {e.equipment_id} ·{' '}
              {
                {
                  total_external_kg: 'общий внешний вес',
                  machine_marking_kg: 'маркировка тренажёра',
                  per_implement_kg: 'на один снаряд',
                }[e.weight_convention]
              }
              <br />
              {e.targets
                .map(
                  (t) =>
                    `${t.weight_g / 1000} кг × ${t.suggested_reps}${t.rep_min === null ? '' : ` (${t.rep_min}–${t.rep_max})`}`,
                )
                .join('; ')}
            </p>
          ))}
        </div>
      ))}
    </section>
  );
}

export default function Account() {
  const [profile, setProfile] = useState<CachedProfile>();
  const [session, setSession] = useState<SessionInfo>();
  const [candidate, setCandidate] = useState<SessionInfo>();
  const [demo, setDemo] = useState(localStorage.getItem(modeKey) === 'demo');
  const [message, setMessage] = useState('Проверяем вход…');
  const [change, setChange] = useState<Change>();
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const [staging, setStaging] = useState(false);
  const account = session ?? candidate;

  useEffect(() => {
    let active = true;
    void api<{ purpose: string }>('/__gym/release')
      .then((info) => {
        if (active) setStaging(info.purpose === 'staging-release');
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  async function addStagingProgram() {
    if (!staging || !session || !profile || profile.program.version || busy) return;
    setBusy(true);
    try {
      const program = await publishStagingProgram(session);
      if (localStorage.getItem(ownerKey) !== session.user_id || localStorage.getItem('gym.hidden')) return;
      const cached = { owner: session.user_id, program };
      await profileCache(session.user_id, cached);
      setProfile(cached);
      setMessage('Программа сохранена в аккаунте. Можно начать тренировку.');
      window.dispatchEvent(new Event('gym-account-ready'));
    } catch {
      setMessage(
        'Добавление программы не подтверждено. Повтори попытку: существующая программа и тренировки сохранятся.',
      );
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const previous = localStorage.getItem(ownerKey);
      let verified: SessionInfo | undefined;
      if (previous && !localStorage.getItem('gym.hidden')) {
        const cached = await profileCache(previous).catch(() => undefined);
        if (!cancelled && cached?.owner === previous) setProfile(cached);
      }
      try {
        const current = await api<SessionInfo>('/auth/session');
        if (cancelled) return;
        if (previous && previous !== current.user_id) {
          setProfile(undefined);
          setSession(undefined);
          setCandidate(current);
          setMessage('Выполнен вход в другой аккаунт. Прежние записи останутся в его отдельном профиле.');
          return;
        }
        verified = current;
        setSession(current);
        const program = await api<ProgramView>('/api/v1/program');
        if (program.owner_id !== current.user_id) throw new Error('account_mismatch');
        toProgram(program);
        const cached = { owner: current.user_id, program };
        await profileCache(current.user_id, cached);
        if (cancelled) return;
        localStorage.setItem(ownerKey, current.user_id);
        localStorage.removeItem('gym.hidden');
        localStorage.removeItem(modeKey);
        setDemo(false);
        setProfile(cached);
        setSession(current);
        setMessage('');
        window.dispatchEvent(new Event('gym-account-ready'));
        const changeId =
          location.hash.match(/change_id=([a-f0-9-]{36})/)?.[1] ??
          (location.hash.includes('/program')
            ? localStorage.getItem(`gym.review-change:${current.user_id}`)
            : null);
        if (changeId) {
          const draft = await api<Change>(`/api/v1/program/changes/${changeId}`);
          if (!cancelled) {
            localStorage.setItem(`gym.review-change:${current.user_id}`, changeId);
            setChange(draft);
          }
        }
      } catch (error) {
        if (cancelled) return;
        setSession(verified);
        setMessage(
          error instanceof ApiError && error.code === 'account_disabled'
            ? 'Доступ к серверу закрыт. Локальные результаты сохранены.'
            : error instanceof ApiError && [401, 403].includes(error.status)
              ? 'Войди через Яндекс ID для отправки записей. Локальные тренировки сохранены.'
              : 'Нет связи с сервером. Локальные тренировки доступны; вход повторять пока не нужно.',
        );
        if (previous && !localStorage.getItem('gym.hidden')) {
          const cached = await profileCache(previous);
          if (!cancelled && cached?.owner === previous) {
            toProgram(cached.program);
            setProfile(cached);
          }
        }
      }
    }
    // «Скрыть» действует до явного нового входа, включая другие вкладки.
    if (
      !localStorage.getItem('gym.hidden') ||
      location.hash.includes('/app') ||
      location.hash.includes('/program')
    )
      void load();
    else setMessage('Данные на устройстве скрыты. Для открытия войди снова.');
    const hide = (event: StorageEvent) => {
      if (event.key === 'gym.hidden' || event.key === ownerKey) {
        cancelled = true;
        setProfile(undefined);
        setSession(undefined);
        setChange(undefined);
        setMessage('Профиль закрыт или сменился в другой вкладке.');
      }
    };
    window.addEventListener('storage', hide);
    return () => {
      cancelled = true;
      window.removeEventListener('storage', hide);
    };
  }, [revision]);

  function hide(discardConfirmed = false) {
    if (
      !discardConfirmed &&
      document.querySelector('[data-unsaved="true"]') &&
      !window.confirm(
        'Скрыть профиль и сбросить незаписанный ввод? Сохранённые тренировки и очередь останутся.',
      )
    )
      return;
    window.dispatchEvent(new Event('gym-profile-closing'));
    location.hash = '';
    localStorage.setItem('gym.hidden', 'yes');
    localStorage.removeItem(modeKey);
    setProfile(undefined);
    setSession(undefined);
    setChange(undefined);
    setCandidate(undefined);
    setDemo(false);
    setMessage('Данные скрыты на устройстве. Это не отзывает сессию на сервере.');
    setRevision((r) => r + 1);
  }

  async function logout() {
    if (
      !session ||
      busy ||
      !window.confirm(
        'Выйти? Локальные тренировки и очередь останутся на устройстве; отправка продолжится после входа тем же аккаунтом.',
      )
    )
      return;
    if (
      document.querySelector('[data-unsaved="true"]') &&
      !window.confirm('Незаписанный ввод будет сброшен. Продолжить выход?')
    )
      return;
    window.dispatchEvent(new Event('gym-profile-closing'));
    setBusy(true);
    try {
      await api('/auth/logout', {}, session);
      hide(true);
      setMessage('Выход выполнен. Записи сохранены на устройстве.');
    } catch {
      setMessage('Серверный выход не подтверждён. Можно скрыть данные на устройстве.');
      setRevision((r) => r + 1);
    } finally {
      setBusy(false);
    }
  }

  async function publish() {
    if (!session || !change || busy) return;
    const viewed = change;
    setBusy(true);
    try {
      await api(
        `/api/v1/program/changes/${viewed.change_id}/validate`,
        {
          change_id: viewed.change_id,
          expected_revision: viewed.change_revision,
        },
        session,
      );
      await api(
        `/api/v1/program/changes/${viewed.change_id}/approve-and-publish`,
        {
          request_id: crypto.randomUUID(),
          expected_owner_id: session.user_id,
          change_id: viewed.change_id,
          expected_revision: viewed.change_revision,
          base_program_version_id: viewed.proposal.base_program_version_id,
          content_digest: viewed.content_digest,
        },
        session,
      );
      localStorage.removeItem(`gym.review-change:${session.user_id}`);
      setChange(undefined);
      location.hash = '';
      setRevision((r) => r + 1);
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.code === 'session_required'
          ? 'Перед одобрением подтверди вход через Яндекс ID.'
          : 'Публикация не подтверждена. Обнови черновик: программа или его редакция могли измениться.',
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PwaUpdate />
      <section
        className={`account-panel card ${!profile && !demo ? 'account-welcome' : ''}`}
        aria-label="Аккаунт"
      >
        <div className="account-heading">
          <div>
            {!profile && !demo && <p className="eyebrow">Gym</p>}
            <h2>
              {account
                ? account.yandex_login || 'Твой аккаунт'
                : demo
                  ? 'Демо-режим'
                  : profile
                    ? 'Профиль на устройстве'
                    : 'Твои тренировки — рядом'}
            </h2>
          </div>
          {account && (
            <span className="account-status">
              <span className="status-dot" />
              Яндекс ID
            </span>
          )}
        </div>
        {account && !account.yandex_login && (
          <p className="account-message">Подтверди вход, чтобы показать логин аккаунта.</p>
        )}
        {message && !demo && (
          <p className="account-message" role="status">
            {message}
          </p>
        )}
        <div className="account-actions">
          {account && !account.yandex_login && (
            <a className="secondary" href="/auth/login?purpose=reauth&return_to=/app">
              Подтвердить аккаунт
            </a>
          )}
          {!account && (
            <a className="primary" href="/auth/login?return_to=/app">
              Войти через Яндекс ID
            </a>
          )}
          {candidate && (
            <>
              <button
                className="primary"
                onClick={() => {
                  localStorage.setItem(ownerKey, candidate.user_id);
                  localStorage.removeItem('gym.hidden');
                  setCandidate(undefined);
                  setRevision((r) => r + 1);
                }}
              >
                Открыть отдельный профиль
              </button>
              <a className="secondary" href="/auth/login?purpose=switch&return_to=/app">
                Войти прежним аккаунтом
              </a>
            </>
          )}
          {!profile && !candidate && (
            <button
              className="secondary"
              onClick={() => {
                localStorage.setItem(modeKey, 'demo');
                setDemo(true);
              }}
            >
              Открыть демо
            </button>
          )}
          {session && (
            <button className="secondary" onClick={() => setRevision((r) => r + 1)}>
              Обновить программу
            </button>
          )}
        </div>
        {(profile || demo) && (
          <details className="account-options">
            <summary>Управление аккаунтом</summary>
            <div className="account-actions">
              <a className="secondary" href="/auth/update-app">
                Обновить приложение и подтвердить аккаунт
              </a>
              <button className="secondary" onClick={() => hide()}>
                Скрыть данные на устройстве
              </button>
              {session && (
                <button className="secondary" onClick={() => void logout()} disabled={busy}>
                  Выйти
                </button>
              )}
            </div>
          </details>
        )}
      </section>
      {change && session && (
        <main className="program-review">
          <p className="eyebrow">Проверка перед публикацией</p>
          <h1>Изменение программы</h1>
          <p className="subheading">{change.proposal.rationale}</p>
          <h3>Сейчас</h3>
          {profile?.program.version ? (
            <Plan content={profile.program.version.content} />
          ) : (
            <p>Программы нет</p>
          )}
          <h3>Предложено</h3>
          <Plan content={change.proposal.content} />
          <div className="review-actions">
            <button
              className="primary"
              disabled={busy || change.status === 'published'}
              onClick={() => void publish()}
            >
              Одобрить и опубликовать
            </button>
            <a className="secondary" href={`/auth/login?purpose=reauth&return_to=/program`}>
              Подтвердить вход перед одобрением
            </a>
          </div>
        </main>
      )}
      {profile && !candidate && (
        <>
          {staging && session && !profile.program.version && (
            <section className="card staging-program" aria-label="Тестовые шаблоны staging">
              <h2>Попробуй две тестовые тренировки</h2>
              <p>
                По три упражнения и три подхода по 8 повторений. Плановые веса условные, для проверки
                интерфейса; фактический вес введёшь сам.
              </p>
              <ul>
                {stagingProgram.templates.map((template) => (
                  <li key={template.template_id}>
                    <strong>{template.name}</strong>:{' '}
                    {template.exercises
                      .map(
                        (e) =>
                          `${e.name} — ${e.targets[0].weight_g / 1000} кг${e.weight_convention === 'per_implement_kg' ? ' на гантель' : ''}`,
                      )
                      .join('; ')}
                    .
                  </li>
                ))}
              </ul>
              <button className="primary" disabled={busy} onClick={() => void addStagingProgram()}>
                Добавить тестовую программу
              </button>
              <p className="caption">
                Кнопка опубликует оба шаблона в твоём аккаунте staging. Существующая программа не заменяется.
              </p>
            </section>
          )}
          <App key={profile.owner} ownerId={profile.owner} program={toProgram(profile.program)} />
        </>
      )}
      {!profile && demo && !candidate && <App key="demo" ownerId={DEMO_OWNER} program={demoProgram} demo />}
    </>
  );
}
