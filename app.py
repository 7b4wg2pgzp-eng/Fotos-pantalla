import os
import io
import time
import uuid
import json
import base64
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

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
GDRIVE_SA_JSON_B64 = os.environ.get("GDRIVE_SERVICE_ACCOUNT_B64")

_drive_service = None
if GDRIVE_SA_JSON_B64:
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


def save_original(name: str, data: bytes) -> None:
    if _drive_service:
        try:
            from googleapiclient.http import MediaIoBaseUpload
            metadata = {"name": name}
            if GDRIVE_FOLDER_ID:
                metadata["parents"] = [GDRIVE_FOLDER_ID]
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


def save_message(nombre: str, texto: str) -> Optional[dict]:
    texto = (texto or "").strip()[:MAX_MESSAGE_LEN]
    nombre = (nombre or "").strip()[:MAX_NAME_LEN]
    if not texto:
        return None

    entry = {"nombre": nombre, "texto": texto, "ts": time.time()}
    with _messages_lock:
        mensajes = load_messages()
        mensajes.append(entry)
        tmp = MESSAGES_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mensajes, f, ensure_ascii=False)
        tmp.replace(MESSAGES_FILE)

    # Respaldo del saludo en Drive, para que quede junto a las fotos.
    try:
        contenido = f"{nombre or 'Anonimo'}\n\n{texto}\n"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        save_original(f"saludo-{stamp}-{uuid.uuid4().hex[:6]}.txt", contenido.encode("utf-8"))
    except Exception:
        import traceback
        traceback.print_exc()

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
        img.save(CACHE_DIR / cache_name, "JPEG", quality=92)
        return cache_name
    except Exception as e:
        print(f"[upload] guardado como respaldo, sin preview de loop ({ext}): {file_storage.filename!r} - {e}")
        return "sin-preview"


@app.route("/")
def upload_page():
    return render_template("upload.html", event_name=EVENT_NAME)


@app.route("/loop")
def loop_page():
    return render_template("loop.html", event_name=EVENT_NAME)


@app.route("/saludos")
def saludos_page():
    return render_template("saludos.html", event_name=EVENT_NAME)


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
    files = [p for p in CACHE_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"]
    files.sort(key=lambda p: p.stat().st_mtime)
    return jsonify(photos=[f"/photos/{p.name}" for p in files])


@app.route("/api/mensajes")
def api_mensajes():
    return jsonify(mensajes=load_messages())


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    return send_from_directory(CACHE_DIR, filename)



def _check_admin():
    key = request.args.get("key") or request.form.get("key") or ""
    return bool(ADMIN_KEY) and key == ADMIN_KEY


@app.route("/admin")
def admin_page():
    if not ADMIN_KEY:
        return "Falta configurar ADMIN_KEY en Render", 500
    if not _check_admin():
        return "Clave incorrecta", 403
    return render_template("admin.html", event_name=EVENT_NAME, admin_key=ADMIN_KEY)


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
        tmp = MESSAGES_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(quedan, f, ensure_ascii=False)
        tmp.replace(MESSAGES_FILE)
    return jsonify(ok=True)


if __name__ == "__main__":
    print(f"Carpeta de fotos:   {PHOTOS_DIR}")
    print(f"Subida (invitados): http://0.0.0.0:{PORT}/")
    print(f"Loop fotos (OBS):   http://0.0.0.0:{PORT}/loop")
    print(f"Saludos (VJ):       http://0.0.0.0:{PORT}/saludos")
    app.run(host="0.0.0.0", port=PORT, debug=False)
