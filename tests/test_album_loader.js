const assert = require('node:assert/strict');
const {createThumbnailLoader} = require('../static/album_loader.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const image = id => ({dataset: {src: '/fake/' + id}, fetchPriority: id < 2 ? 'high' : 'low',
                     removeAttribute(key) {delete this[key];}, decode: async () => {}});

(async () => {
  let active = 0;
  let maxActive = 0;
  const calls = [];
  const pending = [];
  const revoked = [];
  const loader = createThumbnailLoader({
    fetcher: (url, options) => {
      active++; maxActive = Math.max(active, maxActive); calls.push({url, options});
      return new Promise(resolve => pending.push(ok => {
        active--; resolve({ok, status: ok ? 200 : 404, blob: async () => ({fake: true})});
      }));
    },
    urls: {createObjectURL: () => 'blob:fake-' + calls.length, revokeObjectURL: url => revoked.push(url)}
  });
  const images = Array.from({length: 1000}, (_, i) => image(i));
  images.slice(0, 2).forEach(img => loader.enqueue(img));
  assert.equal(calls.length, 2); // No requests for offscreen photos.
  images.slice(2, 8).forEach(img => loader.enqueue(img));
  assert.equal(calls.length, 2); // Visibility queue never bypasses the cap.
  loader.leave(images[2]); // User scrolls past a queued thumbnail before its request starts.
  loader.cancel(images[3]); // Moderated while waiting.
  pending.shift()(true); await tick();
  assert.equal(calls[2].url, '/fake/4');
  assert.ok(images[0].src.startsWith('blob:'));
  assert.equal(revoked.length, 1);
  pending.shift()(false); await tick();
  assert.match(images[1].alt, /Vista previa no disponible/);
  assert.equal(calls[3].url, '/fake/5'); // A failed image cannot stall the rest.
  loader.cancel(images[4]);
  pending.shift()(true); await tick();
  assert.equal(images[4].src, undefined); // Late fetch cannot revive a moderated thumbnail.
  while (pending.length) {pending.shift()(true); await tick();}
  assert.equal(maxActive, 2);
  assert.equal(calls.length, 6);
  assert.equal(calls[0].options.priority, 'high');
  assert.equal(calls[2].options.priority, 'low');
  for (const call of calls) assert.equal(call.options.cache, 'no-cache');
  loader.enqueue(images[0]);
  assert.equal(calls.length, 6); // Loaded images aren't fetched on every observer callback.
  loader.enqueue(images[2]); await tick();
  assert.equal(calls.at(-1).url, '/fake/2'); // Re-entering the viewport may load a skipped image.
  loader.destroy();pending.shift()(true);await tick();
  assert.equal(images[2].src, undefined);
  console.log('PASS: first-row priority, lazy viewport queue, max 2 requests, scroll cancellation, moderation race, errors, revalidation, blob cleanup');
})().catch(error => {console.error(error);process.exitCode = 1;});
