// Revisa que el loop y la página de subida sigan siendo JavaScript válido
// y que conserven el comportamiento pedido. Sin navegador ni red.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function scripts(file) {
  const html = fs.readFileSync(file, 'utf8');
  const found = [];
  const re = /<script>([\s\S]*?)<\/script>/g;
  let match;
  while ((match = re.exec(html))) found.push(match[1]);
  assert.ok(found.length, 'sin script en ' + file);
  return found.join('\n');
}

function check(file) {
  const source = scripts(file);
  const tmp = path.join(os.tmpdir(), path.basename(file) + '.js');
  fs.writeFileSync(tmp, source);
  execFileSync(process.execPath, ['--check', tmp], { stdio: 'inherit' });
  return source;
}

const upload = check(path.join(__dirname, '../templates/upload.html'));
assert.match(upload, /MAX_LADO = 2048/);
assert.match(upload, /image\/jpeg', 0\.85/);
assert.match(upload, /Enviando \$\{i \+ 1\} de \$\{total\}…/);
assert.match(upload, /async function enviar\(form, intentos = 3\)/);
assert.match(upload, /form\.append\('photos'/);
assert.doesNotMatch(upload, /selected\.forEach\(f => form\.append\('photos'/);

const loop = check(path.join(__dirname, '../templates/loop.html'));
assert.match(loop, /const pendientes = \[\]/);
assert.match(loop, /slides = new Map\(\)/);
assert.match(loop, /60000/);
assert.doesNotMatch(loop, /rebuildSlides/);
assert.doesNotMatch(loop, /setInterval\(refreshPhotos/);

console.log('paginas ok');
