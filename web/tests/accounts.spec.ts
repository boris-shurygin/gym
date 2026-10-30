import { expect, test, type Page } from '@playwright/test';

async function server(page: Page, owner: { value: string; signedIn: boolean }) {
  const backend = 'http://127.0.0.1:4174';
  const response = await page.request.post(`${backend}/__test/fixture`);
  const fixture = (await response.json()).fixture;
  await page
    .context()
    .addCookies([{ name: 'gym_e2e_fixture', value: fixture, url: 'http://127.0.0.1:4173' }]);
  const configure = async (extra: object = {}) => {
    const response = await page.request.post(`${backend}/__test/config`, {
      headers: { 'X-Test-Fixture': fixture },
      data: { user: owner.value, signed_in: owner.signedIn, ...extra },
    });
    expect(response.ok()).toBe(true);
  };
  await configure();
  return configure;
}

test('staging: две тестовые тренировки публикуются в пустой аккаунт и не заменяются при повторе', async ({
  page,
}) => {
  const configure = await server(page, { value: 'alice', signedIn: true });
  await configure({ staging: true, empty_program: true });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'login-101', exact: true })).toBeVisible();
  const examples = page.getByRole('region', { name: 'Тестовые шаблоны staging' });
  await examples.getByRole('button', { name: 'Добавить тестовую программу' }).click();
  await expect(examples).not.toBeVisible();
  await expect(page.locator('.template-card')).toHaveCount(2);
  const first = await (await page.request.get('/api/v1/program')).json();
  expect(first.version.content.templates).toHaveLength(2);
  expect(
    first.version.content.templates.every((t: { exercises: unknown[] }) => t.exercises.length === 3),
  ).toBe(true);
  await page.reload();
  await expect(page.locator('.template-card')).toHaveCount(2);
  await expect(examples).not.toBeVisible();
  expect((await (await page.request.get('/api/v1/program')).json()).version.program_version_id).toBe(
    first.version.program_version_id,
  );
});

test('тестовые шаблоны скрыты вне staging; потерянный ответ публикации не создаёт вторую версию', async ({
  page,
}) => {
  const configure = await server(page, { value: 'alice', signedIn: true });
  await configure({ empty_program: true });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'login-101', exact: true })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Тестовые шаблоны staging' })).not.toBeVisible();
  await configure({ staging: true, drop_publish: true });
  await page.reload();
  const button = page.getByRole('button', { name: 'Добавить тестовую программу' });
  await button.click();
  await expect(
    page.getByRole('status').filter({ hasText: 'Добавление программы не подтверждено' }),
  ).toBeVisible();
  const version = (await (await page.request.get('/api/v1/program')).json()).version.program_version_id;
  await button.click();
  await expect(button).not.toBeVisible();
  expect((await (await page.request.get('/api/v1/program')).json()).version.program_version_id).toBe(version);
  expect((await (await page.request.get('http://127.0.0.1:4174/__test/status')).json()).publications).toBe(1);
});

test('подтверждённый план и локальная тренировка работают после истечения входа без сети', async ({
  page,
  context,
}) => {
  const owner = { value: 'alice', signedIn: true };
  const configure = await server(page, owner);
  await page.goto('/');
  await page.locator('.template-card').first().click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('70');
  await page.getByRole('textbox', { name: 'Повторения' }).fill('9');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '70 кг × 9',
  );
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  // Закрывай сетевые моки тоже: route.fulfill сам не зависит от offline.
  owner.signedIn = false;
  await configure();
  await context.setOffline(true);
  await page.reload();
  await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
    '70 кг × 9',
  );
  // Панель аккаунта скрыта в редакторе, чтобы не перекрывать запись подхода.
  await page.getByRole('button', { name: 'К упражнениям', exact: false }).click();
  await expect(page.getByRole('link', { name: 'Войти через Яндекс ID' })).toBeVisible();
  const cached = await page.evaluate(async () => {
    return await new Promise<string>((resolve) => {
      const request = indexedDB.open('gym-profiles-v1');
      request.onsuccess = () => {
        const read = request.result.transaction('profiles').objectStore('profiles').get('alice');
        read.onsuccess = () => {
          resolve(JSON.stringify(read.result));
          request.result.close();
        };
      };
    });
  });
  expect(cached).not.toContain('csrf_token');
  expect(cached).not.toContain('expires_at');
});

test('смена аккаунта закрывает старый профиль, а скрытие сохраняет его очередь', async ({ page }) => {
  const owner = { value: 'alice', signedIn: true };
  const configure = await server(page, owner);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'login-101', exact: true })).toBeVisible();
  await page.locator('.template-card').first().click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  // Дождись фиксации занятия в IndexedDB до смены аккаунта и перезагрузки.
  await expect(page.getByRole('button', { name: /Жим лёжа/ })).toBeVisible();
  owner.value = 'bob';
  await configure();
  await page.reload();
  await expect(page.getByText('Выполнен вход в другой аккаунт.', { exact: false })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'login-102', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Открыть отдельный профиль' }).click();
  await expect(page.locator('.template-card')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'login-102', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toHaveCount(0);
  await page.getByText('Управление аккаунтом', { exact: true }).click();
  await page.getByRole('button', { name: 'Скрыть данные на устройстве' }).click();
  await page.reload();
  await expect(page.locator('.template-card')).toHaveCount(0);
  await page.evaluate(() => {
    localStorage.removeItem('gym.hidden');
    localStorage.setItem('gym.active-owner', 'alice');
  });
  owner.value = 'alice';
  await configure();
  await page.reload();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toBeVisible();
});

test('service worker не перехватывает auth-навигацию и не кэширует session', async ({ page }) => {
  const owner = { value: 'alice', signedIn: true };
  const configure = await server(page, owner);
  await page.goto('/');
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await configure({ login_probe: true });
  await page.goto('/auth/login?return_to=/app');
  await expect(page.locator('body')).toContainText('provider-redirect-probe');
  await page.goto('/');
  const keys = await page.evaluate(async () => {
    const names = await caches.keys();
    return (
      await Promise.all(
        names.map(async (name) =>
          (await (await caches.open(name)).keys()).map((r) => new URL(r.url).pathname),
        ),
      )
    ).flat();
  });
  expect(keys.some((key) => key.startsWith('/auth/'))).toBe(false);
});

test('прежняя сессия без логина сохраняет профиль и предлагает подтверждение аккаунта', async ({ page }) => {
  const configure = await server(page, { value: 'alice', signedIn: true });
  await configure({ legacy: true });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Твой аккаунт', exact: true })).toBeVisible();
  await expect(page.locator('.template-card').first()).toBeVisible();
  await expect(page.getByRole('link', { name: 'Подтвердить аккаунт', exact: true })).toHaveAttribute(
    'href',
    '/auth/login?purpose=reauth&return_to=/app',
  );
});
