import { useEffect, useState } from 'react';

interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: string }>;
}

export default function Landing() {
  const [prompt, setPrompt] = useState<InstallPrompt>();
  const [installed, setInstalled] = useState(window.matchMedia('(display-mode: standalone)').matches);
  const [message, setMessage] = useState('');
  useEffect(() => {
    const ready = (event: Event) => {
      event.preventDefault();
      setPrompt(event as InstallPrompt);
    };
    const done = () => {
      setInstalled(true);
      setPrompt(undefined);
    };
    window.addEventListener('beforeinstallprompt', ready);
    window.addEventListener('appinstalled', done);
    return () => {
      window.removeEventListener('beforeinstallprompt', ready);
      window.removeEventListener('appinstalled', done);
    };
  }, []);
  return (
    <section className="landing">
      <header className="landing-nav">
        <a className="wordmark" href="/">
          gym<span>↗</span>
        </a>
        <a className="secondary" href="#/app">
          Открыть приложение
        </a>
      </header>
      <div className="landing-hero">
        <p className="eyebrow">Тренируйся в своём ритме</p>
        <h1>
          Меньше записей.
          <br />
          Больше движения.
        </h1>
        <p className="subheading">
          Твоя программа, история и прогресс — рядом. Записывай подходы без интернета, возвращайся к
          результатам и обсуждай следующий шаг с ИИ-тренером.
        </p>
        <div className="account-actions">
          <a className="primary" href="#/app">
            Начать тренировку
          </a>
          <a className="secondary" href="#installation">
            Установить PWA
          </a>
        </div>
      </div>
      <div className="landing-features">
        {[
          [
            '01',
            'План под рукой',
            'Вес, подходы и повторения. Программа хранится отдельно от фактических результатов.',
          ],
          ['02', 'Без сети', 'Записывай подходы в зале. Синхронизация продолжится, когда появится связь.'],
          [
            '03',
            'Понятный прогресс',
            'История, рабочие веса и объём упражнения. Изменения программы — после твоего одобрения.',
          ],
        ].map(([n, title, text]) => (
          <article className="card" key={n}>
            <span className="eyebrow">{n}</span>
            <h3>{title}</h3>
            <p>{text}</p>
          </article>
        ))}
      </div>
      <section id="installation" className="card installation">
        <p className="eyebrow">Всегда под рукой</p>
        <h2>Установи Gym</h2>
        <p>Это веб-приложение. Отдельный APK или приложение из магазина скачивать не нужно.</p>
        {installed ? (
          <p role="status">Gym уже открыт как установленное приложение.</p>
        ) : prompt ? (
          <button
            className="primary"
            onClick={() =>
              void (async () => {
                await prompt.prompt();
                const choice = await prompt.userChoice;
                setPrompt(undefined);
                setMessage(
                  choice.outcome === 'accepted'
                    ? 'Следуй подсказке браузера. Можно также открыть Gym по ссылке.'
                    : 'Можно установить позже или использовать Gym в браузере.',
                );
              })()
            }
          >
            Установить на устройство
          </button>
        ) : (
          <p className="caption">Если браузер не предлагает установку, используй инструкцию ниже.</p>
        )}
        {message && <p role="status">{message}</p>}
        <div className="install-columns">
          <div>
            <h3>iPhone / iPad</h3>
            <ol>
              <li>Открой Gym в Safari.</li>
              <li>Нажми «Поделиться».</li>
              <li>Выбери «На экран “Домой”» и подтверди добавление.</li>
            </ol>
          </div>
          <div>
            <h3>Android</h3>
            <ol>
              <li>Открой Gym в Chrome.</li>
              <li>В меню ⋮ выбери «Установить приложение» или «Добавить на главный экран».</li>
              <li>Подтверди установку.</li>
            </ol>
          </div>
        </div>
        <p className="caption">
          При первом открытии нужна сеть. После загрузки приложения тренировки можно записывать офлайн. На
          общем устройстве скрывай профиль после занятия.
        </p>
      </section>
      <p className="landing-private">
        Личная статистика доступна после входа. Сейчас Gym работает по приглашениям.
      </p>
    </section>
  );
}
