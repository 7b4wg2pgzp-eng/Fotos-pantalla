const photos=JSON.parse(document.getElementById('galleryData').textContent);
const viewer=document.getElementById('viewer');
const image=document.getElementById('viewerImage');
const counter=document.getElementById('photoCounter');
const download=document.getElementById('viewerDownload');
let current=0;let opener=null;let toastTimer;let viewerRequest;let viewerUrl;
const thumbnails=[...document.querySelectorAll('.photo-open img[data-src]')];
const loader=createThumbnailLoader({fetcher:window.fetch.bind(window),urls:URL});
// Exactly two requests may run; other thumbnails are queued only near the viewport.
thumbnails.slice(0,2).forEach(item=>loader.enqueue(item));
if ('IntersectionObserver' in window) {
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(entry.isIntersecting)loader.enqueue(entry.target);else loader.leave(entry.target);
  }),{rootMargin:'150px 0px'});
  thumbnails.forEach(item=>observer.observe(item));
} else {
  const loadVisible=()=>thumbnails.forEach(item=>{const r=item.getBoundingClientRect();if(r.top<innerHeight+150&&r.bottom>-150)loader.enqueue(item);else loader.leave(item);});
  addEventListener('scroll',loadVisible,{passive:true});addEventListener('resize',loadVisible);loadVisible();
}
async function showPhoto(index){
  if(!photos.length)return;
  current=(index+photos.length)%photos.length;
  if(photos[current].removed){if(viewer.open)viewer.close();toast('Esta foto ya no está disponible.');return;}
  viewerRequest?.abort();viewerRequest=new AbortController();
  const controller=viewerRequest;
  if(viewerUrl){URL.revokeObjectURL(viewerUrl);viewerUrl=null;}
  image.removeAttribute('src');image.alt='Cargando foto '+(current+1)+' del álbum';
  counter.textContent=(current+1)+' / '+photos.length;download.href=photos[current].download_url;
  try{
    const response=await fetch(photos[current].preview_url,{cache:'no-cache',signal:controller.signal});
    if(!response.ok)throw new Error(String(response.status));
    const blob=await response.blob();
    if(controller.signal.aborted)return;
    viewerUrl=URL.createObjectURL(blob);image.src=viewerUrl;image.alt='Foto '+(current+1)+' del álbum';
  }catch(error){if(error.name!=='AbortError'){image.alt='Vista previa no disponible';toast('La vista previa no está disponible. Podés intentar descargar el original.');}}
}
document.querySelectorAll('.photo-open').forEach(button=>button.addEventListener('click',()=>{opener=button;showPhoto(Number(button.dataset.index));viewer.showModal();}));
document.querySelector('.viewer-close').addEventListener('click',()=>viewer.close());
document.querySelector('.viewer-prev').addEventListener('click',()=>showPhoto(current-1));
document.querySelector('.viewer-next').addEventListener('click',()=>showPhoto(current+1));
viewer.addEventListener('click',event=>{const box=viewer.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)viewer.close();});
viewer.addEventListener('close',()=>{viewerRequest?.abort();if(viewerUrl){URL.revokeObjectURL(viewerUrl);viewerUrl=null;}image.removeAttribute('src');opener?.focus();});
viewer.addEventListener('keydown',event=>{if(event.key==='ArrowRight'){event.preventDefault();showPhoto(current+1);}if(event.key==='ArrowLeft'){event.preventDefault();showPhoto(current-1);}});
document.querySelectorAll('[data-layout]').forEach(button=>button.addEventListener('click',()=>{document.getElementById('photoGrid').classList.toggle('feed',button.dataset.layout==='feed');document.querySelectorAll('[data-layout]').forEach(item=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));});}));
function toast(message){const target=document.getElementById('toast');clearTimeout(toastTimer);target.textContent=message;target.classList.add('visible');toastTimer=setTimeout(()=>target.classList.remove('visible'),3500);}
// Refresh moderation for an album left open, including browser back/forward restoration.
async function refreshAvailability(){
  if(document.hidden)return;
  try{
    const response=await fetch(document.getElementById('galleryData').dataset.statusUrl,{cache:'no-store'});
    if(!response.ok)throw new Error('unavailable');
    const allowed=new Set((await response.json()).photos);
    document.getElementById('photoGrid').hidden=false;
    photos.forEach((photo,index)=>{
      if(!allowed.has(photo.id)){
        photo.removed=true;
        const button=document.querySelector('.photo-open[data-index="'+index+'"]');
        if(button){const thumbnail=button.querySelector('img[data-src]');if(thumbnail)loader.cancel(thumbnail);button.closest('.photo-card').hidden=true;}
        if(current===index&&viewer.open)viewer.close();
      }
    });
  }catch(error){
    document.getElementById('photoGrid').hidden=true;
    if(viewer.open)viewer.close();
  }
}
setInterval(refreshAvailability,30000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAvailability();});
addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
document.querySelectorAll('.share').forEach(button=>button.addEventListener('click',async()=>{const url=location.href;try{if(navigator.share){await navigator.share({title:document.title,url});}else{await navigator.clipboard.writeText(url);toast('Enlace del álbum copiado.');}}catch(error){if(error.name!=='AbortError')toast('Copiá el enlace de la barra de direcciones para compartirlo.');}}));

// Subtle pointer depth; touch and reduced-motion visitors keep a static gallery.
if (matchMedia('(hover:hover) and (pointer:fine)').matches && !matchMedia('(prefers-reduced-motion:reduce)').matches) {
  document.querySelectorAll('.photo-card').forEach(card => {
    card.addEventListener('pointermove', event => {
      const rect=card.getBoundingClientRect();
      const x=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width));
      const y=Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height));
      card.style.setProperty('--tilt-x', ((.5-y)*5).toFixed(2)+'deg');
      card.style.setProperty('--tilt-y', ((x-.5)*5).toFixed(2)+'deg');
      card.style.setProperty('--shine-x', (x*100).toFixed(1)+'%');
      card.style.setProperty('--shine-y', (y*100).toFixed(1)+'%');
    });
    card.addEventListener('pointerleave', () => {
      ['--tilt-x','--tilt-y','--shine-x','--shine-y'].forEach(name=>card.style.removeProperty(name));
    });
  });
}

