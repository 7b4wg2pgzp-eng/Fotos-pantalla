const photos=JSON.parse(document.getElementById('galleryData').textContent);
const viewer=document.getElementById('viewer');
const image=document.getElementById('viewerImage');
const counter=document.getElementById('photoCounter');
const download=document.getElementById('viewerDownload');
let current=0;let opener=null;let toastTimer;
function showPhoto(index){if(!photos.length)return;current=(index+photos.length)%photos.length;image.src=photos[current].preview_url;image.alt='Foto '+(current+1)+' del álbum';counter.textContent=(current+1)+' / '+photos.length;download.href=photos[current].download_url;}
document.querySelectorAll('.photo-open').forEach(button=>button.addEventListener('click',()=>{opener=button;showPhoto(Number(button.dataset.index));viewer.showModal();}));
document.querySelector('.viewer-close').addEventListener('click',()=>viewer.close());
document.querySelector('.viewer-prev').addEventListener('click',()=>showPhoto(current-1));
document.querySelector('.viewer-next').addEventListener('click',()=>showPhoto(current+1));
viewer.addEventListener('click',event=>{const box=viewer.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)viewer.close();});
viewer.addEventListener('close',()=>{image.removeAttribute('src');opener?.focus();});
viewer.addEventListener('keydown',event=>{if(event.key==='ArrowRight'){event.preventDefault();showPhoto(current+1);}if(event.key==='ArrowLeft'){event.preventDefault();showPhoto(current-1);}});
document.querySelectorAll('[data-layout]').forEach(button=>button.addEventListener('click',()=>{document.getElementById('photoGrid').classList.toggle('feed',button.dataset.layout==='feed');document.querySelectorAll('[data-layout]').forEach(item=>{const selected=item===button;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected));});}));
function toast(message){const target=document.getElementById('toast');clearTimeout(toastTimer);target.textContent=message;target.classList.add('visible');toastTimer=setTimeout(()=>target.classList.remove('visible'),3500);}
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
