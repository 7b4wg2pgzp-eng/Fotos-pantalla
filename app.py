import os
import io
import time
import uuid
import json
import hmac
import base64
import threading
import mimetypes
import encodings.idna  # Importar antes de iniciar el hilo de restauración de Drive.
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from delivery import Delivery, parse_date, photo_uid
from event_source import EventSource
from gallery import Gallery, register_gallery

from flask import Flask, g, request, jsonify, render_template, send_from_directory, abort, redirect
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

app = Flask(__name__)
# Tope por pedido. Configurable con MAX_UPLOAD_MB. Un archivo enorme dejaba
# trabado al único worker del plan gratuito.
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "40")) * 1024 * 1024

# Cuántos archivos acepta cada persona en 10 minutos. La IP es la que manda
# Cloudflare (CF-Connecting-IP); si no está, se usa la del pedido.
UPLOAD_LIMIT = int(os.environ.get("UPLOAD_LIMIT_10MIN", "40"))
UPLOAD_WINDOW_SECONDS = 600
_upload_hits = {}
_upload_hits_lock = threading.Lock()


def _client_ip():
    header = (request.headers.get("CF-Connecting-IP") or "").strip()
    if header:
        return header.split(",")[0].strip()
    return request.remote_addr or "?"


def _rate_limited(n_files: int) -> bool:
    now = time.time()
    ip = _client_ip()
    counted = max(int(n_files), 1)
    with _upload_hits_lock:
        hits = [t for t in _upload_hits.get(ip, []) if now - t < UPLOAD_WINDOW_SECONDS]
        if len(hits) + counted > UPLOAD_LIMIT:
            _upload_hits[ip] = hits
            return True
        hits.extend([now] * counted)
        _upload_hits[ip] = hits
        if len(_upload_hits) > 5000:
            _upload_hits.clear()
        return False


@app.errorhandler(413)
def demasiado_grande(_e):
    return jsonify(ok=False, error="El archivo es muy pesado para subirlo por acá."), 413


@app.route("/health")
def health():
    # Para el health check de Render. No consulta Drive ni el evento.
    return jsonify(ok=True)


@app.before_request
def redirect_legacy_pages():
    """Migrar las páginas de Nico sin redirigir APIs ni envíos POST."""
    if (request.host.split(":", 1)[0].lower() == "fotospantalla.nicovasquezdjs.com"
            and request.method in {"GET", "HEAD"}
            and request.path in {"/", "/loop", "/saludos", "/admin"}):
        destino = "https://fotospantalla.nicovasquez.com.ar" + request.full_path
        return redirect(destino.rstrip("?") if not request.query_string else destino, code=301)


EVENT_NAME = os.environ.get("EVENT_NAME", "Compartí tus fotos")

# ---- Marca ----
# La misma aplicación sirve a más de un cliente. Lo único que cambia entre uno
# y otro es el título, la firma y el logo, así que sale de variables de entorno
# y no de una copia del código: una rama aparte se queda vieja sola y las
# mejoras dejan de llegarle.
# Los valores por defecto son los de Nico Vásquez, o sea que un servicio que no
# defina nada sigue viéndose exactamente igual que antes.
MARCA = {
    "titulo": os.environ.get("MARCA_TITULO", "Álbum Interactivo"),
    "duenio": os.environ.get("MARCA_DUENIO", "Nico Vásquez"),
    "bajada": os.environ.get("MARCA_BAJADA", "Show Audiovisual para Eventos"),
    "logo":   os.environ.get("MARCA_LOGO",   "logo.webp"),
    # Vacío = no se muestra el botón "Salir en vivo".
    "vivo_url": os.environ.get("MARCA_VIVO_URL", "https://camara.nicovasquez.com.ar/"),
}
PORT = int(os.environ.get("PORT", 5050))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")


def _detect_google_drive_folder():
    base = Path.home() / "Library" / "CloudStorage"
    if not base.exists():
        return None
    for entry in base.iterdir():
        if entry.is_dir() and entry.name.startswith("GoogleDrive-"):
            for sub in ("My Drive", "Mi unidad"):
                candidate = entry / sub
                if candidate.exists():
                    return candidate
    return None


_gdrive = _detect_google_drive_folder()
_default_photos_dir = (_gdrive / "EventPhotos") if _gdrive else (Path.home() / "EventPhotos")

PHOTOS_DIR = Path(os.environ.get("EVENTPHOTOS_DIR", str(_default_photos_dir)))
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
GDRIVE_SA_JSON_B64 = os.environ.get("GDRIVE_SERVICE_ACCOUNT_B64")

# OAuth con cuenta personal: usa TU cuota de Drive (las Service Accounts no tienen).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()

_drive_service = None

if GOOGLE_REFRESH_TOKEN and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        _credentials = Credentials(
            None,
            refresh_token=GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        _drive_service = build("drive", "v3", credentials=_credentials)
        print("[drive] conectado via OAuth (cuenta personal).")
    except Exception:
        import traceback
        traceback.print_exc()
        print("[drive] fallo el OAuth, se probara con Service Account.")

if _drive_service is None and GDRIVE_SA_JSON_B64:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        _creds_info = json.loads(base64.b64decode(GDRIVE_SA_JSON_B64))
        _credentials = service_account.Credentials.from_service_account_info(
            _creds_info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        _drive_service = build("drive", "v3", credentials=_credentials)
        print("[drive] conectado via Service Account (modo nube).")
    except Exception:
        import traceback
        traceback.print_exc()
        print("[drive] no se pudo inicializar la API de Drive, se usara la carpeta local.")


_subcarpetas = {}
_subcarpetas_lock = threading.Lock()


def get_subfolder(nombre: str):
    """Devuelve el id de una subcarpeta dentro de la carpeta del evento,
    creándola la primera vez. Cachea el resultado."""
    if not _drive_service or not GDRIVE_FOLDER_ID:
        return GDRIVE_FOLDER_ID or None
    with _subcarpetas_lock:
        if nombre in _subcarpetas:
            return _subcarpetas[nombre]
        try:
            q = ("name = '" + nombre + "' and mimeType = 'application/vnd.google-apps.folder'"
                 " and trashed = false and '" + GDRIVE_FOLDER_ID + "' in parents")
            res = _drive_service.files().list(q=q, fields="files(id)", pageSize=1).execute()
            archivos = res.get("files", [])
            if archivos:
                fid = archivos[0]["id"]
            else:
                meta = {
                    "name": nombre,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [GDRIVE_FOLDER_ID],
                }
                fid = _drive_service.files().create(body=meta, fields="id").execute()["id"]
                print(f"[drive] subcarpeta creada: {nombre}")
            _subcarpetas[nombre] = fid
            return fid
        except Exception:
            import traceback
            traceback.print_exc()
            return GDRIVE_FOLDER_ID


_delivery = Delivery(_drive_service, GDRIVE_FOLDER_ID,
                     lambda: get_subfolder("Datos"),
                     "entrega-" + (os.environ.get("RENDER_SERVICE_ID") or "local") + ".json")


_gallery = Gallery(_delivery, os.environ.get("GALLERY_LINK_SECRET", ""))
_delivery.gallery = _gallery
register_gallery(app, _gallery)


def _update_delivery(kind, value):
    try:
        if kind == "photo":
            _delivery.on_photo(value)
        else:
            _delivery.on_messages(value)
    except Exception:
        _delivery.last_error = "Hay archivos pendientes de copiar. Verificá Drive y completá la entrega."
        print("[entrega] copia pendiente; original conservado.")


def _drive_find_id(name: str, parent_id=None):
    """Devuelve el id del archivo con ese nombre en la carpeta indicada."""
    if not _drive_service:
        return None
    try:
        parent = parent_id or GDRIVE_FOLDER_ID
        q = "name = '" + name.replace("'", "") + "' and trashed = false"
        if parent:
            q += " and '" + parent + "' in parents"
        res = _drive_service.files().list(q=q, fields="files(id)", pageSize=1).execute()
        archivos = res.get("files", [])
        return archivos[0]["id"] if archivos else None
    except Exception:
        import traceback
        traceback.print_exc()
        return None


def drive_upsert(name: str, data: bytes, parent_id=None) -> None:
    """Crea o reemplaza un archivo en Drive (para mensajes.json y previews)."""
    if not _drive_service:
        return
    try:
        from googleapiclient.http import MediaIoBaseUpload
        parent = parent_id or GDRIVE_FOLDER_ID
        mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
        existente = _drive_find_id(name, parent)
        if existente:
            _drive_service.files().update(fileId=existente, media_body=media).execute()
        else:
            metadata = {"name": name}
            if parent:
                metadata["parents"] = [parent]
            _drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
    except Exception:
        import traceback
        traceback.print_exc()


def drive_download(file_id: str) -> Optional[bytes]:
    if not _drive_service:
        return None
    try:
        return _drive_service.files().get_media(fileId=file_id).execute()
    except Exception:
        import traceback
        traceback.print_exc()
        return None


def save_original(name: str, data: bytes) -> None:
    if _drive_service:
        try:
            from googleapiclient.http import MediaIoBaseUpload
            metadata = {"name": name}
            destino = get_subfolder("Originales")
            if destino:
                metadata["parents"] = [destino]
            mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
            original = _drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
            _update_delivery("photo", original["id"])
            return
        except Exception:
            import traceback
            traceback.print_exc()
            print(f"[drive] fallo la subida por API de {name!r}, se guarda localmente.")

    with open(PHOTOS_DIR / name, "wb") as f:
        f.write(data)
    # El original local es recuperable, pero Render Free no garantiza su persistencia.
    # No confirmar una carga como exitosa si el respaldo en Drive falló.
    raise RuntimeError("No se pudo respaldar el original en Drive.")


CACHE_DIR = Path(os.environ.get("EVENTPHOTOS_CACHE", "/tmp/eventphotos_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {
    "jpg", "jpeg", "png", "heic", "heif", "webp",
    "dng", "cr2", "cr3", "nef", "arw", "orf", "raf", "rw2",
}
PREVIEWABLE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp"}
GOLD_BORDER_RGB = (232, 196, 104)
PREVIEW_MAX_PX = int(os.environ.get("PREVIEW_MAX_PX", "1920"))
PREVIEW_QUALITY = int(os.environ.get("PREVIEW_QUALITY", "82"))
BORDER_RATIO = 0.012
MIN_BORDER_PX = 10

# Cuánto tiempo siguen apareciendo en pantalla las fotos y saludos (horas).
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", 12))
RETENTION_SECONDS = RETENTION_HOURS * 3600

# ---- Saludos -------------------------------------------------------------
MESSAGES_FILE = CACHE_DIR / "mensajes.json"
MAX_NAME_LEN = 40
MAX_MESSAGE_LEN = 280
_messages_lock = threading.Lock()


def load_messages():
    try:
        with open(MESSAGES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_messages(mensajes) -> None:
    """Guarda la lista local y la sincroniza a Drive, para sobrevivir reinicios."""
    tmp = MESSAGES_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mensajes, f, ensure_ascii=False)
    tmp.replace(MESSAGES_FILE)
    try:
        drive_upsert("mensajes.json",
                     json.dumps(mensajes, ensure_ascii=False).encode("utf-8"),
                     get_subfolder("Datos"))
        _update_delivery("messages", mensajes)
        stored = _drive_find_id("mensajes.json", get_subfolder("Datos"))
        if not stored or drive_download(stored) != json.dumps(mensajes, ensure_ascii=False).encode("utf-8"):
            raise RuntimeError("No se pudo verificar el respaldo de los saludos.")
    except Exception:
        import traceback
        traceback.print_exc()
        raise


def save_message(nombre: str, texto: str) -> Optional[dict]:
    texto = (texto or "").strip()[:MAX_MESSAGE_LEN]
    nombre = (nombre or "").strip()[:MAX_NAME_LEN]
    if not texto:
        return None

    entry = {"nombre": nombre, "texto": texto, "ts": time.time()}
    with _messages_lock:
        mensajes = load_messages()
        mensajes.append(entry)
        _write_messages(mensajes)

    return entry
# ---------------------------------------------------------------------------


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def add_gold_border(img):
    border = max(MIN_BORDER_PX, int(max(img.size) * BORDER_RATIO))
    return ImageOps.expand(img, border=border, fill=GOLD_BORDER_RGB)


def convert_raw_to_jpeg_bytes(original_bytes: bytes, ext: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as src:
        src.write(original_bytes)
        src_path = src.name
    dst_path = src_path + ".jpg"
    try:
        subprocess.run(
            ["sips", "-s", "format", "jpeg", src_path, "--out", dst_path],
            check=True, capture_output=True, timeout=30,
        )
        with open(dst_path, "rb") as f:
            return f.read()
    finally:
        for p in (src_path, dst_path):
            if os.path.exists(p):
                os.remove(p)


def _preview_jpeg(original_bytes: bytes) -> bytes:
    """Arma la JPEG del loop. Si Pillow no puede abrirla, no es una imagen."""
    with Image.open(io.BytesIO(original_bytes)) as probe:
        probe.verify()
    with Image.open(io.BytesIO(original_bytes)) as source:
        source.draft("RGB", (PREVIEW_MAX_PX, PREVIEW_MAX_PX))
        img = ImageOps.exif_transpose(source)
        img = img.convert("RGB")
        # La pantalla no necesita 12 MP: 1920 px de lado alcanza para el loop.
        img.thumbnail((PREVIEW_MAX_PX, PREVIEW_MAX_PX), Image.Resampling.LANCZOS)
        img = add_gold_border(img)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=PREVIEW_QUALITY, optimize=True, progressive=True)
        return buf.getvalue()


def save_upload(file_storage) -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    uid = uuid.uuid4().hex
    original_bytes = file_storage.stream.read()

    # La extensión sola no alcanza: un texto renombrado a .jpg no tiene que
    # llegar a Drive. Primero se valida y se arma la preview.
    preview_data = None
    if ext in PREVIEWABLE_EXT:
        try:
            preview_data = _preview_jpeg(original_bytes)
        except Exception:
            print(f"[upload] descartado (no es una imagen válida): {file_storage.filename!r}")
            return None

    save_original(f"{uid}.{ext}", original_bytes)

    if preview_data is None:
        try:
            preview_data = _preview_jpeg(convert_raw_to_jpeg_bytes(original_bytes, ext))
        except Exception as e:
            print(f"[upload] guardado como respaldo, sin preview de loop ({ext}): {file_storage.filename!r} - {e}")
            return "sin-preview"

    cache_name = f"{uid}.jpg"
    with open(CACHE_DIR / cache_name, "wb") as f:
        f.write(preview_data)
    # Copia chica en Drive para poder rearmar el loop si el server reinicia.
    drive_upsert(f"preview-{uid}.jpg", preview_data, get_subfolder("Previews"))
    return cache_name


@app.route("/")
def upload_page():
    return render_template("upload.html", event_name=app.config.get("CURRENT_EVENT_NAME", EVENT_NAME), marca=MARCA)


@app.route("/loop")
def loop_page():
    return render_template("loop.html", event_name=app.config.get("CURRENT_EVENT_NAME", EVENT_NAME), marca=MARCA)


@app.route("/saludos")
def saludos_page():
    return render_template("saludos.html", event_name=app.config.get("CURRENT_EVENT_NAME", EVENT_NAME), marca=MARCA)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    nombre = request.form.get("nombre", "")
    mensaje = request.form.get("mensaje", "")
    print(f"[upload] fotos={[f.filename for f in files]} nombre={nombre!r} mensaje={bool(mensaje.strip())}")

    if not files and not mensaje.strip():
        return jsonify(ok=False, error="Manda al menos una foto o un saludo"), 400
    if _rate_limited(len(files)):
        return jsonify(ok=False, error="Mandaste muchas fotos seguidas. Esperá unos minutos y probá de nuevo."), 429

    saved = []
    fallo_guardado = False
    for f in files:
        try:
            name = save_upload(f)
            if name:
                saved.append(name)
            else:
                print(f"[upload] descartado (extension no reconocida o no es imagen): {f.filename!r}")
        except Exception:
            fallo_guardado = True
            import traceback
            traceback.print_exc()
            continue

    if files and not saved:
        # El saludo no se guarda en este pedido: el celular va a reintentar la foto
        # y el saludo viaja con ella. Así no queda duplicado.
        if fallo_guardado:
            return jsonify(ok=False, error="No se pudo guardar la foto. Probá de nuevo."), 503
        return jsonify(ok=False, error="Ese archivo no es una imagen que podamos mostrar."), 400

    mensaje_guardado = False
    try:
        if save_message(nombre, mensaje):
            mensaje_guardado = True
    except Exception:
        import traceback
        traceback.print_exc()

    if not saved and not mensaje_guardado:
        return jsonify(ok=False, error="No se pudo procesar el envio"), 400
    return jsonify(ok=True, saved=saved, mensaje=mensaje_guardado, entrega_pendiente=bool(_delivery.last_error))


@app.route("/api/photos")
def api_photos():
    with _event_lock:
        try:
            vetoes = _photo_vetoes()
        except Exception:
            return jsonify(ok=False, photos=[], error="No se pudo verificar la moderación."), 503
        corte = time.time() - RETENTION_SECONDS
        files = [
            p for p in CACHE_DIR.iterdir()
            if p.is_file() and p.suffix.lower() == ".jpg" and p.stat().st_mtime >= corte
            and photo_uid(p.name) not in vetoes
        ]
        files.sort(key=lambda p: p.stat().st_mtime)
        return jsonify(photos=[f"/photos/{p.name}" for p in files])


@app.route("/api/mensajes")
def api_mensajes():
    corte = time.time() - RETENTION_SECONDS
    return jsonify(mensajes=[m for m in load_messages() if m.get("ts", 0) >= corte])


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    with _event_lock:
        try:
            vetoed = photo_uid(filename) in _photo_vetoes()
        except Exception:
            abort(503)
        if vetoed:
            abort(404)
        response = send_from_directory(CACHE_DIR, filename)
        # El nombre es un UUID: el contenido no cambia. El navegador del loop
        # puede guardarla. Si se modera, deja de salir en /api/photos.
        response.headers['Cache-Control'] = 'private, max-age=43200, immutable'
        return response


def _photo_vetoes():
    local = {p.stem.removeprefix('moderation-') for p in CACHE_DIR.glob('moderation-*.json')}
    if _drive_service:
        local.update(_delivery.vetoes())
    return local


# ---- Clave del moderador ----
# Sale solo de ADMIN_KEY, en el entorno del servicio. Antes, si esa variable no
# estaba, la primera persona que abría /admin elegía la clave desde la web.
# Sin la variable el moderador no abre y nadie puede inventar una clave.


def _check_admin():
    clave = request.args.get("key") or request.form.get("key") or ""
    if not ADMIN_KEY or not clave:
        return False
    return hmac.compare_digest(clave, ADMIN_KEY)


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    if not ADMIN_KEY:
        return (
            "El moderador todavía no tiene clave. Configurá ADMIN_KEY en el entorno del servicio y volvé a entrar.",
            403,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    if not _check_admin():
        intento = bool(request.args.get("key") or request.form.get("key"))
        return render_template("admin_login.html", error=intento), (403 if intento else 200)

    clave = request.args.get("key") or request.form.get("key") or ADMIN_KEY
    return render_template("admin.html", event_name=app.config.get("CURRENT_EVENT_NAME", EVENT_NAME), marca=MARCA, admin_key=clave)


@app.route("/api/admin/entrega", methods=["GET", "POST"])
def admin_entrega():
    if not _check_admin():
        return jsonify(ok=False), 403
    try:
        agenda = _event_source.status()
        if request.method == "POST":
            config = _event_source.ensure(load_messages())
            if config:
                _delivery.reconcile(load_messages())
        state = _delivery.status()
        gallery_url = _gallery.link(state["config"]) if state.get("config") else None
        return jsonify(ok=True, agenda=agenda, gallery_url=gallery_url, **state)
    except Exception:
        return jsonify(ok=False, error="No se pudo verificar Abrolista o Drive. No se limpiaron los archivos; revisá las conexiones antes del evento."), 503


@app.route("/api/admin/borrar-foto", methods=["POST"])
def admin_borrar_foto():
    if not _check_admin():
        return jsonify(ok=False), 403
    nombre = request.form.get("nombre", "")
    uid = photo_uid(nombre)
    if not uid or nombre != uid + '.jpg':
        return jsonify(ok=False), 400
    with _event_lock:
        try:
            # Mantener oculto incluso si Drive falla antes de persistir el veto.
            marker = CACHE_DIR / ('moderation-' + uid + '.json')
            temporary = marker.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(uid=uid)), encoding='utf-8')
            temporary.replace(marker)
            (CACHE_DIR / nombre).unlink(missing_ok=True)
            result = _delivery.moderate_photo(uid)
            for original in PHOTOS_DIR.glob(uid + '.*'):
                if original.is_file():
                    original.unlink()
            return jsonify(ok=True, **result)
        except Exception:
            import traceback
            traceback.print_exc()
            return jsonify(ok=False, pending=True,
                           error="No se completó la eliminación o su veto persistente en Drive. Volvé a intentar."), 503


@app.route("/api/admin/borrar-mensaje", methods=["POST"])
def admin_borrar_mensaje():
    if not _check_admin():
        return jsonify(ok=False), 403
    ts = request.form.get("ts", "")
    with _messages_lock:
        quedan = [m for m in load_messages() if str(m.get("ts")) != ts]
        _write_messages(quedan)
    return jsonify(ok=True)


def _restore_from_drive():
    """Al arrancar, rearma el cache desde Drive: los saludos y las previews
    de las últimas RETENTION_HOURS. Así un reinicio de Render no vacía la
    pantalla en medio del evento."""
    if not _drive_service:
        return
    try:
        # 1) Saludos
        mid = _drive_find_id("mensajes.json", get_subfolder("Datos"))
        if mid and not MESSAGES_FILE.exists():
            datos = drive_download(mid)
            if datos:
                with open(MESSAGES_FILE, "wb") as f:
                    f.write(datos)
                print("[restore] saludos recuperados de Drive.")

        # 2) Previews de fotos
        vetoes = _photo_vetoes()
        corte = time.time() - RETENTION_SECONDS
        carpeta_previews = get_subfolder("Previews")
        q = "name contains 'preview-' and trashed = false"
        if carpeta_previews:
            q += " and '" + carpeta_previews + "' in parents"
        token, recuperadas = None, 0
        while True:
            res = _drive_service.files().list(
                q=q, fields="nextPageToken, files(id,name,createdTime)",
                pageSize=200, pageToken=token,
            ).execute()
            for archivo in res.get("files", []):
                nombre = archivo["name"]
                if not nombre.startswith("preview-"):
                    continue
                if photo_uid(nombre) in vetoes:
                    continue
                creado = archivo.get("createdTime", "")
                try:
                    # Drive devuelve UTC con zona horaria: no interpretarlo como hora local.
                    ts = parse_date(creado).timestamp()
                except (ValueError, TypeError, KeyError):
                    # Sin fecha fiable, no restaurar una foto vieja como si fuera nueva.
                    continue
                if ts < corte:
                    continue
                destino = CACHE_DIR / nombre.replace("preview-", "", 1)
                if destino.exists():
                    continue
                datos = drive_download(archivo["id"])
                if datos:
                    with open(destino, "wb") as f:
                        f.write(datos)
                    os.utime(destino, (ts, ts))
                    recuperadas += 1
            token = res.get("nextPageToken")
            if not token:
                break
        if recuperadas:
            print(f"[restore] {recuperadas} fotos recuperadas de Drive.")
    except Exception:
        import traceback
        traceback.print_exc()


_event_lock = threading.RLock()


def _reset_event_runtime(config, previous):
    # Sólo las carpetas técnicas; nunca Originales, Entregas ni las claves del administrador.
    for name in ("Previews", "Datos"):
        folder = _delivery.find_folder(GDRIVE_FOLDER_ID, name)
        if not folder:
            continue
        for file in _delivery.list_files(folder["id"]):
            eligible = name == "Previews" and file["name"].startswith("preview-")
            eligible = eligible or (name == "Datos" and file["name"] == "mensajes.json")
            if eligible:
                if name == "Previews" and parse_date(file["createdTime"]) >= parse_date(config["start"]):
                    continue
                # Papelera de Drive: recuperable; el original y la entrega siguen guardados.
                _drive_service.files().update(fileId=file["id"], body={"trashed": True}, fields="id").execute()
    for file in CACHE_DIR.glob("*.jpg"):
        if file.stat().st_mtime < parse_date(config["start"]).timestamp():
            file.unlink()
    if MESSAGES_FILE.exists():
        remaining = [m for m in load_messages() if m.get("ts", 0) >= parse_date(config["start"]).timestamp()]
        # Escribir directo para no actualizar la entrega de la fiesta anterior.
        MESSAGES_FILE.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")


def _persistent_event_messages():
    """Leer el respaldo vigente; ante un fallo no habilitar archivo ni limpieza."""
    folder = _delivery.find_folder(GDRIVE_FOLDER_ID, "Datos")
    rows = _delivery.list_files(folder["id"], "name = 'mensajes.json'") if folder else []
    if len(rows) > 1:
        raise RuntimeError("Hay más de un archivo de saludos temporales.")
    persisted = json.loads(_drive_service.files().get_media(fileId=rows[0]["id"]).execute()) if rows else []
    if not isinstance(persisted, list) or any(not isinstance(m, dict) or "ts" not in m for m in persisted):
        raise RuntimeError("El respaldo de saludos no es válido.")
    combined = {json.dumps(m, sort_keys=True, ensure_ascii=False): m for m in persisted + load_messages()}
    return list(combined.values())


def _close_event_runtime(config):
    """Retira archivos temporales al finalizar una fiesta ya archivada.

    Conserva Entregas y los archivos de autor de la galería.
    Los archivos técnicos se envían a Papelera (recuperables).
    """
    cutoff = parse_date(config["end"]).timestamp()
    previews = _delivery.find_folder(GDRIVE_FOLDER_ID, "Previews")
    if previews:
        for item in _delivery.list_files(previews["id"]):
            if not item["name"].startswith("preview-"):
                continue
            # No eliminar fotos de un evento posterior.
            if parse_date(item["createdTime"]).timestamp() < cutoff:
                _drive_service.files().update(
                    fileId=item["id"], body={"trashed": True}, fields="id"
                ).execute()

    # Los saludos del evento fueron copiados a Entregas antes de llamar aquí.
    remaining = [m for m in _persistent_event_messages() if float(m.get("ts", 0)) >= cutoff]
    data_folder = _delivery.find_folder(GDRIVE_FOLDER_ID, "Datos")
    if data_folder:
        existing = _delivery.list_files(data_folder["id"], "name = 'mensajes.json'")
        if len(existing) > 1:
            raise RuntimeError("Hay más de un archivo de saludos temporales.")
        if remaining:
            payload = json.dumps(remaining, ensure_ascii=False).encode("utf-8")
            saved = _delivery.write(data_folder["id"], "mensajes.json", payload, "application/json")
            if _drive_service.files().get_media(fileId=saved["id"]).execute() != payload:
                raise RuntimeError("No se pudo confirmar la limpieza de saludos.")
        else:
            for item in existing:
                _drive_service.files().update(
                    fileId=item["id"], body={"trashed": True}, fields="id"
                ).execute()

    # Solo retirar originales después de verificar todas las copias de esta fiesta.
    _delivery.clean_event_originals(config)
    # Limpiar caché solo después de completar las operaciones en Drive.
    for item in CACHE_DIR.glob("*.jpg"):
        if item.stat().st_mtime < cutoff:
            item.unlink()
    MESSAGES_FILE.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
    print("[evento] cerrada entrega anterior; previews y saludos temporales depurados.")


_event_source = EventSource(_delivery, _reset_event_runtime, close_runtime=_close_event_runtime,
                            closure_messages=_persistent_event_messages)


@app.before_request
def isolate_eventphotos_event():
    if request.path not in {"/api/upload", "/api/photos", "/api/mensajes", "/", "/loop", "/saludos"}:
        return None
    _event_lock.acquire()
    g.eventphotos_lock = True
    try:
        with _event_lock:
            config = _event_source.ensure(load_messages())
            if not config:
                if request.path == "/api/upload":
                    return jsonify(ok=False, error="Todavía no hay un evento de Abrolista en curso."), 409
                if request.path == "/api/photos":
                    return jsonify(photos=[])
                if request.path == "/api/mensajes":
                    return jsonify(mensajes=[])
            else:
                # El título se obtiene de Abrolista, sin editar variables de Render por evento.
                app.config["CURRENT_EVENT_NAME"] = config["name"]
    except Exception:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error="No se pudo verificar el evento y su respaldo. Volvé a intentar."), 503


@app.teardown_request
def release_eventphotos_lock(error):
    if getattr(g, "eventphotos_lock", False):
        g.eventphotos_lock = False
        _event_lock.release()


def _restore_serialized():
    with _event_lock:
        _restore_from_drive()


threading.Thread(target=_restore_serialized, daemon=True).start()


if __name__ == "__main__":
    print(f"Carpeta de fotos:   {PHOTOS_DIR}")
    print(f"Subida (invitados): http://0.0.0.0:{PORT}/")
    print(f"Loop fotos (OBS):   http://0.0.0.0:{PORT}/loop")
    print(f"Saludos (VJ):       http://0.0.0.0:{PORT}/saludos")
    app.run(host="0.0.0.0", port=PORT, debug=False)



