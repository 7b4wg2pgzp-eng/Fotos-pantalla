// Marco dorado y turnos de video, sin navegador.
const assert = require('node:assert/strict');
const player = require('../static/screen_loop_videos.js');

function border(w, h) {
  return Math.max(10, Math.trunc(Math.max(w, h) * 0.012));
}

const cases = [
  [1920, 1080, 1920, 1080],
  [1080, 1920, 1920, 1080],
  [1000, 1000, 800, 600],
  [50, 80, 1280, 720],
];
for (const [videoW, videoH, stageW, stageH] of cases) {
  const box = player.frameBox(videoW, videoH, stageW, stageH);
  const edge = border(videoW, videoH);
  assert.equal(box.border, edge);
  assert.equal(box.color, 'rgb(232, 196, 104)');
  assert.ok(box.width <= stageW + 0.001);
  assert.ok(box.height <= stageH + 0.001);
  const touchesWidth = Math.abs(box.width - stageW) < 0.001;
  const touchesHeight = Math.abs(box.height - stageH) < 0.001;
  assert.ok(touchesWidth || touchesHeight);
  const innerW = box.width - box.borderWidth * 2;
  const innerH = box.height - box.borderWidth * 2;
  assert.ok(Math.abs(innerW / innerH - videoW / videoH) < 0.001);
}

assert.deepEqual(player.outputOptions(''), {audio: false, marco: false});
assert.deepEqual(player.outputOptions('?token=abc'), {audio: false, marco: false});
assert.deepEqual(player.outputOptions('?token=abc&audio=1'), {audio: true, marco: false});
assert.deepEqual(player.outputOptions('?marco=1'), {audio: false, marco: true});
assert.equal(player.outputOptions('?audio=0').audio, false);

function clockWith(videos) {
  const calls = [];
  let playing = null;
  const clock = player.createTurnClock({
    holdMs: 25000,
    showNext: () => calls.push('photo'),
    setInterval: () => 1,
    clearInterval: () => {},
    play: (video, done) => { playing = {video, done}; },
  });
  clock.setVideos(videos);
  return {clock, calls, get playing() { return playing; }};
}

const quiet = clockWith([]);
quiet.clock.tick();
quiet.clock.tick();
quiet.clock.tick();
assert.deepEqual(quiet.calls, ['photo', 'photo', 'photo']);
assert.equal(quiet.playing, null);

const uid = 'a'.repeat(32);
const mixed = clockWith([{uid, src: '/videos/' + uid, mime: 'video/mp4'}]);
mixed.clock.tick();
assert.deepEqual(mixed.calls, ['photo']);
assert.equal(mixed.playing, null);
mixed.clock.tick();
assert.equal(mixed.playing.video.uid, uid);
assert.deepEqual(mixed.calls, ['photo']);
mixed.playing.done('ended');
assert.deepEqual(mixed.calls, ['photo', 'photo']);

const broken = 'b'.repeat(32);
const skips = clockWith([{uid: broken, src: '/videos/' + broken, mime: 'video/mp4'}]);
skips.clock.tick();
skips.clock.tick();
skips.playing.done('decode');
skips.clock.tick();
skips.clock.tick();
assert.equal(skips.playing.video.uid, broken);
assert.deepEqual(skips.calls.at(-1), 'photo');

console.log('PASS: marco horizontal, vertical y cuadrado; el loop de fotos sigue cuando no hay video');
