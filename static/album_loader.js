/* Bounded image requests, shared by the first visible row and scroll observer. */
(function (root) {
  function createThumbnailLoader({fetcher, urls, limit = 2}) {
    const queue = [];
    const states = new Map();
    let active = 0;
    let stopped = false;
    function pump() {
      while (!stopped && active < limit && queue.length) {
        const image = queue.shift();
        if (states.get(image) !== 'queued') continue;
        states.set(image, 'loading');
        active++;
        (async () => {
          let url;
          try {
            const response = await fetcher(image.dataset.src, {cache: 'no-cache', credentials: 'same-origin',
                                                               priority: image.fetchPriority === 'high' ? 'high' : 'low'});
            if (!response.ok) throw new Error(String(response.status));
            const blob = await response.blob();
            if (stopped || states.get(image) === 'cancelled') return;
            url = urls.createObjectURL(blob);
            image.loading = 'eager'; // Visibility and concurrency are already controlled here.
            image.src = url;
            if (image.decode) await image.decode();
            if (states.get(image) !== 'cancelled') {
              states.set(image, 'loaded'); image.dataset.loaded = 'true';
            }
          } catch (error) {
            if (states.get(image) !== 'cancelled') {
              states.set(image, 'failed');
              image.alt = 'Vista previa no disponible. Podés descargar el original.';
            }
          } finally {
            // The decoded image stays visible; the temporary blob need not stay allocated.
            if (url) urls.revokeObjectURL(url);
            active--;
            pump();
          }
        })();
      }
    }
    return {
      enqueue(image) {
        if (stopped || states.has(image)) return;
        states.set(image, 'queued'); queue.push(image); pump();
      },
      leave(image) {
        if (states.get(image) === 'queued') states.delete(image);
      },
      cancel(image) { states.set(image, 'cancelled'); delete image.dataset.loaded; image.removeAttribute('src'); },
      destroy() { stopped = true; queue.length = 0; states.clear(); }
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = {createThumbnailLoader};
  else root.createThumbnailLoader = createThumbnailLoader;
})(typeof window === 'undefined' ? globalThis : window);
