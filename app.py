import os
import io
import uuid
import subprocess
import tempfile
import base64
import json
import mimetypes
from pathlib import Path
from typing import Optional

from flask import Flask, request, jsonify, render_template, send_from_directory, abort
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pillow_heif = None  # Si no está instalado, simplemente no se podrá procesar HEIC

app = Flask(__name__)

# ---- Configuración por evento -------------------------------------------
# Editá estas 3 líneas para cada fiesta (o pasalas como variables de entorno,
# ver el LaunchAgent .plist incluido).
EVENT_NAME = os.environ.get("EVENT_NAME", "Compartí tus fotos")
PORT = int(os.environ.get("PORT", 5050))


def _detect_google_drive_folder():
    """Encuentra la carpeta real de Google Drive Desktop (CloudStorage),
    sin depender del email de la cuenta ni del idioma del sistema."""
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
# --------------------------------------------------------------------------

PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# ---- Google Drive vía API (para cuando corre en la nube, sin Drive Desktop) ----
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
        print("[drive] conectado vía Service Account (modo nube).")
    except Exception:
        import traceback
        traceback.print_exc()
        print("[drive] no se pudo inicializar la API de Drive, se usará la carpeta local.")


def save_original(name: str, data: bytes) -> None:
    """Guarda el archivo original: por la API de Drive si hay credenciales
    configuradas (modo nube), o en la carpeta local sincronizada con Drive
    Desktop (modo Mac). Nunca revienta el upload si Drive falla."""
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
            print(f"[drive] falló la subida por API de {name!r}, se guarda localmente como respaldo.")

    with open(PHOTOS_DIR / name, "wb") as f:
        f.write(data)
# ---------------------------------------------------------------------------------

# Copia liviana usada solo por el loop de OBS/vMix — no se sincroniza a Drive.
CACHE_DIR = Path(os.environ.get("EVENTPHOTOS_CACHE", "/tmp/eventphotos_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {
    "jpg", "jpeg", "png", "heic", "heif", "webp",
    "dng", "cr2", "cr3", "nef", "arw", "orf", "raf", "rw2",  # RAW: se respaldan pero sin preview
}
PREVIEWABLE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "webp"}
GOLD_BORDER_RGB = (232, 196, 104)  # mismo dorado de la página de subida
BORDER_RATIO = 0.012  # ~1.2% del lado más largo de la foto
MIN_BORDER_PX = 10


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def add_gold_border(img):
    border = max(MIN_BORDER_PX, int(max(img.size) * BORDER_RATIO))
    return ImageOps.expand(img, border=border, fill=GOLD_BORDER_RGB)


def convert_raw_to_jpeg_bytes(original_bytes: bytes, ext: str) -> bytes:
    """Convierte RAW/DNG a JPEG con `sips` (nativo de macOS), para poder
    generar una preview aunque Pillow no sepa leer el formato original."""
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
    """Guarda el archivo original SIN tocar (calidad completa, se sincroniza
    a Drive) y genera una copia para el loop con borde dorado. Los RAW se
    convierten con sips para poder mostrarlos igual (a menor calidad)."""
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    uid = uuid.uuid4().hex
    original_bytes = file_storage.stream.read()

    # 1) Original intacto -> Drive (API en la nube, o carpeta local con Mac)
    save_original(f"{uid}.{ext}", original_bytes)

    # 2) Preview para el loop (RAW se convierte primero con sips)
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
        print(f"[upload] guardado como respaldo, sin preview de loop ({ext}): {file_storage.filename!r} — {e}")
        return "sin-preview"


@app.route("/")
def upload_page():
    return render_template("upload.html", event_name=EVENT_NAME)


@app.route("/loop")
def loop_page():
    return render_template("loop.html", event_name=EVENT_NAME)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("photos")
    print(f"[upload] recibidos: {[f.filename for f in files]}")
    if not files:
        return jsonify(ok=False, error="Sin archivos"), 400

    saved = []
    for f in files:
        try:
            name = save_upload(f)
            if name:
                saved.append(name)
            else:
                print(f"[upload] descartado (extensión no reconocida): {f.filename!r}")
        except Exception:
            import traceback
            traceback.print_exc()
            continue

    if not saved:
        return jsonify(ok=False, error="No se pudo procesar ninguna imagen"), 400
    return jsonify(ok=True, saved=saved)


@app.route("/api/photos")
def api_photos():
    files = [p for p in CACHE_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return jsonify(photos=[f"/photos/{p.name}" for p in files])


@app.route("/photos/<path:filename>")
def serve_photo(filename):
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    return send_from_directory(CACHE_DIR, filename)


if __name__ == "__main__":
    print(f"Carpeta de fotos:   {PHOTOS_DIR}")
    print(f"Subida (invitados): http://0.0.0.0:{PORT}/")
    print(f"Loop (OBS/vMix):    http://0.0.0.0:{PORT}/loop")
    app.run(host="0.0.0.0", port=PORT, debug=False)
