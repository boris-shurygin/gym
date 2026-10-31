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
        Выход из PWA не отзывает доступ агента. Отозвать его можно здесь; история останется.
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
              if (!window.confirm('Отозвать доступ этого подключения агента?')) return;
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
