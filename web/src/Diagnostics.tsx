import { useEffect, useMemo, useState } from 'react';
import type { SessionInfo } from './account';
import { TelemetryClient, recordDiagnostic, recentDiagnosticErrors } from './telemetry';

export default function Diagnostics({ owner, session }: { owner?: string; session?: SessionInfo }) {
  const client = useMemo(() => (owner ? new TelemetryClient(owner, session) : undefined), [owner, session]);
  const [enabled, setEnabled] = useState(false);
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (!owner || !client) return;
    const value = client;
    setEnabled(value.isEnabled());
    value.activate();
    recordDiagnostic('app_open');
    const error = () => recordDiagnostic('page_error', 'unexpected');
    const storage = (event: StorageEvent) => {
      if (event.key === `gym.diagnostics.consent:${owner}` && event.newValue !== 'v1') {
        value.setConsent(false);
        setEnabled(false);
        setCount(0);
      }
    };
    const send = () => {
      if (
        navigator.onLine &&
        document.visibilityState === 'visible' &&
        !document.querySelector('[data-saving="true"], [data-syncing="true"]')
      )
        void value.flush().finally(() => setCount(value.size()));
    };
    const timer = setInterval(send, 60000);
    window.addEventListener('error', error);
    window.addEventListener('unhandledrejection', error);
    window.addEventListener('storage', storage);
    // Online/foreground не обходят минутный лимит запросами на каждое действие.
    return () => {
      clearInterval(timer);
      value.stop();
      window.removeEventListener('error', error);
      window.removeEventListener('unhandledrejection', error);
      window.removeEventListener('storage', storage);
    };
  }, [owner, client]);
  return (
    <details className="card" aria-label="Диагностика и данные">
      <summary>Диагностика и данные</summary>
      <p>
        Gym хранит логин Яндекса, программу и историю тренировок в Yandex Cloud в России, а локальную копию —
        на этом устройстве. Доступ есть у тебя и у подключённых тобой агентов в пределах разрешённых прав.
        Владелец сервиса имеет технический доступ для поддержки. Программа и история хранятся до удаления
        аккаунта; для удаления обратись к владельцу Gym.
      </p>
      <p>
        Серверные технические журналы не содержат тренировок, логинов и тел запросов. Добровольная диагностика
        отправляет только фиксированные коды ошибок и счётчики; сообщения и стеки ошибок не отправляются.
        Журналы Monium хранятся 31 день, локальный диагностический буфер — до 24 часов. Резервные копии — до 7
        дней.
      </p>
      <label>
        <input
          type="checkbox"
          disabled={!client}
          checked={enabled}
          onChange={(e) => {
            client?.setConsent(e.target.checked);
            setEnabled(e.target.checked);
            setCount(client?.size() ?? 0);
            if (client && e.target.checked && navigator.onLine) {
              void client.flush().finally(() => setCount(client.size()));
            }
          }}
        />{' '}
        Разрешить отправку технической диагностики с этого устройства
      </label>
      <p className="caption">
        Можно отключить в любой момент: ожидающая диагностика удалится. Тренировки и синхронизация работают
        при любом выборе.
      </p>
      <p className="caption">
        Сборка: {import.meta.env.VITE_GYM_SOURCE_SHA?.slice(0, 12) ?? 'локальная'}. Диагностических событий в
        буфере: {count}. Сеть: {navigator.onLine ? 'доступна' : 'нет сети'}.
      </p>
      {recentDiagnosticErrors().map((e, i) => (
        <p className="caption" key={i}>
          {e.at}: {e.code}
        </p>
      ))}
    </details>
  );
}
