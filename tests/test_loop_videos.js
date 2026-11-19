// Marco dorado y turnos de video, sin navegador.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
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

// El original HEVC no decodificó; llega la copia H.264 (otro mime) y se reintenta.
let turns = 0;
const retried = player.createTurnClock({
  holdMs: 25000,
  showNext: () => {},
  setInterval: () => 1,
  clearInterval: () => {},
  play: (video, done) => { turns += 1; retried.last = {video, done}; },
});
const hevcUid = 'c'.repeat(32);
retried.setVideos([{uid: hevcUid, src: '/videos/' + hevcUid, mime: 'video/mp4', pantalla: 'failed'}]);
retried.tick();
retried.tick();
assert.equal(turns, 1);
retried.last.done('decode');
retried.tick();
retried.tick();
assert.equal(turns, 1);
retried.setVideos([{uid: hevcUid, src: '/videos/' + hevcUid, mime: 'video/mp4', pantalla: 'ready'}]);
retried.tick();
retried.tick();
assert.equal(turns, 2);
assert.equal(retried.last.video.mime, 'video/mp4');
assert.equal(player.validLoopVideo({uid: hevcUid, src: '/videos/' + hevcUid, mime: 'video/mp4', playable: false}), false);

function classList(names) {
  const set = new Set(names);
  return {
    contains: (name) => set.has(name),
    add: (name) => set.add(name),
    remove: (name) => set.delete(name),
  };
}

function slide(active) {
  return {classList: classList(active ? ['slide', 'active'] : ['slide']), style: {}};
}

function stageFor(slides) {
  return {
    slides,
    querySelectorAll(selector) {
      if (selector !== '.slide.active') return [];
      return this.slides.filter((item) => item.classList.contains('active'));
    },
  };
}

const page = fs.readFileSync(path.join(__dirname, '../templates/loop.html'), 'utf8');
assert.match(page, /background:transparent/);
assert.match(page, /transition:opacity 1\.5s ease/);
assert.match(page, /\.slide\.active\{ opacity:1; \}/);
assert.doesNotMatch(page, /\.stage\{[^}]*background:\s*(#|rgb|black)/);
assert.doesNotMatch(page, /\.clip-frame\{[^}]*inset:\s*0/);

const hidden = stageFor([slide(true), slide(true)]);
player.concealSlides(hidden);
assert.equal(hidden.querySelectorAll('.slide.active').length, 0);
assert.equal(hidden.slides[0].style.opacity, undefined);
assert.equal(hidden.slides[1].style.backgroundImage, undefined);

function photoLoop() {
  const photos = ['a', 'b', 'c'];
  let index = 0;
  const shown = [];
  return {
    shown,
    showNext() {
      const src = photos[index % photos.length];
      index = (index + 1) % photos.length;
      shown.push(src);
    },
  };
}

function clockOver(loop, videos) {
  const slides = stageFor([slide(true)]);
  let playing = null;
  const clock = player.createTurnClock({
    holdMs: 25000,
    showNext: () => loop.showNext(),
    conceal: () => player.concealSlides(slides),
    setInterval: () => 1,
    clearInterval: () => {},
    play: (video, done) => {
      playing = {video, done};
      return (reason) => done(reason);
    },
  });
  clock.setVideos(videos);
  return {clock, slides, get playing() { return playing; }};
}

const order = photoLoop();
const run = clockOver(order, [{uid, src: '/videos/' + uid, mime: 'video/mp4'}]);
run.clock.tick();
assert.deepEqual(order.shown, ['a']);
run.clock.tick();
assert.equal(run.slides.querySelectorAll('.slide.active').length, 0);
assert.deepEqual(order.shown, ['a']);
run.clock.tick();
assert.deepEqual(order.shown, ['a']);
run.playing.done('ended');
assert.deepEqual(order.shown, ['a', 'b']);
run.clock.tick();
assert.deepEqual(order.shown, ['a', 'b', 'c']);

for (const reason of ['cap', 'stall', 'decode']) {
  const loop = photoLoop();
  const turn = clockOver(loop, [{uid, src: '/videos/' + uid, mime: 'video/mp4'}]);
  turn.clock.tick();
  turn.clock.tick();
  turn.playing.done(reason);
  assert.deepEqual(loop.shown, ['a', 'b'], reason);
  turn.playing.done(reason);
  assert.deepEqual(loop.shown, ['a', 'b'], reason);
}

const removed = photoLoop();
const cut = clockOver(removed, [{uid, src: '/videos/' + uid, mime: 'video/mp4'}]);
cut.clock.tick();
cut.clock.tick();
cut.clock.setVideos([]);
assert.deepEqual(removed.shown, ['a', 'b']);
cut.clock.tick();
assert.deepEqual(removed.shown, ['a', 'b', 'c']);

function clipDoc(slides) {
  const kids = [];
  const clip = {
    muted: false,
    currentTime: 0,
    error: null,
    videoWidth: 1920,
    videoHeight: 1080,
    firstChild: null,
    pause() {},
    load() {},
    play() { return null; },
    removeAttribute() {},
    appendChild(node) { kids.push(node); this.firstChild = node; },
    removeChild() { kids.pop(); this.firstChild = kids[0] || null; },
  };
  const frame = {classList: classList(), style: {}};
  const stage = {
    clientWidth: 1920,
    clientHeight: 1080,
    style: {},
    querySelector: (selector) => selector === '.slide' ? slides[0] : null,
  };
  return {
    clip,
    frame,
    stage,
    createElement: () => ({}),
    querySelectorAll: (selector) => (
      selector === '.slide.active' ? slides.filter((item) => item.classList.contains('active')) : []
    ),
    getElementById: (id) => ({clip, clipFrame: frame, stage, empty: {style: {}}}[id] || null),
  };
}

(async () => {
  const slides = [slide(true), slide(true)];
  const doc = clipDoc(slides);
  const reasons = [];
  const stop = player.playClip(doc, {src: '/videos/' + uid, mime: 'video/mp4'}, (reason) => reasons.push(reason));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(doc.querySelectorAll('.slide.active').length, 0);
  assert.equal(doc.clip.muted, true);
  assert.equal(doc.frame.classList.contains('playing'), true);
  assert.equal(doc.stage.style.background, undefined);
  doc.clip.onended();
  assert.deepEqual(reasons, ['ended']);
  stop("deleted");
  assert.deepEqual(reasons, ['ended']);

  const capped = [slide(true)];
  const capDoc = clipDoc(capped);
  const capReasons = [];
  player.playClip(capDoc, {src: '/videos/' + uid, mime: 'video/mp4'}, (reason) => capReasons.push(reason));
  await new Promise((resolve) => setTimeout(resolve, 20));
  capDoc.clip.currentTime = 60;
  capDoc.clip.ontimeupdate();
  assert.deepEqual(capReasons, ['cap']);
  assert.equal(capDoc.frame.classList.contains('playing'), false);

  const failed = [slide(true)];
  const failDoc = clipDoc(failed);
  const failReasons = [];
  const cancel = player.playClip(failDoc, {src: '/videos/' + uid, mime: 'video/mp4'}, (reason) => failReasons.push(reason));
  await new Promise((resolve) => setTimeout(resolve, 20));
  failDoc.clip.error = {code: 4};
  failDoc.clip.onerror();
  assert.deepEqual(failReasons, ['decode']);
  cancel("deleted");
  assert.deepEqual(failReasons, ['decode']);

  const dropped = [slide(true)];
  const dropDoc = clipDoc(dropped);
  const dropReasons = [];
  const drop = player.playClip(dropDoc, {src: '/videos/' + uid, mime: 'video/mp4'}, (reason) => dropReasons.push(reason));
  await new Promise((resolve) => setTimeout(resolve, 20));
  drop('deleted');
  assert.deepEqual(dropReasons, ['deleted']);
  assert.equal(dropDoc.querySelectorAll('.slide.active').length, 0);
  assert.equal(stop === undefined, false);
  console.log('PASS: marco horizontal, vertical y cuadrado; la foto se oculta durante el video');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
