import os
import io
import time
import uuid
import json
import hmac
import base64
import hashlib
import secrets
import threading
import mimetypes
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from flask import Flask, request, jsonify, render_template, send_from_directory, abort
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None

app = Flask(__name__)

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
    "vivo_url": os.environ.get("MARCA_VIVO_URL", "https://camara.nicovasquezdjs.com/"),
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
            _drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
            return
        except Exception:
            import traceback
            traceback.print_exc()
            print(f"[drive] fallo la subida por API de {name!r}, se guarda localmente.")

    with open(PHOTOS_DIR / name, "wb") as f:
        f.write(data)


CACHE_DIR = Path(os.environ.get("EVENTPHOTOS_CACHE", "/tmp/eventphotos_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {
    "jpg", "jpeg", "png", "heic", "heif", "webp",
    "dng", "cr2", "cr3", "nef", "arw", "orf", "raf", "rw2",
}
PREVIEWABLE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp"}
GOLD_BORDER_RGB = (232, 196, 104)
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
    except Exception:
        import traceback
        traceback.print_exc()


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


def save_upload(file_storage) -> Optional[str]:
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    uid = uuid.uuid4().hex
    original_bytes = file_storage.stream.read()

    save_original(f"{uid}.{ext}", original_bytes)

    try:
        preview_bytes = original_bytes if ext in PREVIEWABLE_EXT else convert_raw_to_jpeg_bytes(original_bytes, ext)
        img = Image.open(io.BytesIO(preview_bytes))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img = add_gold_border(img)

        cache_name = f"{uid}.jpg"
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=92)
        preview_data = buf.getvalue()
        with open(CACHE_DIR / cache_name, "wb") as f:
            f.write(preview_data)
        # Copia chica en Drive para poder rearmar el loop si el server reinicia.
        drive_upsert(f"preview-{uid}.jpg", preview_data, get_subfolder("Previews"))
        return cache_name
    except Exception as e:
        print(f"[upload] guardado como respaldo, sin preview de loop ({ext}): {file_storage.filename!r} - {e}")
        return "sin-preview"


@app.route("/")
def upload_page():
    return render_template("upload.html", event_name=EVENT_NAME, marca=MARCA)


@app.route("/loop")
def loop_page():
    return render_template("loop.html", event_name=EVENT_NAME, marca=MARCA)


@app.route("/saludos")
def saludos_page():
    return render_template("saludos.html", event_name=EVENT_NAME, marca=MARCA)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    nombre = request.form.get("nombre", "")
    mensaje = request.form.get("mensaje", "")
    print(f"[upload] fotos={[f.filename for f in files]} nombre={nombre!r} mensaje={bool(mensaje.strip())}")

    if not files and not mensaje.strip():
        return jsonify(ok=False, error="Manda al menos una foto o un saludo"), 400

    saved = []
    for f in files:
        try:
            name = save_upload(f)
            if name:
                saved.append(name)
            else:
                print(f"[upload] descartado (extension no reconocida): {f.filename!r}")
        except Exception:
            import traceback
            traceback.print_exc()
            continue

    mensaje_guardado = False
    try:
        if save_message(nombre, mensaje):
            mensaje_guardado = True
    except Exception:
        import traceback
        traceback.print_exc()

    if not saved and not mensaje_guardado:
        return jsonify(ok=False, error="No se pudo procesar el envio"), 400
    return jsonify(ok=True, saved=saved, mensaje=mensaje_guardado)


@app.route("/api/photos")
def api_photos():
    corte = time.time() - RETENTION_SECONDS
    files = [
        p for p in CACHE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() == ".jpg" and p.stat().st_mtime >= corte
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
    return send_from_directory(CACHE_DIR, filename)


# ---- Clave del moderador ----
# Se estrena como el PIN de un cajero: la primera persona que abre /admin elige
# la suya. Se guarda como hash (nunca la clave en claro) en la carpeta Datos de
# Drive, porque el disco de Render en plan gratuito es efimero y se borra en
# cada reinicio. El nombre del archivo lleva el id del servicio de Render, asi
# main y vj tienen cada uno su clave aunque compartan la carpeta de Drive.
ADMIN_STATE_NAME = "admin-" + (os.environ.get("RENDER_SERVICE_ID") or "local") + ".json"
ADMIN_STATE_FILE = CACHE_DIR / ADMIN_STATE_NAME
_admin_lock = threading.Lock()


def _admin_hash(clave: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", clave.encode(), salt.encode(), 200_000).hex()


def _admin_state():
    """El estado de la clave: primero el cache local, si no esta, Drive."""
    if ADMIN_STATE_FILE.exists():
        try:
            with open(ADMIN_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    fid = _drive_find_id(ADMIN_STATE_NAME, get_subfolder("Datos"))
    if fid:
        datos = drive_download(fid)
        if datos:
            try:
                estado = json.loads(datos)
            except Exception:
                return None
            with open(ADMIN_STATE_FILE, "wb") as f:
                f.write(datos)
            return estado
    return None


def _admin_set_key(clave: str) -> None:
    salt = secrets.token_hex(16)
    datos = json.dumps({
        "salt": salt,
        "hash": _admin_hash(clave, salt),
        "creada": int(time.time()),
    }).encode("utf-8")
    with _admin_lock:
        with open(ADMIN_STATE_FILE, "wb") as f:
            f.write(datos)
        drive_upsert(ADMIN_STATE_NAME, datos, get_subfolder("Datos"))


def _admin_configurada() -> bool:
    return bool(ADMIN_KEY) or _admin_state() is not None


def _check_admin():
    clave = request.args.get("key") or request.form.get("key") or ""
    if not clave:
        return False
    if ADMIN_KEY and hmac.compare_digest(clave, ADMIN_KEY):
        return True
    estado = _admin_state()
    if not estado:
        return False
    return hmac.compare_digest(_admin_hash(clave, estado["salt"]), estado["hash"])


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    # 1) Todavia no hay clave: la primera persona que entra elige la suya.
    if not _admin_configurada():
        if request.method == "POST":
            nueva = request.form.get("nueva", "")
            repetir = request.form.get("repetir", "")
            if len(nueva) < 6:
                return render_template(
                    "admin_estrenar.html",
                    error="La clave tiene que tener al menos 6 caracteres."), 400
            if nueva != repetir:
                return render_template(
                    "admin_estrenar.html", error="Las dos no coinciden."), 400
            _admin_set_key(nueva)
            return render_template("admin.html", event_name=EVENT_NAME,
                                   marca=MARCA, admin_key=nueva)
        return render_template("admin_estrenar.html", error=None)

    # 2) Ya hay clave: formulario de siempre.
    if not _check_admin():
        intento = bool(request.args.get("key") or request.form.get("key"))
        return render_template("admin_login.html", error=intento), (403 if intento else 200)

    clave = request.args.get("key") or request.form.get("key") or ADMIN_KEY
    return render_template("admin.html", event_name=EVENT_NAME, marca=MARCA, admin_key=clave)


@app.route("/api/admin/borrar-foto", methods=["POST"])
def admin_borrar_foto():
    if not _check_admin():
        return jsonify(ok=False), 403
    nombre = secure_filename(request.form.get("nombre", ""))
    if not nombre:
        return jsonify(ok=False), 400
    ruta = CACHE_DIR / nombre
    if ruta.exists():
        ruta.unlink()
    uid = nombre.rsplit(".", 1)[0]
    if _drive_service:
        try:
            res = _drive_service.files().list(
                q="name contains '" + uid + "'", fields="files(id,name)").execute()
            for f in res.get("files", []):
                _drive_service.files().delete(fileId=f["id"]).execute()
        except Exception:
            import traceback
            traceback.print_exc()
    else:
        for p in PHOTOS_DIR.glob(uid + ".*"):
            p.unlink()
    return jsonify(ok=True)


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
                creado = archivo.get("createdTime", "")
                try:
                    ts = time.mktime(time.strptime(creado[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    ts = time.time()
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


threading.Thread(target=_restore_from_drive, daemon=True).start()


if __name__ == "__main__":
    print(f"Carpeta de fotos:   {PHOTOS_DIR}")
    print(f"Subida (invitados): http://0.0.0.0:{PORT}/")
    print(f"Loop fotos (OBS):   http://0.0.0.0:{PORT}/loop")
    print(f"Saludos (VJ):       http://0.0.0.0:{PORT}/saludos")
    app.run(host="0.0.0.0", port=PORT, debug=False)
