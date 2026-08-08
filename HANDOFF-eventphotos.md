# EventPhotos — Documento de continuidad

> Subí este archivo al inicio de un chat nuevo para retomar el proyecto sin explicar nada.

---

## 1. Qué es el proyecto

App web para eventos (quinceañeras, fiestas). Los invitados escanean un QR, suben fotos y escriben saludos desde el celular. Todo se muestra en loop en la pantalla del salón vía **OBS** o **vMix**. Corre en la nube; la Mac solo se usa para el software de video.

**Dueño:** Nicolás — DJ y productor de eventos. Trabaja con terminal en macOS, itera pegando errores para que se los corrija. Prefiere minimalismo visual. Idioma: español.

---

## 2. Infraestructura

**Repo:** `github.com/7b4wg2pgzp-eng/Fotos-pantalla` (rama `main`)
**Carpeta local:** `~/Downloads/eventphotos 3`
**Hosting:** Render, plan Free, dos servicios apuntando al **mismo repo**

| Instancia | URL base | Persiste en Drive |
|---|---|---|
| Nicolás | `https://fotos-pantalla.onrender.com` | Sí |
| Compañero (vMix) | `https://fotos-en-pantalla.onrender.com` | No |

**Rutas (iguales en ambas):**

| Ruta | Uso |
|---|---|
| `/` | Página de invitados: fotos + nombre + saludo |
| `/loop` | Slideshow de fotos → Browser Source en OBS/vMix |
| `/saludos` | Saludos en burbuja, fondo transparente → VJ |
| `/admin?key=CLAVE` | Panel de moderación |
| `/api/photos`, `/api/mensajes`, `/api/upload` | APIs internas |

---

## 3. Variables de entorno (Render)

**Instancia de Nicolás:**
```
EVENT_NAME                    nombre del evento (cambia entre fiestas)
ADMIN_KEY                     161289
GDRIVE_FOLDER_ID              1n3xYo19VuEkNSVDiHStbLQ0gmkDHXxKW
GOOGLE_CLIENT_ID              (OAuth)
GOOGLE_CLIENT_SECRET          (OAuth)
GOOGLE_REFRESH_TOKEN          (OAuth)
GDRIVE_SERVICE_ACCOUNT_B64    legacy, ya no se usa
RETENTION_HOURS               opcional, default 12
```

**Instancia del compañero:** solo `EVENT_NAME` y `ADMIN_KEY`.
La **ausencia** de variables `GOOGLE_*` es lo que la hace efímera — el código detecta que no hay credenciales y guarda solo en disco temporal.

Cuenta de Drive en uso: `cuentadeproduccionmusical@gmail.com`

---

## 4. Archivos del proyecto

```
app.py                    ~519 líneas — todo el backend Flask
templates/
  upload.html             página de invitados (fotos + saludo)
  loop.html               slideshow de fotos
  saludos.html            saludos en burbuja dorada
  admin.html              132 líneas — panel con lightbox
requirements.txt          Flask, Pillow, pillow-heif, qrcode[pil],
                          gunicorn, google-api-python-client, google-auth
Procfile                  web: gunicorn app:app --bind 0.0.0.0:$PORT
get_token.py              se corre UNA vez para obtener el refresh token
make_qr.py                genera el QR de invitados
start.sh / stop.sh        solo para el modo local antiguo (Cloudflare tunnel)
```

---

## 5. Arquitectura de `app.py`

**Inicialización de Drive (orden de prioridad):**
1. Si hay `GOOGLE_REFRESH_TOKEN` + `CLIENT_ID` + `CLIENT_SECRET` → OAuth (usa la cuota personal de Drive)
2. Si no, y hay `GDRIVE_SERVICE_ACCOUNT_B64` → Service Account (**no funciona** con Gmail personal)
3. Si no hay nada → solo disco local, sin persistencia

Al arrancar imprime `[drive] conectado via OAuth (cuenta personal).` o `via Service Account`. Ese log es el primer lugar a revisar cuando algo de Drive falla.

**Funciones principales:**

| Función | Qué hace |
|---|---|
| `get_subfolder(nombre)` | Crea/encuentra subcarpeta en Drive, cachea el ID |
| `save_original(name, data)` | Sube el archivo intacto a `Originales/` |
| `drive_upsert(name, data, parent)` | Crea o reemplaza (previews y `mensajes.json`) |
| `save_upload(file_storage)` | Guarda original + genera preview con borde dorado |
| `save_message(nombre, texto)` | Agrega saludo y sincroniza `mensajes.json` a Drive |
| `_restore_from_drive()` | Al arrancar, rearma el cache desde Drive (thread daemon) |
| `convert_raw_to_jpeg_bytes()` | RAW→JPEG con `sips` (**solo macOS**, falla en Render) |

**Almacenamiento:**
- `CACHE_DIR` = `/tmp/eventphotos_cache` → previews que sirve el loop
- Drive `EventPhotos/` → `Originales/`, `Previews/`, `Datos/`

---

## 6. Comportamiento actual

**Retención:** fotos y saludos se muestran 12 h desde su subida; después dejan de aparecer. **No se borra nada de Drive**, el corte es solo visual (filtro en `/api/photos` y `/api/mensajes`).

**Loop de fotos:** 25 s por foto, fundido 1.5 s, sin zoom, `background-size: contain`, viñeta, refresco cada 8 s.

**Saludos:** burbuja `rgba(18,12,27,0.80)` con blur, borde dorado 2px, `border-radius: 36px` uniforme, `width: fit-content`, 18 s cada uno, fondo de página transparente.

**Paleta:** dorado `#E8C468`, magenta `#FF3E8E`, fondo `#150F1F`, texto `#F5F0FA`. Tipografías: Unbounded (títulos) + Manrope (texto).

**Panel admin:** grilla de fotos con X para borrar, clic en la miniatura abre lightbox con botón de borrar, Escape o clic fuera cierra. Lista de saludos con botón Borrar. Recarga cada 10 s. Borrar una foto la elimina también de Drive.

---

## 7. Decisiones técnicas (no revisitar)

- **Las Service Accounts de Google no tienen cuota de almacenamiento.** Con Gmail personal siempre dan `403 storageQuotaExceeded`. Por eso se migró a OAuth con refresh token. No volver a proponer Service Account.
- **OAuth requiere:** proyecto en Google Cloud → pantalla de consentimiento (Externo) → el email como *usuario de prueba* → credencial tipo **App de escritorio** → correr `get_token.py` una vez en local.
- **Render Free duerme** tras inactividad; despertar tarda ~50 s. OBS/vMix con el Browser Source abierto consulta cada 8 s y lo mantiene vivo.
- **El disco de Render es efímero.** Por eso las previews y `mensajes.json` se replican a Drive y se restauran al arrancar.
- **`sips` es de macOS.** La conversión de RAW/DNG solo funciona corriendo local; en Render esos archivos se guardan pero no aparecen en el loop.

---

## 8. Flujo entre eventos

1. Bajar la carpeta `Originales` de Drive → entregar al cliente
2. Vaciar las tres subcarpetas (`Originales`, `Previews`, `Datos`)
3. Cambiar `EVENT_NAME` en Render
4. Abrir `/loop` unos 5 min antes para despertar el servicio

Con ~5 días entre eventos, el corte de 12 h ya limpió todo solo; vaciar es por orden y espacio.

---

## 9. Cómo aplicar un cambio

```bash
cd ~/Downloads/"eventphotos 3"
# editar archivos
python3 -m py_compile app.py && echo "compila OK"
git add .
git commit -m "descripción"
git push
```
Render redeploya solo al detectar el push. **Afecta a las dos instancias** (comparten repo).

Verificar siempre con `wc -l` que el archivo tenga el largo esperado antes de commitear.

---

## 10. Trampas conocidas (ahorran mucho tiempo)

| Síntoma | Causa y solución |
|---|---|
| Terminal muestra `heredoc>` o `quote>` | Bloque sin cerrar. **Ctrl+C**. Evitar heredocs largos: mejor entregar el archivo para descargar. |
| HTML se ve como texto plano en el navegador | TextEdit lo guardó como RTF/HTML enriquecido. **Usar el editor web de GitHub**, que nunca falló. |
| `cp` copia una versión vieja | macOS renombra descargas repetidas: `archivo 2.html`, `app-oauth-2.py`. Verificar con `ls ~/Downloads/nombre*`. |
| Error 404 de Drive con `%0A` en el ID | Se coló un salto de línea al pegar en Render. Reescribir el valor a mano. |
| `Address already in use: Port 5050` | Proceso viejo vivo (solo modo local): `lsof -ti:5050 \| xargs kill -9` |
| `print()` no aparece en el log | Buffer de Python. Se resolvió con `python3 -u` en `start.sh`. |
| Pegar bloques largos en `nano` se trunca | Pegar en dos partes o usar GitHub. |

**Método más confiable para entregar archivos a Nicolás:** generarlos con `create_file` y compartirlos con `present_files` para que los descargue, con un **nombre único** cada vez (ej. `app-v3.py`, no `app.py`) para evitar el problema de los duplicados de macOS.

---

## 11. Ideas pendientes / no implementadas

- Moderación previa (los saludos hoy salen directo a pantalla)
- Segunda instancia con Drive propio para el compañero (hoy es efímera)
- Limpieza automática de Drive al terminar el evento
- Dominio propio en vez de `*.onrender.com`
