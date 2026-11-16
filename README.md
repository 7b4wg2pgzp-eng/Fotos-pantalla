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
después sigue la rotación de fotos. Entra con el mismo criterio que una foto:
se ve enseguida y en `/admin` lo podés borrar. Si el navegador de la pantalla
no lo puede reproducir (por ejemplo un `.mov` HEVC del iPhone), lo saltea y
el archivo queda en Drive.

Quedan en la carpeta `Videos pantalla`, al lado de `Originales`. No se mezclan
con los videos del álbum ni con la limpieza de las fotos. No hace falta
ninguna variable nueva: usa la misma conexión de Drive.

El `Procfile` deja un solo proceso de gunicorn con 4 hilos y un timeout de
120 segundos, para que la pantalla pueda estar pasando un video y al mismo
tiempo entre una foto. En el plan gratuito de Render no subas la cantidad de
procesos: la memoria es de 512 MB.

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

