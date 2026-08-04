import os
import io
import uuid
import json
import base64
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
            _creds_info, scopes=["https://www.googleap

cd ~/Downloads/"eventphotos 3"
cat > app.py << 'PYEOF'
import os
import io
import uuid
import json
import base64
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
                print(f"[upload] descartado (extension no reconocida): {f.filename!r}")
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
