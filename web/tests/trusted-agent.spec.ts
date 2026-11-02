import { expect, test } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { stagingProgram } from '../src/staging-program';

test('разовое согласие Python-агенту разрешает публикацию через MCP и отзыв немедленно закрывает доступ', async ({
  page,
  context,
  request,
}) => {
  const backend = 'http://127.0.0.1:4174';
  const fixture = (await (await request.post(`${backend}/__test/fixture`)).json()).fixture;
  const headers = { 'X-Test-Fixture': fixture };
  await context.addCookies([{ name: 'gym_e2e_fixture', value: fixture, url: 'http://127.0.0.1:4173' }]);
  const authorization = await (
    await request.post(`${backend}/__test/oauth-request`, {
      headers,
      data: { direct_publication: true },
    })
  ).json();
  let code = '';
  await page.route('**/oauth/consent', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch({ maxRedirects: 0 });
    expect(response.status()).toBe(302);
    const callback = new URL(response.headers().location);
    expect(callback.origin + callback.pathname).toBe('http://127.0.0.1:8765/callback');
    code = callback.searchParams.get('code')!;
    await route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: '<!doctype html><meta charset="utf-8"><h1>Согласие сохранено</h1>',
    });
  });
  await page.goto(authorization.location);
  await expect(
    page.getByText(
      'Публиковать программу по твоему запросу через Python-агента без отдельного одобрения в Gym',
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText(/Ты доверяешь этому агенту/)).toBeVisible();
  await page.getByRole('button', { name: 'Разрешить доступ', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Согласие сохранено' })).toBeVisible();
  const exchange = await request.post(`${backend}/oauth/token`, {
    headers,
    form: {
      grant_type: 'authorization_code',
      client_id: 'gym-python',
      code,
      code_verifier: 'v'.repeat(43),
      redirect_uri: 'http://127.0.0.1:8765/callback',
      resource: 'https://gym.example/mcp',
    },
  });
  expect(exchange.ok()).toBe(true);
  const token = await exchange.json();
  expect(token.scope.split(' ')).toContain('program:publish_direct');
  const mcpHeaders = {
    ...headers,
    Authorization: `Bearer ${token.access_token}`,
    Accept: 'application/json, text/event-stream',
    'MCP-Protocol-Version': '2025-11-25',
    'X-Gym-Schema-Version': '2',
  };
  let id = 0;
  const call = async (name: string, args: object = {}) => {
    const response = await request.post(`${backend}/mcp`, {
      headers: mcpHeaders,
      data: { jsonrpc: '2.0', id: ++id, method: 'tools/call', params: { name, arguments: args } },
    });
    expect(response.ok()).toBe(true);
    const result = (await response.json()).result.structuredContent;
    expect(result.ok).toBe(true);
    return result.data;
  };
  const current = await call('get_program');
  const change = await call('propose_program_changes', {
    request_id: randomUUID(),
    expected_owner_id: current.owner_id,
    proposal: {
      base_program_version_id: current.version.program_version_id,
      content: stagingProgram,
      rationale: 'Явный синтетический запрос владельца доверенному агенту',
    },
  });
  await call('validate_program_changes', {
    change_id: change.change_id,
    expected_revision: change.change_revision,
  });
  await request.post(`${backend}/__test/config`, { headers, data: { old_auth: true } });
  const publication = await call('publish_program_changes', {
    request_id: randomUUID(),
    expected_owner_id: current.owner_id,
    change_id: change.change_id,
    expected_revision: change.change_revision,
    base_program_version_id: change.proposal.base_program_version_id,
    content_digest: change.content_digest,
  });
  expect(publication.authorization.kind).toBe('delegated_agent');
  expect(publication.approval_id).toBeUndefined();
  await page.goto('/#/app');
  await expect(page.locator('.template-card')).toHaveCount(2);
  expect((await call('get_program')).version.program_version_id).toBe(
    publication.program_version.program_version_id,
  );
  await page.goto('/');
  await page.getByText('Доступ тренировочного агента', { exact: true }).click();
  const access = page.locator('.agent-access');
  await expect(access).toContainText(
    'Публиковать программу по твоему запросу без отдельного одобрения в Gym',
  );
  page.once('dialog', (dialog) => void dialog.accept());
  await access.getByRole('button', { name: 'Отозвать доступ', exact: true }).click();
  await expect(access.getByRole('button', { name: 'Доступ отозван', exact: true })).toBeDisabled();
  const revoked = await request.post(`${backend}/mcp`, {
    headers: mcpHeaders,
    data: { jsonrpc: '2.0', id: ++id, method: 'tools/call', params: { name: 'get_program', arguments: {} } },
  });
  expect(revoked.status()).toBe(401);
});
