import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { stagingProgram } from '../src/staging-program';

test('отказ публикации виден у кнопки; после свежего входа открываются опубликованные тренировки', async ({
  page,
  context,
  request,
}) => {
  const backend = 'http://127.0.0.1:4174';
  const fixture = (await (await request.post(`${backend}/__test/fixture`)).json()).fixture;
  const headers = { 'X-Test-Fixture': fixture };
  await context.addCookies([{ name: 'gym_e2e_fixture', value: fixture, url: 'http://127.0.0.1:4173' }]);
  const session = await (await request.get(`${backend}/auth/session`, { headers })).json();
  const before = await (await request.get(`${backend}/api/v1/program`, { headers })).json();
  const response = await request.post(`${backend}/api/v1/program/changes`, {
    headers: { ...headers, 'X-CSRF-Token': session.csrf_token },
    data: {
      request_id: randomUUID(),
      expected_owner_id: session.user_id,
      proposal: {
        base_program_version_id: before.version.program_version_id,
        content: stagingProgram,
        rationale: 'Синтетическая проверка сообщения об истёкшем подтверждении входа',
      },
    },
  });
  expect(response.ok()).toBe(true);
  const change = await response.json();
  await request.post(`${backend}/__test/config`, { headers, data: { old_auth: true } });
  await page.goto(`/#/program?change_id=${change.change_id}`);
  const review = page.locator('.program-review');
  await review.getByRole('button', { name: 'Одобрить и опубликовать', exact: true }).click();
  const error = review.getByRole('alert');
  await expect(error).toContainText('за последние 10 минут');
  await expect(error).toBeInViewport();
  await expect(review.getByRole('link', { name: 'Подтвердить вход перед одобрением' })).toHaveAttribute(
    'href',
    '/auth/login?purpose=reauth&return_to=/program',
  );
  expect(
    (await (await request.get(`${backend}/api/v1/program`, { headers })).json()).version.program_version_id,
  ).toBe(before.version.program_version_id);
  // Only the local test fixture simulates the completed interactive reauthentication.
  await request.post(`${backend}/__test/config`, { headers, data: { old_auth: false } });
  await review.getByRole('button', { name: 'Одобрить и опубликовать', exact: true }).click();
  await expect(page).toHaveURL(/#\/app$/);
  await expect(review).not.toBeVisible();
  await expect(page.locator('.template-card')).toHaveCount(2);
  const published = await (await request.get(`${backend}/api/v1/program`, { headers })).json();
  expect(published.version.change_id).toBe(change.change_id);
});
