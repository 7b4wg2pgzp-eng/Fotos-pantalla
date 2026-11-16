// Run with: node tests/test_video_upload.js
const assert = require('node:assert/strict');
const upload = require('../static/video_upload.js');

const limits = [
  [{name: 'clip.avi', size: 10}, 5, upload.VIDEO_ERRORS.type],
  [{name: 'clip.mp4', size: upload.VIDEO_MAX_BYTES + 1}, 5, upload.VIDEO_ERRORS.size],
  [{name: 'clip.mp4', size: 10}, 61, upload.VIDEO_ERRORS.duration],
  [{name: 'clip.mp4', size: 10}, Number.NaN, upload.VIDEO_ERRORS.unreadable],
  [{name: 'clip.MOV', size: 10}, 60, null],
  [{name: 'clip.webm', size: upload.VIDEO_MAX_BYTES}, 1, null]
];
for (const [file, duration, expected] of limits) {
  assert.equal(upload.videoRejection(file, duration), expected);
}
assert.equal(upload.VIDEO_CHUNK % (256 * 1024), 0);
assert.equal(upload.chunkEnd(0, 10, 4), 3);
assert.equal(upload.chunkEnd(8, 10, 4), 9);
assert.equal(upload.resumeOffset('bytes=0-3', 0), 4);
assert.equal(upload.resumeOffset(null, 8), 8);

const bytes = Uint8Array.from([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
const file = {
  name: 'clip.mp4',
  size: bytes.length,
  slice: (start, end) => bytes.slice(start, end)
};
const fetches = [];
const puts = [];
const progress = [];
let retries = 0;
let putStep = 0;
const script = [
  {status: 0, range: null},
  {status: 308, range: 'bytes=0-3'},
  {status: 308, range: null},
  {status: 201, range: null}
];

async function fakeFetch(url, options) {
  fetches.push({url, body: options.body});
  if (url === '/api/videos/session') {
    return {ok: true, status: 200, json: async () => ({ok: true, uid: 'a'.repeat(32), uploadUrl: 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=test'})};
  }
  assert.equal(url, '/api/videos/register');
  return {ok: true, status: 200, json: async () => ({ok: true})};
}

async function fakePut(url, body, headers) {
  puts.push({url, body, headers});
  const next = script[putStep++];
  assert.ok(next, 'sobran intentos de subida');
  return next;
}

(async () => {
  await upload.uploadVideoFile(file, 8, {
    chunkSize: 4,
    fetch: fakeFetch,
    put: fakePut,
    sleep: async () => {},
    onProgress: (sent) => progress.push(sent),
    onRetry: () => retries++
  });
  assert.equal(fetches.length, 2);
  const session = JSON.parse(fetches[0].body);
  assert.deepEqual(session, {ext: 'mp4', size: 10, mime: 'video/mp4', duration: 8});
  assert.equal(fetches[0].body.includes('"1,2,3"'), false);
  const registered = JSON.parse(fetches[1].body);
  assert.deepEqual(registered, {uid: 'a'.repeat(32), size: 10, duration: 8});
  assert.ok(puts.every(call => call.url.startsWith('https://www.googleapis.com/upload/drive/')));
  assert.deepEqual(Array.from(puts[0].body), [1, 2, 3, 4]);
  assert.equal(puts[0].headers['Content-Range'], 'bytes 0-3/10');
  assert.equal(puts[1].headers['Content-Range'], 'bytes */10');
  assert.equal(puts[1].body, null);
  assert.deepEqual(Array.from(puts[2].body), [5, 6, 7, 8]);
  assert.equal(puts[2].headers['Content-Range'], 'bytes 4-7/10');
  assert.deepEqual(Array.from(puts[3].body), [9, 10]);
  assert.equal(puts[3].headers['Content-Range'], 'bytes 8-9/10');
  assert.equal(progress.at(-1), 10);
  assert.equal(retries, 1);

  let calls = 0;
  await assert.rejects(upload.uploadVideoFile({name: 'grande.mp4', size: upload.VIDEO_MAX_BYTES + 1}, 3, {
    fetch: async () => { calls += 1; throw new Error('no debía pedir sesión'); }
  }), /100 MB/);
  assert.equal(calls, 0);

  const refused = await upload.uploadVideoFile(file, 4, {
    fetch: async () => ({ok: false, status: 409, json: async () => ({ok: false, error: 'Todavía no hay un evento de Abrolista en curso.'})}),
    put: async () => { throw new Error('no debía subir bytes'); }
  }).then(() => { throw new Error('tendría que fallar'); }, (error) => error);
  assert.equal(refused.message, 'Todavía no hay un evento de Abrolista en curso.');
  console.log('PASS: límites, sesión sin bytes, reintento por rango y registro');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
