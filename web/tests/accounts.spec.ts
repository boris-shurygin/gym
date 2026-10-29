import { expect, test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

const examples = JSON.parse(
  readFileSync(new URL('../../contracts/examples.json', import.meta.url), 'utf8'),
).examples;
const version = examples.find((e: { operation: string }) => e.operation === 'get_program').output.version;

async function server(page: Page, owner: { value: string; signedIn: boolean }) {
  await page.route('**/auth/session', async (route) => {
    await route.fulfill({
      status: owner.signedIn ? 200 : 401,
      json: owner.signedIn
        ? {
            user_id: owner.value,
            csrf_token: 'c'.repeat(64),
            expires_at: '2026-12-01T12:00:00Z',
            analytics_timezone: 'UTC',
          }
        : { error: { code: 'session_required' } },
    });
  });
  await page.route('**/api/v1/program', async (route) => {
    await route.fulfill({
      json: {
        owner_id: owner.value,
        version: {
          ...version,
          owner_id: owner.value,
          content: {
            ...version.content,
            templates: version.content.templates.map((t: object) => ({ ...t, name: `План ${owner.value}` })),
          },
        },
      },
    });
  });
}

test('подтверждённый план и локальная тренировка работают после истечения входа без сети', async ({
  page,
  context,
}) => {
  const owner = { value: 'alice', signedIn: true };
  await server(page, owner);
  await page.goto('/');
  await page.getByRole('button', { name: /План alice/ }).click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  await page.getByRole('button', { name: /Жим лёжа/ }).click();
  await page.getByRole('textbox', { name: 'Вес, кг' }).fill('70');
  await page.getByRole('textbox', { name: 'Повторения' }).fill('9');
  await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  // Закрывай сетевые моки тоже: route.fulfill сам не зависит от offline.
  owner.signedIn = false;
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
  await server(page, owner);
  await page.goto('/');
  await page.getByRole('button', { name: /План alice/ }).click();
  await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
  owner.value = 'bob';
  await page.reload();
  await expect(page.getByText('Выполнен вход в другой аккаунт.', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Открыть отдельный профиль' }).click();
  await expect(page.getByRole('button', { name: /План bob/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Скрыть данные на устройстве' }).click();
  await page.reload();
  await expect(page.getByRole('button', { name: /План bob/ })).toHaveCount(0);
  await page.evaluate(() => {
    localStorage.removeItem('gym.hidden');
    localStorage.setItem('gym.active-owner', 'alice');
  });
  owner.value = 'alice';
  await page.reload();
  await expect(page.getByRole('button', { name: 'Продолжить тренировку' })).toBeVisible();
});

test('service worker не перехватывает auth-навигацию и не кэширует session', async ({ page }) => {
  const owner = { value: 'alice', signedIn: true };
  await server(page, owner);
  await page.goto('/');
  await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
  await page.route('**/auth/login**', (route) =>
    route.fulfill({ status: 200, contentType: 'text/plain', body: 'provider-redirect-probe' }),
  );
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
