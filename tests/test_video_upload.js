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

  function box(type, payload) {
    const size = 8 + payload.length;
    const out = new Uint8Array(size);
    out[0] = (size >>> 24) & 255;
    out[1] = (size >>> 16) & 255;
    out[2] = (size >>> 8) & 255;
    out[3] = size & 255;
    out.set(type, 4);
    out.set(payload, 8);
    return out;
  }
  function concat(parts) {
    const size = parts.reduce((sum, part) => sum + part.length, 0);
    const out = new Uint8Array(size);
    let offset = 0;
    parts.forEach((part) => { out.set(part, offset); offset += part.length; });
    return out;
  }
  function fileFrom(name, bytes, seen) {
    return {
      name,
      size: bytes.length,
      slice(start, end) {
        if (seen) seen.push([start, end]);
        const part = bytes.slice(start, end);
        return {arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength)};
      }
    };
  }
  const hevc = concat([
    box([0x66, 0x74, 0x79, 0x70], new Uint8Array([0x69, 0x73, 0x6f, 0x6d, 0, 0, 0, 0])),
    box([0x6d, 0x64, 0x61, 0x74], new Uint8Array(64)),
    box([0x6d, 0x6f, 0x6f, 0x76], box([0x73, 0x74, 0x73, 0x64], new Uint8Array([0x68, 0x76, 0x63, 0x31])))
  ]);
  const seen = [];
  const hevcFile = fileFrom('iphone.mov', hevc, seen);
  assert.equal(await upload.containerIsHevc(hevcFile), true);
  assert.equal(await upload.screenFormatError(hevcFile, {canPlay: () => 'maybe', probe: async () => true}), upload.VIDEO_ERRORS.screen);
  assert.ok(seen.every(([start]) => start < 24 || start >= 88));
  const avc = concat([
    box([0x66, 0x74, 0x79, 0x70], new Uint8Array([0x69, 0x73, 0x6f, 0x6d, 0, 0, 0, 0])),
    box([0x6d, 0x6f, 0x6f, 0x76], box([0x73, 0x74, 0x73, 0x64], new Uint8Array([0x61, 0x76, 0x63, 0x31])))
  ]);
  assert.equal(await upload.screenFormatError(fileFrom('ok.mp4', avc), {canPlay: () => 'probably', probe: async () => true}), null);
  assert.equal(await upload.screenFormatError(fileFrom('ok.mp4', avc), {canPlay: () => '', probe: async () => true}), upload.VIDEO_ERRORS.screen);
  assert.equal(await upload.screenFormatError(fileFrom('ok.webm', avc), {canPlay: () => 'maybe', probe: async () => false}), upload.VIDEO_ERRORS.screen);
  assert.equal(upload.VIDEO_ERRORS.screen.includes('Más compatible'), true);
  console.log('PASS: límites, sesión sin bytes, reintento por rango y registro');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
