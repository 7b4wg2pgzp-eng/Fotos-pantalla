# EventPhotos — muro de fotos en vivo

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

## Notas técnicas

- Todas las fotos se convierten a JPG, se corrige la rotación (EXIF) y se
  redimensionan a máx. 1920px de lado — así el loop no sufre con fotos
  pesadas de celular.
- HEIC (formato nativo de iPhone) se soporta gracias a `pillow-heif`.
- `/api/photos` devuelve la lista de fotos en orden de subida; `loop.html`
  la re-consulta cada 8s para sumar fotos nuevas sin recargar la página.
