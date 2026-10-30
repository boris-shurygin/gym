import { expect, test, type BrowserContext, type Page, type APIRequestContext } from '@playwright/test';

const backend = 'http://127.0.0.1:4174';
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const clicks: object[] = [];
    Object.assign(window, { gymTestClicks: clicks });
    document.addEventListener(
      'click',
      (event) => {
        const target = event.target as Element;
        const button = target.closest('button');
        clicks.push({
          target: target.tagName,
          button: button?.getAttribute('aria-label') ?? button?.textContent?.trim(),
          editor: document.querySelector('.editor h2')?.textContent,
        });
      },
      true,
    );
  });
});
test.afterEach(async ({ page }, info) => {
  if (info.status === info.expectedStatus) return;
  // Только синтетический localhost-стенд; не печатай cookies/CSRF или сессионные ответы.
  const screen = await page
    .locator('body')
    .innerText({ timeout: 1000 })
    .catch(() => 'page unavailable');
  console.log('P08_FAILURE_SCREEN', JSON.stringify(screen.slice(0, 6000)));
  console.log(
    'P08_FAILURE_CLICKS',
    JSON.stringify(
      await page
        .evaluate(() => (window as unknown as { gymTestClicks: object[] }).gymTestClicks)
        .catch(() => []),
    ),
  );
  const state = await page
    .evaluate(
      async () =>
        await new Promise<object>((resolve) => {
          const request = indexedDB.open('gym-local-v1');
          request.onerror = () => resolve({ database: 'unavailable' });
          request.onsuccess = () => {
            const db = request.result,
              tx = db.transaction(['sessions', 'outbox']);
            const sessions = tx.objectStore('sessions').getAll(),
              outbox = tx.objectStore('outbox').getAll();
            tx.oncomplete = () => {
              resolve({
                sessions: sessions.result.map((s) => ({
                  id: s.id,
                  revision: s.revision,
                  serverRevision: s.serverRevision,
                  status: s.status,
                  sets: s.sets.map((set: { id: string; deletedAt?: string }) => ({
                    id: set.id,
                    deleted: Boolean(set.deletedAt),
                  })),
                })),
                queue: outbox.result.map((op) => ({
                  id: op.id,
                  status: op.status,
                  command: op.wire?.command.type,
                })),
              });
              db.close();
            };
            tx.onabort = () => {
              db.close();
              resolve({ database: 'aborted' });
            };
          };
        }),
    )
    .catch(() => ({ database: 'page unavailable' }));
  console.log('P08_FAILURE_STATE', JSON.stringify(state));
});
interface Connection {
  fixture: string;
}
async function fixture(request: APIRequestContext): Promise<Connection> {
  const response = await request.post(`${backend}/__test/fixture`);
  expect(response.ok()).toBe(true);
  return { fixture: (await response.json()).fixture };
}
async function connect(context: BrowserContext, connection: Connection) {
  await context.addCookies([
    { name: 'gym_e2e_fixture', value: connection.fixture, url: 'http://127.0.0.1:4173' },
  ]);
}
async function status(
  request: APIRequestContext,
  connection: Connection,
): Promise<{ posts: string[]; dropped: number }> {
  return (
    await request.get(`${backend}/__test/status`, { headers: { 'X-Test-Fixture': connection.fixture } })
  ).json();
}
async function enter(page: Page) {
  await page.goto('/');
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await expect(page.locator('.template-card').first()).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Синхронизация' }).getByText('Связь с сервером проверена'),
  ).toBeVisible();
}
async function start(page: Page) {
  await page.locator('.template-card').first().click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
}
async function record(page: Page, weight: string, reps = '9') {
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill(weight);
  await page.getByRole('textbox', { name: 'Повторения' }).fill(reps);
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
}
async function edit(page: Page, weight: string) {
  await page.getByRole('button', { name: 'Редактировать подход 1', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Исправление подхода', exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill(weight);
  await page.getByRole('button', { name: 'Сохранить изменения', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    weight.replace('.', ','),
  );
}
async function synced(page: Page) {
  await expect(page.getByRole('region', { name: 'Синхронизация' })).toContainText(
    '0 действий ожидают доставки',
    { timeout: 15_000 },
  );
}
async function allWorkouts(request: APIRequestContext, connection: Connection) {
  const response = await request.get(`${backend}/api/v1/workouts`, {
    headers: { 'X-Test-Fixture': connection.fixture },
  });
  expect(response.ok()).toBe(true);
  return (await response.json()).items as {
    workout_id: string;
    status: string;
    server_revision: number;
    sets: { set_id: string; weight_g: number; reps: number; deleted_at: string | null }[];
  }[];
}

test('настоящий API: офлайн старт, исправление, завершение, потерянный ответ и второе устройство', async ({
  page,
  context,
  request,
  browser,
}) => {
  const connection = await fixture(request);
  await connect(context, connection);
  await enter(page);
  await context.setOffline(true);
  await start(page);
  await record(page, '70,125');
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '70,125 кг × 9',
  );
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await edit(page, '65,125');
  await page.getByRole('button', { name: 'Разминка', exact: true }).click();
  await record(page, '20', '6');
  await page.getByRole('button', { name: 'К упражнениям', exact: false }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Завершить тренировку', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'История тренировок' })).toBeVisible();
  expect((await status(request, connection)).posts).toHaveLength(0);
  await request.post(`${backend}/__test/config`, {
    headers: { 'X-Test-Fixture': connection.fixture },
    data: { drop_next: true },
  });
  await context.setOffline(false);
  await expect.poll(async () => (await status(request, connection)).dropped).toBe(1);
  await page.getByRole('button', { name: 'Синхронизировать', exact: true }).click();
  await synced(page);
  const workouts = await allWorkouts(request, connection);
  expect(workouts).toHaveLength(1);
  expect(workouts[0].status).toBe('completed');
  expect(workouts[0].sets.map((s) => [s.weight_g, s.reps])).toEqual([
    [65125, 9],
    [20000, 6],
  ]);
  const posted = (await status(request, connection)).posts;
  expect(new Set(posted.map((body) => JSON.parse(body).operation_id)).size).toBe(5);
  expect(posted).toHaveLength(5); // The lost response is recovered by GET receipt.
  const other = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'allow' });
  try {
    await connect(other, connection);
    const second = await other.newPage();
    await second.goto('/');
    await second.getByRole('button', { name: 'История', exact: false }).click();
    await second.locator('.history-card').first().click();
    await second.getByRole('button', { name: /Жим лёжа/ }).click();
    await expect(second.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
      '65,125 кг × 9',
    );
  } finally {
    await other.close();
  }
  const before = (await status(request, connection)).posts.length;
  await page.reload();
  await synced(page);
  expect((await status(request, connection)).posts).toHaveLength(before);
});

for (const choice of ['local', 'server'] as const)
  test(`конфликт двух устройств: явный выбор ${choice} для всей цепочки`, async ({
    page,
    context,
    request,
    browser,
  }) => {
    const connection = await fixture(request);
    await connect(context, connection);
    await enter(page);
    await start(page);
    await record(page, '60');
    await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
      '60 кг × 9',
    );
    await synced(page);
    const other = await browser.newContext({
      serviceWorkers: 'allow',
      viewport: { width: 390, height: 844 },
    });
    try {
      const secondConnection = connection;
      await connect(other, secondConnection);
      const second = await other.newPage();
      await second.goto('/');
      await second.getByRole('button', { name: 'Продолжить тренировку' }).click();
      await second.getByRole('button', { name: /Жим лёжа/ }).click();
      await context.setOffline(true);
      await edit(page, '75');
      await record(page, '65', '8');
      await page.getByRole('button', { name: 'К упражнениям', exact: false }).click();
      page.once('dialog', (dialog) => void dialog.accept());
      await page.getByRole('button', { name: 'Завершить тренировку', exact: true }).click();
      await edit(second, '80');
      await synced(second);
      await page.bringToFront();
      await context.setOffline(false);
      await page.getByRole('button', { name: 'Сравнить версии', exact: true }).click();
      const review = page.getByRole('region', { name: 'Разбор конфликта' });
      await expect(review).toContainText('3 действий');
      await expect(review).toContainText('80 кг');
      await expect(review).toContainText('75 кг');
      page.once('dialog', (dialog) => void dialog.accept());
      await review
        .getByRole('button', {
          name: choice === 'local' ? 'Применить мои правки' : 'Оставить серверную версию',
          exact: true,
        })
        .click();
      await synced(page);
      const workouts = await allWorkouts(request, connection);
      expect(workouts).toHaveLength(1);
      expect(workouts[0].sets[0].weight_g).toBe(choice === 'local' ? 75000 : 80000);
      expect(workouts[0].status).toBe(choice === 'local' ? 'completed' : 'active');
      expect(workouts[0].sets).toHaveLength(choice === 'local' ? 2 : 1);
      const states = await page.evaluate(
        async () =>
          await new Promise<string[]>((resolve) => {
            const request = indexedDB.open('gym-local-v1');
            request.onsuccess = () => {
              const db = request.result;
              const read = db.transaction('outbox').objectStore('outbox').index('ownerId').getAll('alice');
              read.onsuccess = () => {
                resolve(read.result.map((op: { status: string }) => op.status));
                db.close();
              };
            };
          }),
      );
      expect(states.filter((state) => state === 'superseded')).toHaveLength(3);
    } finally {
      await other.close();
    }
  });

test('невалидные десятичные значения сохраняют ввод и не создают операцию', async ({
  page,
  context,
  request,
}) => {
  const connection = await fixture(request);
  await connect(context, connection);
  await enter(page);
  await start(page);
  await synced(page);
  const count = (await status(request, connection)).posts.length;
  await record(page, '62,1234');
  await expect(page.getByRole('alert')).toContainText('трёх знаков');
  await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('62,1234');
  expect((await status(request, connection)).posts).toHaveLength(count);
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toHaveCount(0);
});

test('удаление на другом устройстве сохраняет незаписанный ввод и не превращает исправление в добавление', async ({
  page,
  context,
  request,
  browser,
}) => {
  const connection = await fixture(request);
  await connect(context, connection);
  await enter(page);
  await start(page);
  await record(page, '60');
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toBeVisible();
  await synced(page);
  const other = await browser.newContext({ serviceWorkers: 'allow' });
  try {
    await connect(other, connection);
    const second = await other.newPage();
    await second.goto('/');
    await second.getByRole('button', { name: 'Продолжить тренировку' }).click();
    await second.getByRole('button', { name: /Жим лёжа/ }).click();
    await page.getByRole('button', { name: 'Редактировать подход 1', exact: true }).click();
    await page.getByRole('textbox', { name: 'Вес, кг' }).fill('72');
    await second.getByRole('button', { name: 'Редактировать подход 1', exact: true }).click();
    second.once('dialog', (dialog) => void dialog.accept());
    await second.getByRole('button', { name: 'Удалить подход', exact: true }).click();
    await expect(second.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toHaveCount(0);
    await synced(second);
    await page.bringToFront();
    await page.getByRole('button', { name: 'Синхронизировать', exact: true }).click();
    await expect(page.getByText('Этот подход удалён на другом устройстве.', { exact: false })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('72');
    await expect(page.getByRole('button', { name: 'Записать подход', exact: true })).toBeDisabled();
    expect((await status(request, connection)).posts).toHaveLength(3);
    page.once('dialog', (dialog) => void dialog.accept());
    await page.getByRole('button', { name: 'Сбросить ввод удалённого подхода' }).click();
    await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('60');
  } finally {
    await other.close();
  }
});
