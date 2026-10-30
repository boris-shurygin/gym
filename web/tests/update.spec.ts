import { expect, test } from '@playwright/test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

test('мост обновляет старую оболочку без баннера и сохраняет данные перед новым входом', async ({
  page,
  context,
}) => {
  const root = resolve('dist');
  const worker = await readFile(resolve(root, 'service-worker.js'), 'utf8');
  const bridge = await readFile(resolve(root, 'update.html'));
  let legacy = true;
  const legacyHtml =
    '<!doctype html><meta charset="UTF-8"><h1>Прежняя версия Gym</h1><script>navigator.serviceWorker.register("/service-worker.js")</script>';
  const legacyWorker = `
    self.addEventListener('install', e => e.waitUntil(caches.open('gym-legacy-test').then(c => c.put('/index.html', new Response(${JSON.stringify(legacyHtml)}, {headers:{'Content-Type':'text/html'}})))));
    self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
    self.addEventListener('fetch', e => { if(e.request.mode==='navigate' && !new URL(e.request.url).pathname.startsWith('/auth/')) e.respondWith(caches.open('gym-legacy-test').then(c=>c.match('/index.html'))); });`;
  const server = createServer(async (request, response) => {
    const path = new URL(request.url!, 'http://localhost').pathname;
    response.setHeader('Cache-Control', 'no-store');
    if (path === '/service-worker.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      response.end(legacy ? legacyWorker : worker);
      return;
    }
    if (path === '/auth/update-app') {
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      response.end(bridge);
      return;
    }
    if (path === '/auth/login') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end('<meta charset="UTF-8"><h1>Подтверждение аккаунта</h1>');
      return;
    }
    if (path.startsWith('/auth/') || path.startsWith('/api/') || path.startsWith('/__gym/')) {
      response.writeHead(401, { 'Content-Type': 'application/json' });
      response.end('{"error":{"code":"session_required"}}');
      return;
    }
    if (legacy && (path === '/' || path === '/index.html')) {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      response.end(legacyHtml);
      return;
    }
    const file = resolve(root, path === '/' ? 'index.html' : path.slice(1));
    if (!file.startsWith(root + '/') && !file.startsWith(root + '\\')) {
      response.writeHead(404);
      response.end();
      return;
    }
    try {
      const bytes = await readFile(file);
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
  const origin = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
  try {
    await context.addCookies([{ name: 'synthetic_preservation_probe', value: 'kept', url: origin }]);
    await page.goto(origin);
    await page.evaluate(async () => {
      await navigator.serviceWorker.ready;
    });
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Прежняя версия Gym' })).toBeVisible();
    await page.evaluate(async () => {
      localStorage.setItem('gym.mode', 'demo');
      await new Promise<void>((resolve) => {
        const request = indexedDB.open('gym-legacy-preservation-probe', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('outbox');
        request.onsuccess = () => {
          const db = request.result;
          const tx = db.transaction('outbox', 'readwrite');
          tx.objectStore('outbox').put({ weight_g: 70125, reps: 9 }, 'pending');
          tx.oncomplete = () => {
            db.close();
            resolve();
          };
        };
      });
    });
    legacy = false;
    await page.goto(origin + '/auth/update-app');
    const update = page.getByRole('button', { name: 'Обновить и подтвердить аккаунт' });
    await expect(update).toBeEnabled();
    await update.click();
    await expect(page.getByRole('heading', { name: 'Подтверждение аккаунта' })).toBeVisible();
    expect(
      (await context.cookies(origin)).some(
        (c) => c.name === 'synthetic_preservation_probe' && c.value === 'kept',
      ),
    ).toBe(true);
    expect(
      await page.evaluate(
        async () =>
          await new Promise((resolve) => {
            const request = indexedDB.open('gym-legacy-preservation-probe');
            request.onsuccess = () => {
              const db = request.result;
              const read = db.transaction('outbox').objectStore('outbox').get('pending');
              read.onsuccess = () => {
                resolve(read.result);
                db.close();
              };
            };
          }),
      ),
    ).toEqual({ weight_g: 70125, reps: 9 });
    await page.goto(origin);
    await expect(page.getByRole('heading', { name: 'Демо-режим' })).toBeVisible();
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
      server.closeAllConnections();
    });
  }
});

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
