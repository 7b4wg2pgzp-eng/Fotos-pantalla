// Run with: node tests/test_admin_moderation.js (no browser, network or dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../templates/admin.html'), 'utf8');
const source = template.slice(template.indexOf('const pendingDeletes ='), template.lastIndexOf('\ncargar();'));
const uid = 'a'.repeat(32) + '.jpg';
const storage = new Map();
const alerts = [];
const buttons = [];
let reply;
let reloads = 0;
function context() {
  const ctx = vm.createContext({
    KEY: 'fake-key', FormData, confirm: () => true, alert: text => alerts.push(text),
    cargar: () => reloads++,
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    document: {
      getElementById: () => ({replaceChildren: () => {buttons.length = 0;}, appendChild: el => buttons.push(el)}),
      createElement: () => ({})
    },
    fetch: async () => {
      if (reply instanceof Error) throw reply;
      return {ok: reply.ok, json: async () => reply};
    }
  });
  vm.runInContext(source, ctx);
  return ctx;
}
(async () => {
  let ctx = context();
  reply = {ok: false, error: 'Drive pendiente'};
  await ctx.borrar('/api/admin/borrar-foto', {nombre: uid});
  assert.equal(alerts.at(-1), 'Drive pendiente');
  assert.equal(reloads, 0);
  assert.deepEqual(JSON.parse(storage.get('pendingPhotoDeletes')), [uid]);
  ctx = context(); // Pending action survives panel reload.
  assert.equal(buttons.length, 1);
  reply = {ok: true};
  await buttons[0].onclick();
  assert.equal(buttons.length, 0);
  assert.equal(reloads, 1);
  reply = new Error('offline');
  await ctx.borrar('/api/admin/borrar-foto', {nombre: uid});
  assert.equal(buttons.length, 1);
  assert.equal(reloads, 1);
  reply = {ok: true};
  await ctx.borrar('/api/admin/borrar-mensaje', {ts: 123});
  assert.equal(buttons.length, 1); // Message action does not clear pending photo.
  assert.equal(reloads, 2);
  console.log('PASS: partial error, retry after reload, success, network error, message regression');
})().catch(error => { console.error(error); process.exitCode = 1; });
