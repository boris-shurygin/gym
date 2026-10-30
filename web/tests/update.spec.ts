import { expect, test } from '@playwright/test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

test('установленная PWA: waiting, незаписанный ввод, другая вкладка и явное безопасное обновление', async ({
  page,
  context,
}) => {
  const root = resolve('dist');
  let release = 'a';
  const worker = await readFile(resolve(root, 'service-worker.js'), 'utf8');
  const server = createServer(async (request, response) => {
    const path = new URL(request.url!, 'http://localhost').pathname;
    response.setHeader('Cache-Control', 'no-store');
    if (path.startsWith('/auth/') || path.startsWith('/api/')) {
      response.writeHead(401, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ error: { code: 'session_required' } }));
      return;
    }
    if (path === '/service-worker.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      response.end(worker.replace(/const CACHE = [^;]+;/, `const CACHE = "gym-update-test-${release}";`));
      return;
    }
    const file = resolve(root, path === '/' || path === '/app' ? 'index.html' : path.slice(1));
    if (!file.startsWith(root + '/') && !file.startsWith(root + '\\')) {
      response.writeHead(404);
      response.end();
      return;
    }
    try {
      let bytes: Buffer | string = await readFile(file);
      if (file.endsWith('index.html'))
        bytes = bytes
          .toString()
          .replace('</head>', `<script>window.__release = "${release}";</script></head>`);
      response.writeHead(200, {
        'Content-Type':
          (
            {
              '.html': 'text/html',
              '.js': 'application/javascript',
              '.css': 'text/css',
              '.svg': 'image/svg+xml',
              '.webmanifest': 'application/manifest+json',
            } as Record<string, string>
          )[extname(file)] ?? 'application/octet-stream',
      });
      response.end(bytes);
    } catch {
      response.writeHead(404);
      response.end();
    }
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as { port: number }).port;
  const origin = `http://127.0.0.1:${port}`;
  try {
    await page.addInitScript(() => localStorage.setItem('gym.mode', 'demo'));
    await context.addCookies([{ name: 'synthetic_preservation_probe', value: 'kept', url: origin }]);
    await page.goto(origin);
    await expect(page.getByText('Можно открыть без сети', { exact: true })).toBeVisible();
    await page.locator('.template-card').first().click();
    await page.getByRole('button', { name: 'Начать тренировку', exact: true }).click();
    await page.getByRole('button', { name: /Жим лёжа/ }).click();
    await page.getByRole('textbox', { name: 'Вес, кг' }).fill('70');
    await page.getByRole('textbox', { name: 'Повторения' }).fill('9');
    await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
    await page.getByRole('textbox', { name: 'Вес, кг' }).fill('82,5');
    const other = await context.newPage();
    await other.goto(origin);
    release = 'b';
    await page.evaluate(async () => {
      await (await navigator.serviceWorker.getRegistration())!.update();
    });
    await expect(page.getByRole('button', { name: 'Обновить приложение', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Обновить приложение', exact: true }).click();
    await expect(
      page.getByText('Сначала запиши подход или сбрось незаписанный ввод.', { exact: false }),
    ).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Вес, кг' })).toHaveValue('82,5');
    expect(await page.evaluate(() => (window as unknown as { __release: string }).__release)).toBe('a');
    await page.getByRole('button', { name: 'Записать подход', exact: true }).click();
    await page.getByRole('button', { name: 'Обновить приложение', exact: true }).click();
    await expect(page.getByText('Закрой остальные вкладки и окна Gym', { exact: false })).toBeVisible();
    await other.close();
    await page.getByRole('button', { name: 'Обновить приложение', exact: true }).click();
    await expect
      .poll(() =>
        page.evaluate(() => (window as unknown as { __release: string }).__release).catch(() => undefined),
      )
      .toBe('b');
    await page.getByRole('button', { name: 'Продолжить тренировку' }).click();
    await page.getByRole('button', { name: /Жим лёжа/ }).click();
    await expect(page.getByRole('button', { name: 'Редактировать подход 1', exact: true })).toContainText(
      '70 кг × 9',
    );
    await expect(page.getByRole('button', { name: 'Редактировать подход 2', exact: true })).toContainText(
      '82,5',
    );
    expect(
      (await context.cookies(origin)).some(
        (c) => c.name === 'synthetic_preservation_probe' && c.value === 'kept',
      ),
    ).toBe(true);
    const count = await page.evaluate(
      async () =>
        await new Promise<number>((resolve) => {
          const request = indexedDB.open('gym-local-v1');
          request.onsuccess = () => {
            const db = request.result;
            const read = db.transaction('outbox').objectStore('outbox').count();
            read.onsuccess = () => {
              resolve(read.result);
              db.close();
            };
          };
        }),
    );
    expect(count).toBe(3);
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
      server.closeAllConnections();
    });
  }
});
