
  const stage = document.getElementById('stage');
  const emptyMsg = document.getElementById('empty');
  const HOLD_MS = 25000;
  const FETCH_EVERY_MS = 8000;

  let photos = [];          // orden de llegada, como lo da /api/photos
  let index = 0;
  const slides = new Map();  // src -> <div>, para no recrear (ni volver a bajar) lo que ya está
  const pendientes = [];     // fotos nuevas: salen antes que repetir las viejas
  let primeraCarga = true;
  let fallos = 0;

  async function refreshPhotos() {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    try {
      const res = await fetch('/api/photos', { cache: 'no-store', signal: ctl.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!Array.isArray(data.photos)) throw new Error('lista');
      fallos = 0;
      syncSlides(data.photos);
    } catch (e) {
      // Si el servidor se cae o está despertando, se sigue mostrando lo que ya hay
      // y se reintenta con espera creciente (máx. 60 s).
      fallos++;
    } finally {
      clearTimeout(t);
    }
    setTimeout(refreshPhotos, Math.min(FETCH_EVERY_MS * Math.max(1, fallos), 60000));
  }

  function syncSlides(lista) {
    const vignette = stage.querySelector('.vignette');
    const nuevas = new Set(lista);
    // Sacar las que ya no están (moderadas o vencidas), sin tocar el resto.
    for (const [src, el] of slides) {
      if (!nuevas.has(src)) { el.remove(); slides.delete(src); }
    }
    for (const src of lista) {
      if (slides.has(src)) continue;
      const el = document.createElement('div');
      el.className = 'slide';
      el.style.backgroundImage = `url("${src}")`;
      stage.insertBefore(el, vignette);
      slides.set(src, el);
      if (!primeraCarga) pendientes.push(src);
    }
    primeraCarga = false;
    photos = lista;
    emptyMsg.style.display = photos.length ? 'none' : 'flex';
    index = Math.min(index, Math.max(photos.length - 1, 0));
  }

  function showNext() {
    if (!photos.length) return;
    let src;
    while (pendientes.length && !slides.has(pendientes[0])) pendientes.shift();
    if (pendientes.length) {
      src = pendientes.shift();          // lo recién subido va en el próximo cambio
    } else {
      src = photos[index % photos.length];
      index = (index + 1) % photos.length;
    }
    for (const el of slides.values()) el.classList.remove('active');
    const el = slides.get(src);
    if (!el) return;
    void el.offsetWidth;
    el.classList.add('active');
  }

  refreshPhotos();
  setInterval(showNext, HOLD_MS);
