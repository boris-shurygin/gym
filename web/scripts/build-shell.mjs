import { readdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

async function files(directory, prefix = '') {
  const output = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.isDirectory())
      output.push(...(await files(join(directory, entry.name), `${prefix}${entry.name}/`)));
    else if (entry.name !== 'service-worker.js') output.push(`${prefix}${entry.name}`);
  }
  return output.sort();
}
const assets = await files('dist');
const hash = createHash('sha256');
for (const file of assets) hash.update(await readFile(join('dist', file)));
const cacheName = `gym-shell-${hash.digest('hex').slice(0, 16)}`;
const urls = assets.map((file) => `/${file}`);
await writeFile(
  'dist/service-worker.js',
  `
const CACHE = ${JSON.stringify(cacheName)};
const ASSETS = ${JSON.stringify(urls)};
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});
// Do not force activation/reload or delete old assets and IndexedDB during a workout.
self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin ||
      ['/api/', '/auth/', '/oauth/', '/.well-known/'].some(prefix => url.pathname.startsWith(prefix)) || url.pathname === '/mcp') return;
  if (event.request.mode === 'navigate') {
    event.respondWith(caches.open(CACHE).then(cache => cache.match('/index.html')).then(cached => cached || fetch(event.request)));
  } else if (ASSETS.includes(url.pathname)) {
    // Only the explicit public shell list; API responses and user data never enter this cache.
    // Precache and browser module requests can have different Origin/Vary headers.
    event.respondWith(caches.open(CACHE).then(cache => cache.match(url.pathname, { ignoreVary: true })).then(cached => cached || fetch(event.request)));
  }
});
`,
);
console.log(`Offline shell: ${assets.length} assets (${cacheName})`);
