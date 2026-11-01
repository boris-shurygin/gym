import { useEffect, useRef, useState } from 'react';
import { recordDiagnostic } from './telemetry';

export default function PwaUpdate() {
  const [registration, setRegistration] = useState<ServiceWorkerRegistration>();
  const [waiting, setWaiting] = useState(false);
  const [message, setMessage] = useState('');
  const requested = useRef(false);
  useEffect(() => {
    if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
    let active = true;
    let lastCheck = 0;
    let current: ServiceWorkerRegistration | undefined;
    const inspect = () => {
      if (active) setWaiting(Boolean(current?.waiting));
    };
    const check = () => {
      if (document.visibilityState !== 'visible' || Date.now() - lastCheck < 60_000) return;
      lastCheck = Date.now();
      void current?.update().catch(() => recordDiagnostic('page_error', 'update'));
      inspect();
    };
    const changed = () => {
      if (requested.current) location.reload();
    };
    const message = (event: MessageEvent) => {
      if (event.data?.type === 'UPDATE_BLOCKED') {
        requested.current = false;
        setMessage('Закрой остальные вкладки и окна Gym, затем снова нажми «Обновить приложение».');
      }
    };
    void navigator.serviceWorker
      .register('/service-worker.js', { updateViaCache: 'none' })
      .then((value) => {
        if (!active) return;
        current = value;
        setRegistration(value);
        inspect();
        value.addEventListener('updatefound', () =>
          value.installing?.addEventListener('statechange', inspect),
        );
        check();
      })
      .catch(() => recordDiagnostic('page_error', 'update'));
    navigator.serviceWorker.addEventListener('controllerchange', changed);
    navigator.serviceWorker.addEventListener('message', message);
    document.addEventListener('visibilitychange', check);
    window.addEventListener('online', check);
    return () => {
      active = false;
      navigator.serviceWorker.removeEventListener('controllerchange', changed);
      navigator.serviceWorker.removeEventListener('message', message);
      document.removeEventListener('visibilitychange', check);
      window.removeEventListener('online', check);
    };
  }, []);
  if (!waiting) return null;
  return (
    <section className="update-banner card" aria-label="Обновление приложения">
      <strong>Доступна новая версия Gym</strong>
      <p>Сохранённые тренировки и очередь останутся на устройстве. Обновление перезагрузит это окно.</p>
      {message && <p role="status">{message}</p>}
      <button
        className="secondary"
        onClick={() => {
          if (document.querySelector('[data-unsaved="true"], [data-saving="true"]')) {
            setMessage('Сначала запиши подход или сбрось незаписанный ввод. Затем обнови приложение.');
            return;
          }
          requested.current = true;
          registration?.waiting?.postMessage({ type: 'APPLY_UPDATE' });
        }}
      >
        Обновить приложение
      </button>
    </section>
  );
}
