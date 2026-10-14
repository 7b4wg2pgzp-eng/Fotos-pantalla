"""Galería por enlace de entrega, independiente de las fotos temporales de pantalla."""
import io,json,re,hmac,hashlib,unicodedata,threading,zipfile,os,urllib.request
from collections import deque
from urllib.parse import urlencode
from datetime import datetime
from flask import request,render_template,Response,abort,stream_with_context
from PIL import Image,ImageOps
from delivery import parse_date,TZ,FOLDER

MONTHS=['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre']

class ChunkWriter:
    def __init__(self):self.position=0;self.queue=deque()
    def write(self,data):self.queue.append(data);self.position+=len(data);return len(data)
    def tell(self):return self.position
    def flush(self):pass
    def drain(self):
        while self.queue:yield self.queue.popleft()

class Gallery:
    def __init__(self,delivery,secret='',origin='https://album.abrolista.com'):
        self.delivery,self.secret,self.origin=delivery,secret,origin.rstrip('/')
        self.lock=threading.RLock();self.cache={}

    def manifests(self):
        rows=self.delivery.list_files(self.delivery.data_folder(),"name contains 'album-' and mimeType != 'application/vnd.google-apps.folder'")
        out=[]
        for row in rows:
            if not row['name'].startswith('album-') or row.get('appProperties',{}).get('kind')!='eventGallery':continue
            data=json.loads(self.delivery.service.files().get_media(fileId=row['id']).execute())
            if data.get('identity') and data.get('slug'):out.append(data)
        return out

    def register(self,config,event,photos):
        identity=config['identity']
        with self.lock:
            if identity in self.cache:return self.cache[identity]
            manifests=self.manifests()
            existing=[m for m in manifests if m['identity']==identity]
            if existing:self.cache[identity]=existing[0];return existing[0]
            title=re.sub(r'^(?:15|XV)\s+','',config['name'],flags=re.I).strip()
            slug=unicodedata.normalize('NFKD',title).encode('ascii','ignore').decode().lower()
            slug=config.get('album_slug') or re.sub(r'[^a-z0-9]+','-',slug).strip('-') or 'fiesta'
            date=parse_date(config['start']).astimezone(TZ).date().isoformat()
            if any(m['slug']==slug for m in manifests):slug+='-'+date
            if any(m['slug']==slug for m in manifests):slug+='-'+identity[:8]
            manifest=dict(identity=identity,source_id=config.get('source_id'),slug=slug,name=config['name'],display_name=title,date=date,event_folder=event['id'],photos_folder=photos['id'])
            raw=json.dumps(manifest,ensure_ascii=False).encode()
            saved=self.delivery.write(self.delivery.data_folder(),'album-'+identity+'.json',raw,'application/json')
            self.delivery.service.files().update(fileId=saved['id'],body={'appProperties':{'kind':'eventGallery'}},fields='id').execute()
            self.cache[identity]=manifest
            return manifest

    def token(self,manifest):
        if len(self.secret)<32:raise RuntimeError('Falta configurar la clave privada de enlaces de álbum.')
        return hmac.new(self.secret.encode(),('abrolista:'+str(manifest['source_id'])).encode() if manifest.get('source_id') else manifest['identity'].encode(),hashlib.sha256).hexdigest()

    def link(self,config):
        manifests=[m for m in self.manifests() if m['identity']==config['identity']]
        if len(manifests)!=1 or not self.secret:return None
        manifest=manifests[0]
        return self.origin+'/'+manifest['slug']+'?'+urlencode({'acceso':self.token(manifest)})

    def authorized(self,slug):
        matches=[m for m in self.manifests() if m['slug']==slug]
        if len(matches)>1:abort(404)
        manifest=matches[0] if matches else self.resolve_empty(slug,request.args.get('acceso',''))
        supplied=request.args.get('acceso','')
        if not supplied or not hmac.compare_digest(supplied,self.token(manifest)):abort(404)
        return manifest

    def resolve_empty(self,slug,access):
        if not access:abort(404)
        key=os.environ.get('ABROLISTA_EVENT_SOURCE_TOKEN','')
        if not key:abort(503)
        req=urllib.request.Request('https://abrolista.com/api/eventphotos/album',data=json.dumps({'slug':slug,'access':access}).encode(),headers={'X-EventPhotos-Key':key,'Content-Type':'application/json','User-Agent':'Abrolista-EventPhotos/1.0'},method='POST')
        try:
            with urllib.request.urlopen(req,timeout=12) as response:result=json.loads(response.read(65536))
        except urllib.error.HTTPError as error:abort(404 if error.code in (403,404) else 503)
        except Exception:abort(503)
        if not result.get('ok'):abort(404)
        return result['event']

    def photos(self,manifest):
        if not manifest.get('photos_folder'):return []
        return sorted([f for f in self.delivery.list_files(manifest['photos_folder']) if f['mimeType']!=FOLDER and f.get('appProperties',{}).get('deliveryEvent')==manifest['identity']],key=lambda f:f['name'])

    def greetings(self,manifest):
        if not manifest.get('event_folder'):return []
        files=self.delivery.list_files(manifest['event_folder'],"name = 'Saludos.json'")
        if len(files)>1:raise RuntimeError('Saludos duplicados.')
        return json.loads(self.delivery.service.files().get_media(fileId=files[0]['id']).execute()) if files else []

    def download_chunks(self,file_id):
        from googleapiclient.http import MediaIoBaseDownload
        writer=ChunkWriter();downloader=MediaIoBaseDownload(writer,self.delivery.service.files().get_media(fileId=file_id),chunksize=512*1024)
        done=False
        while not done:
            _,done=downloader.next_chunk(num_retries=2)
            yield from writer.drain()

    def zip_chunks(self,manifest,photos):
        # ZIP en flujo: no acumular el álbum completo en memoria ni disco de Render Free.
        writer=ChunkWriter()
        with zipfile.ZipFile(writer,'w',compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            for index,photo in enumerate(photos,1):
                ext=photo['name'].rsplit('.',1)[-1].lower()
                with archive.open(f'{manifest["slug"]}/Foto-{index:04d}.{ext}','w',force_zip64=True) as member:
                    for chunk in self.download_chunks(photo['id']):
                        member.write(chunk);yield from writer.drain()
                yield from writer.drain()
            greetings=self.greetings(manifest)
            if greetings:
                text=manifest['name']+'\n\n'+'\n\n'.join((m.get('nombre') or 'Invitado')+'\n'+m.get('texto','') for m in greetings)
                archive.writestr(manifest['slug']+'/Saludos.txt',text.encode());yield from writer.drain()
        yield from writer.drain()


def register_gallery(app,gallery):
    def protected(response):
        response.headers['Cache-Control']='private, no-store'
        response.headers['X-Robots-Tag']='noindex, nofollow, noarchive'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    @app.get('/<slug>')
    @app.get('/album/<slug>')
    def gallery_page(slug):
        manifest=gallery.authorized(slug);token=gallery.token(manifest);query=urlencode({'acceso':token})
        date=datetime.fromisoformat(manifest['date']) if manifest.get('date') else None;event=dict(manifest,kind='Fiesta de 15' if re.match(r'^(15|XV)\s',manifest['name'],re.I) else 'Álbum del evento',display_date=f'{date.day} de {MONTHS[date.month-1]} de {date.year}' if date else 'Fecha por confirmar')
        photos=[dict(id=p['id'],preview_url=f'/album/{slug}/foto/{p["id"]}?{query}',download_url=f'/album/{slug}/foto/{p["id"]}?{query}&descargar=1') for p in gallery.photos(manifest)]
        html=render_template('gallery.html',event=event,photos=photos,greetings=gallery.greetings(manifest),zip_url=f'/album/{slug}/descargar?{query}',greetings_url=f'/album/{slug}/saludos?{query}',preview=False)
        return protected(Response(html,mimetype='text/html'))

    @app.get('/album/<slug>/foto/<file_id>')
    def gallery_photo(slug,file_id):
        manifest=gallery.authorized(slug)
        photos=[p for p in gallery.photos(manifest) if p['id']==file_id]
        if len(photos)!=1:abort(404)
        photo=photos[0]
        if request.args.get('descargar')=='1':
            response=Response(stream_with_context(gallery.download_chunks(file_id)),mimetype=photo['mimeType'])
            suffix=photo['name'].rsplit('.',1)[-1].lower();response.headers['Content-Disposition']=f'attachment; filename="{slug}-{file_id[-8:]}.{suffix}"'
            return protected(response)
        data=gallery.delivery.service.files().get_media(fileId=file_id).execute()
        try:
            with Image.open(io.BytesIO(data)) as image:
                image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((1400,1400));buffer=io.BytesIO();image.save(buffer,'JPEG',quality=85)
        except Exception:abort(415)
        return protected(Response(buffer.getvalue(),mimetype='image/jpeg'))

    @app.get('/album/<slug>/descargar')
    def gallery_download(slug):
        manifest=gallery.authorized(slug);photos=gallery.photos(manifest)
        response=Response(stream_with_context(gallery.zip_chunks(manifest,photos)),mimetype='application/zip')
        response.headers['Content-Disposition']=f'attachment; filename="{slug}-album.zip"'
        return protected(response)

    @app.get('/album/<slug>/saludos')
    def gallery_greetings(slug):
        manifest=gallery.authorized(slug);messages=gallery.greetings(manifest)
        text=manifest['name']+'\n\n'+'\n\n'.join((m.get('nombre') or 'Invitado')+'\n'+m.get('texto','') for m in messages)
        response=Response(text,mimetype='text/plain');response.headers['Content-Disposition']=f'attachment; filename="{slug}-saludos.txt"'
        return protected(response)
