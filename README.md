# EventPhotos — muro de fotos en vivo

Corrección de moderación preparada en la rama de pruebas: [comportamiento, pruebas y requisitos](MODERATION.md).

Optimización del álbum familiar en esa misma rama: [miniaturas persistentes, preparación y verificación](ALBUM_PERFORMANCE.md).

Servidor local: los invitados suben fotos desde el celular, vos las mostrás
en loop como Browser Source en OBS o vMix.

## 1. Instalar

```bash
cd eventphotos
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configurar (por evento)

Editá las 3 líneas al inicio de `app.py` (o pasalas como variables de entorno):

- `EVENT_NAME` — título que ven los invitados en la página de subida.
- `EVENTPHOTOS_DIR` — carpeta donde se guardan las fotos. Si la apuntás
  adentro de tu carpeta de **Google Drive Desktop**
  (ej: `~/Google Drive/Mi unidad/EventPhotos`), quedan respaldadas en Drive
  solas, sin usar la API de Google.
- `PORT` — puerto local (5050 por defecto).

## 3. Correr

```bash
python3 app.py
```

- **Subida** (para invitados): `http://TU-IP-LOCAL:5050/`
- **Loop** (Browser Source en OBS/vMix): `http://localhost:5050/loop`

Para saber tu IP local en la wifi: `ipconfig getifaddr en0`

## 4. Que los invitados accedan (con su propia línea, sin wifi del salón)

Instalá `cloudflared` una sola vez:

```bash
brew install cloudflared
```

El día del evento, en vez de `python3 app.py`, corré:

```bash
./start.sh
```

Esto hace 3 cosas:
1. Levanta el servidor Flask.
2. Abre un túnel de Cloudflare (gratis, sin cuenta) que le da una URL
   pública tipo `https://algo-random.trycloudflare.com`.
3. Genera y abre `qr_invitados.png` con el QR apuntando a esa URL.

Mostrás/imprimís ese QR y los invitados escanean desde su propia línea,
sin necesidad de estar en la wifi del salón.

Para bajar todo al terminar: `./stop.sh`

**Importante:** la URL cambia cada vez que corrés `start.sh`, así que el
QR hay que generarlo de nuevo (automático) cada evento — no sirve
imprimirlo con anticipación. Si más adelante querés una URL fija con tu
propio dominio (para un QR que se reutiliza siempre), se puede armar con
un túnel con nombre de Cloudflare — avisame cuando quieras dar ese paso.

## 5. Dejarlo corriendo solo (LaunchAgent, como el muro de WhatsApp)

```bash
cp com.nico.eventphotos.plist ~/Library/LaunchAgents/
# Editá el .plist: reemplazá TU_USUARIO por tu usuario de macOS
launchctl load ~/Library/LaunchAgents/com.nico.eventphotos.plist
```

Para bajarlo: `launchctl unload ~/Library/LaunchAgents/com.nico.eventphotos.plist`

## 6. Videos cortos

En la página de subida, el invitado puede elegir un video de hasta 60 segundos
y 100 MB (`mp4`, `mov` o `webm`). El celular lo manda directo a Drive con una
sesión resumible: el servidor solo abre esa sesión y anota el video. Los
bytes no pasan por gunicorn.

En `/loop` se reproduce sin sonido, entero, con tope de 60 segundos, y
después sigue la rotación de fotos. El loop de fotos y el de saludos no
cambian: el video es un turno más. Entra con el mismo criterio que una foto:
se ve enseguida. Si el navegador de la pantalla no lo puede reproducir (por
ejemplo un `.mov` HEVC del iPhone), lo saltea y el archivo queda en Drive.

El marco del video es el mismo de las fotos: borde sólido `rgb(232, 196, 104)`,
con un grosor de 10 px o el 1,2 % del lado mayor, el que sea más grande. El
video más el marco entran en la pantalla.

En `/admin/videos` (con la misma `ADMIN_KEY`) se ven del más nuevo al más
viejo, con el saludo o el nombre, la duración y la hora. Ahí se lanzan, se
paran, se repiten, se marcan como vistos o se borran. Borrar también los saca
de `/loop`.

La salida para OBS, vMix o Resolume es `/vj?token=...`. El token sale de
`VJ_OUTPUT_TOKEN`. Sin esa variable la página responde 403. La `ADMIN_KEY` no
va en esa URL. Arranca en mudo y sin marco; `&audio=1` prende el sonido y
`&marco=1` pone el marco. Se actualiza sola cada 2 segundos.

Quedan en la carpeta `Videos pantalla`, al lado de `Originales`. No se mezclan
con los videos del álbum ni con la limpieza de las fotos. La subida usa la
misma conexión de Drive. Antes de unir, cargá `VJ_OUTPUT_TOKEN` en Render.

Al registrarse, el video también se copia a `Entregas/<fiesta>/Videos`, que es
la carpeta que el álbum del cliente ya muestra, reproduce por rangos y mete en
el ZIP. Si el VJ lo borra, la copia se saca y no entra al álbum. La limpieza
de `Originales` no mira `Videos pantalla` y no espera esa copia para borrar las
fotos.

El álbum lo sirve otro proceso, `abrolista-album.service`, en la VM de Oracle.
No hace falta una variable nueva. Para que un video borrado no aparezca aunque
Drive no haya terminado de eliminarlo, hay que publicar esta revisión en esa
VM (no se hace sola con Render):

```bash
sudo bash ops/deploy_album.sh SHA_COMPLETO_DE_40_CARACTERES
```

Ese script baja el SHA, corre las pruebas del álbum y reinicia el servicio.
Si falla, vuelve a la versión anterior. No toca `/etc/abrolista-album.env`.

El `Procfile` deja un solo proceso de gunicorn con 4 hilos y un timeout de
120 segundos. Hace falta: el worker por defecto es síncrono y corta a los 30
segundos, así que un video de hasta 60 segundos dejaría trabada la única
petición y no podría entrar una foto al mismo tiempo. El estado de «lanzar» y
«parar» vive en ese proceso: con más de un proceso, la salida podría mirar
otro y no ver el video. Cuatro hilos alcanzan para el loop, la salida, una
subida y los avisos, sin subir la memoria del plan gratuito (512 MB).

### Transcodificar más adelante (VPS, no Render Free)

`transcode_for_screen()` en `screen_videos.py` devuelve `None` a propósito.
No bajes el video a Render para convertirlo: no entra en la memoria del plan
gratuito y el disco se borra en cada reinicio. En un VPS con ffmpeg se puede
dejar una copia H.264 720p, yuv420p, con `+faststart` y un poster, sin borrar
el original:

```bash
ffmpeg -y -i entrada.mov -vf "scale=-2:720" -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 128k -movflags +faststart salida.mp4
ffmpeg -y -i salida.mp4 -ss 1 -frames:v 1 poster.jpg
```

Para probar en local, con Drive simulado:

```bash
python3 -m unittest tests.test_screen_videos -v
node tests/test_video_upload.js
```

## Notas técnicas

- Todas las fotos se convierten a JPG, se corrige la rotación (EXIF) y se
  redimensionan a máx. 1920px de lado — así el loop no sufre con fotos
  pesadas de celular.
- HEIC (formato nativo de iPhone) se soporta gracias a `pillow-heif`.
- `/api/photos` devuelve la lista de fotos en orden de subida; `loop.html`
  la re-consulta cada 8s para sumar fotos nuevas sin recargar la página.

