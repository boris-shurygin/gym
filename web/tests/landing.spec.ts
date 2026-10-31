import { expect, test } from '@playwright/test';

test('публичный лендинг не загружает историю, установка имеет ручной путь', async ({ page }) => {
  const privateRequests: string[] = [];
  await page.route('**/auth/session', (route) =>
    route.fulfill({ status: 401, json: { error: { code: 'session_required' } } }),
  );
  page.on('request', (request) => {
    if (/\/api\/v1\/(stats|workouts)/.test(request.url())) privateRequests.push(request.url());
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /Меньше записей.*Больше движения/ })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Личная статистика' })).toHaveCount(0);
  await page.getByRole('link', { name: 'Установить PWA', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'iPhone / iPad' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Android', exact: true })).toBeVisible();
  expect(privateRequests).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('согласие Python-агента отдельно от публикации и после входа; доступ можно отозвать', async ({
  page,
  request,
}) => {
  const backend = 'http://127.0.0.1:4174';
  const fixture = (await (await request.post(backend + '/__test/fixture')).json()).fixture;
  const headers = { 'X-Test-Fixture': fixture };
  await page
    .context()
    .addCookies([{ name: 'gym_e2e_fixture', value: fixture, url: 'http://127.0.0.1:4173' }]);
  const authorization = await (await request.post(backend + '/__test/oauth-request', { headers })).json();
  await page.route('**/oauth/consent', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch({ maxRedirects: 0 });
    expect(response.status()).toBe(302);
    const callback = new URL(response.headers().location);
    expect(callback.origin + callback.pathname).toBe('http://127.0.0.1:8765/callback');
    // WebKit не перехватывает следующий URL redirect-цепочки: подмени только
    // конечную страницу callback, оставив отправку формы и проверку Location настоящими.
    await route.fulfill({ contentType: 'text/html', body: '<h1>OAuth callback received</h1>' });
  });
  await page.goto(authorization.location);
  await expect(page.getByRole('heading', { name: 'Подключить агента к Gym', exact: true })).toBeVisible();
  await expect(page.getByText('Читать историю тренировок', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Разрешить доступ', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'OAuth callback received' })).toBeVisible();
  await page.goto('/');
  await expect(page.getByRole('region', { name: 'Личная статистика' })).toBeVisible();
  await page.getByText('Доступ тренировочного агента', { exact: true }).click();
  page.once('dialog', (dialog) => void dialog.accept());
  await page.getByRole('button', { name: 'Отозвать доступ', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Доступ отозван', exact: true })).toBeDisabled();
  const status = await (await request.get(backend + '/__test/status', { headers })).json();
  expect(status.publications).toBe(0);
});
