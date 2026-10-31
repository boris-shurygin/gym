import { useEffect, useState } from 'react';
import { api, type SessionInfo } from './account';

const labels: Record<string, string> = {
  'program:read': 'Читать программу',
  'history:read': 'Читать историю тренировок',
  'stats:read': 'Читать статистику',
  'program:draft': 'Готовить и проверять изменения программы',
  'program:publish': 'Получать результат публикации после твоего одобрения в Gym',
};
interface Grant {
  grant_id: string;
  client_name: string;
  scopes: string[];
  expires_at: string;
  revoked_at: string | null;
}
export function AgentAccess({ session }: { session: SessionInfo }) {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void api<{ items: Grant[] }>('/api/v1/agent-grants', undefined, session, controller.signal)
      .then((data) => setGrants(data.items))
      .catch(() => {
        if (!controller.signal.aborted) setMessage('Подключения сейчас недоступны.');
      });
    return () => controller.abort();
  }, [session]);
  return (
    <details className="card agent-access">
      <summary>Доступ тренировочного агента</summary>
      <p>
        <a
          href="https://github.com/boris-shurygin/gym/blob/main/skills/gym-trainer/SKILL.md"
          target="_blank"
          rel="noreferrer"
        >
          Инструкция тренировочного агента Gym
        </a>
      </p>
      <p className="caption">
        Выход из PWA не отзывает доступ ChatGPT. Отозвать его можно здесь; история останется.
      </p>
      {!grants.length && <p>Подключённых агентов пока нет.</p>}
      {grants.map((grant) => (
        <div key={grant.grant_id}>
          <h3>{grant.client_name}</h3>
          <ul>
            {grant.scopes.map((scope) => (
              <li key={scope}>{labels[scope]}</li>
            ))}
          </ul>
          <p className="caption">До {new Date(grant.expires_at).toLocaleDateString('ru-RU')}</p>
          <button
            className="secondary"
            disabled={busy || !!grant.revoked_at}
            onClick={() => {
              if (!window.confirm('Отозвать доступ этого подключения ChatGPT?')) return;
              setBusy(true);
              void api<Grant>(
                `/api/v1/agent-grants/${grant.grant_id}/revoke`,
                {
                  request_id: crypto.randomUUID(),
                  expected_owner_id: session.user_id,
                  grant_id: grant.grant_id,
                },
                session,
              )
                .then((result) => {
                  setGrants((all) => all.map((g) => (g.grant_id === result.grant_id ? result : g)));
                  setMessage('Доступ отозван.');
                })
                .catch(() => setMessage('Отзыв не подтверждён. Повтори попытку.'))
                .finally(() => setBusy(false));
            }}
          >
            {grant.revoked_at ? 'Доступ отозван' : 'Отозвать доступ'}
          </button>
        </div>
      ))}
      {message && <p role="status">{message}</p>}
    </details>
  );
}

export function OAuthConsent({ session, requestId }: { session?: SessionInfo; requestId: string }) {
  const [details, setDetails] = useState<{ client_name: string; scopes: string[]; resource: string }>();
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!session) return;
    const controller = new AbortController();
    void api<{ client_name: string; scopes: string[]; resource: string }>(
      `/api/v1/oauth/consent?request_id=${requestId}`,
      undefined,
      session,
      controller.signal,
    )
      .then(setDetails)
      .catch(() => {
        if (!controller.signal.aborted)
          setMessage(
            'Запрос подключения устарел или вход не подтверждён. Начни подключение заново в ChatGPT.',
          );
      });
    return () => controller.abort();
  }, [session, requestId]);
  async function decide(allow: boolean) {
    if (!session || busy) return;
    setBusy(true);
    try {
      const result = await api<{ redirect_uri: string }>(
        '/api/v1/oauth/consent',
        { request_id: requestId, allow },
        session,
      );
      sessionStorage.removeItem('gym.oauth.request');
      location.assign(result.redirect_uri);
    } catch {
      setMessage('Подключение не подтверждено. Подтверди вход и повтори действие.');
      setBusy(false);
    }
  }
  return (
    <main className="oauth-consent card">
      <p className="eyebrow">Отдельное разрешение</p>
      <h1>Подключить агента к Gym</h1>
      {session && <p>Аккаунт: {session.yandex_login ?? 'Твой аккаунт Gym'}</p>}
      {details && (
        <>
          <h3>{details.client_name} запрашивает:</h3>
          <ul>
            {details.scopes.map((scope) => (
              <li key={scope}>{labels[scope]}</li>
            ))}
          </ul>
          <p className="caption">
            Доступ можно отозвать в Gym. Он действует до 90 дней, при отсутствии использования — до 30 дней.
            Изменять тренировочные факты агент не сможет. Каждую новую программу ты отдельно одобряешь в Gym.
          </p>
          <div className="account-actions">
            <button className="primary" disabled={busy} onClick={() => void decide(true)}>
              Разрешить доступ
            </button>
            <button className="secondary" disabled={busy} onClick={() => void decide(false)}>
              Отказать
            </button>
          </div>
        </>
      )}
      {message && <p role="status">{message}</p>}
      <a className="secondary" href="/auth/login?purpose=reauth&return_to=/settings">
        Подтвердить вход
      </a>
      <a className="secondary" href="#/app" onClick={() => sessionStorage.removeItem('gym.oauth.request')}>
        Вернуться к тренировкам
      </a>
    </main>
  );
}
