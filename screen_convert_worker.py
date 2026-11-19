"""Worker del VPS: pasa a H.264 los videos de invitados que la pantalla no lee.

Corre en el Oracle (fotospantalla-convert.service), un trabajo por vez, con
nice, ffmpeg -threads 1 y memoria acotada para convivir con AbroLista.

Ciclo:
  1. Lee (solo lectura) Datos/pantalla-videos.json en Drive. Las filas con
     screen.state == "pending" son las que hay que convertir.
  2. Baja el original a disco, lo convierte a MP4 H.264 yuv420p (lado largo
     1280 por defecto, -preset veryfast, +faststart, AAC si hay audio).
  3. Sube la copia a "Videos pantalla H264" como <uid>.mp4. El original no se
     toca: sigue en "Videos pantalla" para el álbum y el moderador.
  4. Avisa a la app (POST /api/convert/<uid>/listo con el token compartido).
     La app valida la copia en Drive y es la única que escribe el registro,
     así que no hay carrera con sus propias escrituras.

Si no puede convertir después de MAX_TRIES, avisa /fallo y la pantalla
vuelve a probar con el original.

Variables de entorno (ninguna va al repo):
  SCREEN_CONVERT_URL     https://fotospantalla.nicovasquez.com.ar
  SCREEN_CONVERT_TOKEN   el mismo valor que en Render (32+ caracteres)
  GDRIVE_FOLDER_ID, GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID,
  GOOGLE_CLIENT_SECRET   los mismos del álbum (/etc/abrolista-album.env)
  Opcionales: SCREEN_CONVERT_LONG_SIDE (1280), SCREEN_CONVERT_POLL (15),
  SCREEN_CONVERT_WORKDIR, SCREEN_CONVERT_TIMEOUT (1200),
  SCREEN_CONVERT_TONEMAP (1; 0 = no corregir el HDR, más rápido).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

MANIFEST_NAME = "pantalla-videos.json"
DATA_FOLDER_NAME = "Datos"
CONVERT_FOLDER_NAME = "Videos pantalla H264"
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_BYTES = 100 * 1024 * 1024
MAX_AGE_SECONDS = 12 * 3600
MAX_TRIES = 2
CHUNK = 8 * 1024 * 1024
UID_RE = re.compile(r"[0-9a-f]{32}")
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
USER_AGENT = "fotospantalla-convert/1"


def log(message):
    print(f"[convert] {message}", flush=True)


def escaped(value):
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------- registro

def pending_rows(data, now=None, skip=()):
    """Filas pendientes, la más vieja primero. Ignora lo raro o viejo."""
    now = time.time() if now is None else now
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list):
        return []
    rows = []
    for row in data["videos"]:
        if not isinstance(row, dict):
            continue
        screen = row.get("screen")
        if not isinstance(screen, dict) or screen.get("state") != "pending":
            continue
        uid = row.get("uid")
        if not isinstance(uid, str) or not UID_RE.fullmatch(uid) or uid in skip:
            continue
        if not isinstance(row.get("file_id"), str) or not row["file_id"]:
            continue
        size = row.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
            continue
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if now - ts > MAX_AGE_SECONDS:
            continue
        rows.append(dict(uid=uid, file_id=row["file_id"], size=size, ts=ts))
    rows.sort(key=lambda item: item["ts"])
    return rows


# ---------------------------------------------------------------- ffmpeg

def scale_filter(long_side):
    long_side = int(long_side)
    # Nunca agranda; el lado corto queda par para yuv420p.
    return (f"scale='if(gte(iw,ih),min({long_side},iw),-2)'"
            f":'if(gte(iw,ih),-2,min({long_side},ih))'")


def tonemap_filter():
    return ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


def ffmpeg_command(source, target, long_side=1280, has_audio=True, hdr=False, ffmpeg="ffmpeg"):
    filters = [scale_filter(long_side)]
    if hdr:
        filters.append(tonemap_filter())
    filters.append("format=yuv420p")
    cmd = [
        ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-filter_threads", "1",
        "-i", source,
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += [
        "-t", "61",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-crf", "23",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-fpsmax", "30",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", target]
    return cmd


def probe(path, ffprobe="ffprobe"):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,color_transfer,width,height", "-of", "json", path],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    streams = json.loads(out or "{}").get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("sin video")
    return dict(
        codec=video.get("codec_name"),
        hdr=video.get("color_transfer") in HDR_TRANSFERS,
        audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def has_zscale(ffmpeg="ffmpeg"):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return False
    return bool(re.search(r"\bzscale\b", out)) and bool(re.search(r"\btonemap\b", out))


def tonemap_enabled():
    return os.environ.get("SCREEN_CONVERT_TONEMAP", "1").strip() != "0"


def transcode(source, target, long_side=1280, timeout=1200, ffmpeg="ffmpeg", ffprobe="ffprobe",
              zscale=None):
    info = probe(source, ffprobe)
    # El HDR del iPhone (HLG) sin tonemap se ve lavado; cuesta ~60% más de CPU.
    hdr = info["hdr"] and tonemap_enabled() and (has_zscale(ffmpeg) if zscale is None else zscale)
    cmd = ffmpeg_command(source, target, long_side, info["audio"], hdr, ffmpeg)
    started = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg falló: " + (result.stderr or "")[-400:])
    out = probe(target, ffprobe)
    if out["codec"] != "h264":
        raise RuntimeError("la copia no quedó en H.264")
    size = os.path.getsize(target)
    if not 0 < size <= MAX_BYTES:
        raise RuntimeError("la copia no tiene un tamaño válido")
    return dict(size=size, seconds=round(time.monotonic() - started, 1), hdr=hdr)


# ---------------------------------------------------------------- Drive

class DriveStore:
    def __init__(self, service, root):
        self.service = service
        self.root = root
        self._folders = {}

    def _list(self, query, fields="files(id,name,size,mimeType,modifiedTime)"):
        result = self.service.files().list(
            q=query + " and trashed = false", fields=fields, pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        return result.get("files", [])

    def folder(self, name):
        if name not in self._folders:
            found = self._list(f"name = '{escaped(name)}' and mimeType = '{FOLDER_MIME}'"
                               f" and '{escaped(self.root)}' in parents")
            if not found:
                return None
            self._folders[name] = found[0]["id"]
        return self._folders[name]

    def registry(self):
        data = self.folder(DATA_FOLDER_NAME)
        if not data:
            return {"videos": []}
        found = self._list(f"name = '{MANIFEST_NAME}' and '{escaped(data)}' in parents")
        if len(found) != 1:
            return {"videos": []}
        raw = self.service.files().get_media(fileId=found[0]["id"]).execute()
        return json.loads(raw)

    def existing_copy(self, folder, uid):
        found = self._list(f"name = '{uid}.mp4' and '{escaped(folder)}' in parents")
        for item in found:
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            if item.get("mimeType") == "video/mp4" and size > 0:
                return dict(file_id=item["id"], size=size)
        return None

    def download(self, file_id, path):
        from googleapiclient.http import MediaIoBaseDownload
        request = self.service.files().get_media(fileId=file_id)
        with open(path, "wb") as handle:
            downloader = MediaIoBaseDownload(handle, request, chunksize=CHUNK)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=3)
        return os.path.getsize(path)

    def upload(self, path, folder, uid, source_id):
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(path, mimetype="video/mp4", chunksize=CHUNK, resumable=True)
        body = dict(name=f"{uid}.mp4", mimeType="video/mp4", parents=[folder],
                    appProperties=dict(screenUid=uid, kind="pantalla-video-h264",
                                       sourceFileId=source_id))
        request = self.service.files().create(body=body, media_body=media, fields="id,size",
                                              supportsAllDrives=True)
        response = None
        while response is None:
            _status, response = request.next_chunk(num_retries=3)
        return dict(file_id=response["id"], size=int(response.get("size") or os.path.getsize(path)))

    def delete(self, file_id):
        try:
            self.service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        except Exception:
            traceback.print_exc()


def drive_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    credentials = Credentials(
        None, refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


# ---------------------------------------------------------------- app

class AppNotifier:
    def __init__(self, base_url, token, opener=None):
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("SCREEN_CONVERT_URL tiene que ser https")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        try:
            with self.opener(request, timeout=90) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def done(self, uid, file_id, size):
        return self._post(f"/api/convert/{uid}/listo", dict(file_id=file_id, size=size))

    def failed(self, uid):
        return self._post(f"/api/convert/{uid}/fallo", {})


# ---------------------------------------------------------------- trabajo

class Worker:
    def __init__(self, store, notifier, workdir, long_side=1280, timeout=1200, transcoder=transcode):
        self.store = store
        self.notifier = notifier
        self.workdir = workdir
        self.long_side = long_side
        self.timeout = timeout
        self.transcoder = transcoder
        self.tries = {}
        self.finished = set()

    def run_once(self, now=None):
        """Convierte a lo sumo un video. Devuelve el uid trabajado o None."""
        rows = pending_rows(self.store.registry(), now, skip=self.finished)
        if not rows:
            return None
        folder = self.store.folder(CONVERT_FOLDER_NAME)
        if not folder:
            log("todavía no existe la carpeta de copias")
            return None
        job = rows[0]
        self.process(job, folder)
        return job["uid"]

    def process(self, job, folder):
        uid = job["uid"]
        if self.tries.get(uid, 0) >= MAX_TRIES:
            # Ya se rindió; solo falta que la app se entere.
            if self.notifier.failed(uid) in (200, 410):
                self.finished.add(uid)
            return
        existing = self.store.existing_copy(folder, uid)
        if existing:
            # Un intento anterior subió la copia pero no llegó a avisar.
            self._report_done(uid, existing["file_id"], existing["size"])
            return
        scratch = tempfile.mkdtemp(prefix="conv-", dir=self.workdir)
        try:
            source = os.path.join(scratch, "entrada")
            target = os.path.join(scratch, "salida.mp4")
            self.store.download(job["file_id"], source)
            stats = self.transcoder(source, target, long_side=self.long_side, timeout=self.timeout)
            log(f"uid={uid} convertido en {stats['seconds']} s, {stats['size']} bytes, hdr={stats['hdr']}")
            uploaded = self.store.upload(target, folder, uid, job["file_id"])
        except Exception as error:
            self.tries[uid] = self.tries.get(uid, 0) + 1
            log(f"uid={uid} intento {self.tries[uid]} falló: {str(error)[:300]}")
            if self.tries[uid] >= MAX_TRIES:
                status = self.notifier.failed(uid)
                log(f"uid={uid} marcado como fallido (app={status})")
                if status in (200, 410):
                    self.finished.add(uid)
            return
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        self._report_done(uid, uploaded["file_id"], uploaded["size"])

    def _report_done(self, uid, file_id, size):
        status = self.notifier.done(uid, file_id, size)
        if status == 200:
            self.finished.add(uid)
            log(f"uid={uid} listo en pantalla")
        elif status == 410:
            # Se moderó mientras convertíamos: la app ya borró la copia.
            self.finished.add(uid)
            log(f"uid={uid} ya no está; copia descartada")
        elif status == 400:
            self.store.delete(file_id)
            self.tries[uid] = self.tries.get(uid, 0) + 1
            log(f"uid={uid} la app rechazó la copia")
            if self.tries[uid] >= MAX_TRIES:
                self.notifier.failed(uid)
                self.finished.add(uid)
        else:
            # 503 u otro: queda la copia en Drive y el próximo ciclo reintenta el aviso.
            log(f"uid={uid} la app no confirmó (status={status}); reintento")


def main():
    required = ("SCREEN_CONVERT_URL", "SCREEN_CONVERT_TOKEN", "GDRIVE_FOLDER_ID",
                "GOOGLE_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        log("faltan variables: " + ", ".join(missing))
        return 2
    if len(os.environ["SCREEN_CONVERT_TOKEN"].strip()) < 32:
        log("SCREEN_CONVERT_TOKEN tiene que tener 32+ caracteres")
        return 2
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log(f"falta {tool}")
            return 2
    workdir = os.environ.get("SCREEN_CONVERT_WORKDIR") or os.environ.get("CACHE_DIRECTORY") or tempfile.gettempdir()
    os.makedirs(workdir, exist_ok=True)
    poll = max(5, int(os.environ.get("SCREEN_CONVERT_POLL", "15")))
    worker = Worker(
        DriveStore(drive_service(), os.environ["GDRIVE_FOLDER_ID"]),
        AppNotifier(os.environ["SCREEN_CONVERT_URL"], os.environ["SCREEN_CONVERT_TOKEN"].strip()),
        workdir,
        long_side=int(os.environ.get("SCREEN_CONVERT_LONG_SIDE", "1280")),
        timeout=int(os.environ.get("SCREEN_CONVERT_TIMEOUT", "1200")),
    )
    if "--check" in sys.argv[1:]:
        # Prueba de despliegue: lee Drive sin convertir nada.
        store = worker.store
        data = store.registry()
        pending = pending_rows(data)
        folder = store.folder(CONVERT_FOLDER_NAME)
        log(f"Drive ok: {len(data.get('videos', []))} videos en el registro, {len(pending)} pendientes, "
            f"carpeta de copias {'lista' if folder else 'todavía no creada (la crea la app)'}")
        return 0
    log("arrancó")
    while True:
        try:
            worked = worker.run_once()
        except Exception:
            traceback.print_exc()
            worked = None
        if not worked:
            time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
