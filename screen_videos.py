"""Videos cortos para la pantalla. Los bytes van directo a Drive.

El celular pide una sesión resumible, sube el archivo a Google y después
avisa para registrarlo. Gunicorn no recibe el video: solo metadata, el
registro y, cuando /loop lo pasa, rangos chicos para un solo reproductor.

La moderación es la de las fotos: entra a la rotación ni bien queda
registrado y /admin lo puede borrar. No hay cola de aprobación previa
porque las fotos tampoco la tienen.

Transcodificar (H.264 720p, faststart, poster) no entra en Render Free.
transcode_for_screen() es el gancho para hacerlo después en un VPS.
"""
import json
import re
import threading
import time
import traceback
import urllib.parse
import uuid

from flask import Response, abort, jsonify, request
from delivery import FOLDER, escaped

MAX_SECONDS = 60
MAX_BYTES = 100 * 1024 * 1024
VIDEO_LIMIT = 3
VIDEO_WINDOW_SECONDS = 600
SCREEN_FORMAT_MESSAGE = (
    "Este video está en un formato que la pantalla no puede mostrar. "
    "En el iPhone andá a Ajustes > Cámara > Formatos, elegí «Más compatible» "
    "y grabalo de nuevo, o mandá otro video."
)
# Margen corto: el celular a veces reporta 60.01 en un video de un minuto.
DURATION_GRACE = 0.05
PLAYBACK_CHUNK = 1024 * 1024
MANIFEST_NAME = "pantalla-videos.json"
# No usar "Videos" (eso es el álbum) ni "Originales" (la limpieza de fotos
# exige una copia de cada archivo de esa carpeta).
FOLDER_NAME = "Videos pantalla"
EXTS = {"mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm"}
SCREEN_EVENT_PATHS = {
    "/api/videos/session",
    "/api/videos/register",
    "/api/videos",
    "/api/loop-items",
    "/api/admin/videos",
}
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"
_lock = threading.RLock()
_video_hits = {}
_video_hits_lock = threading.Lock()
deps = None


class ScreenFormatError(ValueError):
    """El contenedor declara HEVC y la pantalla no lo puede mostrar."""
NAME_LIMIT = 40
GREETING_LIMIT = 280


def transcode_for_screen(record, drive):
    """Gancho para un VPS. En Render Free tiene que devolver None.

    No descargues el original acá: un video de hasta 100 MB no entra en los
    512 MB del plan gratuito y el disco se borra en cada reinicio.

    Más adelante, en un servidor con ffmpeg y disco, esta función puede
    bajar el archivo, dejar una copia H.264 720p yuv420p con +faststart y
    un poster, subir esa copia y devolver {"file_id", "mime", "ext", "size"}.
    El original tiene que seguir en Drive.

        ffmpeg -y -i entrada.mov -vf "scale=-2:720" -c:v libx264 \\
            -preset veryfast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 128k \\
            -movflags +faststart salida.mp4
        ffmpeg -y -i salida.mp4 -ss 1 -frames:v 1 poster.jpg
    """
    return None


def parse_size(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("tamaño")
    if value < 1 or value > MAX_BYTES:
        raise ValueError("tamaño")
    return value


def parse_duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("duración")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        raise ValueError("duración")
    if value > MAX_SECONDS + DURATION_GRACE:
        raise ValueError("duración")
    return min(value, float(MAX_SECONDS))


def parse_ext(value):
    ext = str(value or "").lower().strip(".")
    if ext not in EXTS:
        raise ValueError("formato")
    return ext


def guest_origin():
    """El navegador solo puede subir a Drive si la sesión nació con su Origin."""
    host = request.host
    origin = request.headers.get("Origin", "").strip()
    parsed = urllib.parse.urlparse(origin)
    if (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
            and parsed.netloc.lower() == host.lower() and parsed.path in {"", "/"}
            and not parsed.query and not parsed.fragment):
        return f"{parsed.scheme}://{parsed.netloc}"
    scheme = (request.headers.get("X-Forwarded-Proto") or request.scheme or "https").split(",")[0].strip()
    if scheme not in {"http", "https"}:
        scheme = "https"
    return f"{scheme}://{host}"


def public_upload_url(location):
    if isinstance(location, (list, tuple)):
        location = location[0] if location else ""
    parsed = urllib.parse.urlparse(str(location or ""))
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() != "www.googleapis.com"
            or not parsed.path.startswith("/upload/drive/")):
        raise RuntimeError("Drive no devolvió una sesión de subida válida.")
    return parsed.geturl()


def drive_http(service, url, method, body, headers):
    resp, content = service._http.request(url, method=method, body=body, headers=headers)
    status = int(resp.status)
    header_map = {str(key).lower(): value for key, value in resp.items()}
    if isinstance(content, str):
        content = content.encode()
    return status, header_map, content or b""


def read_drive_range(service, file_id, start, end):
    req = service.files().get_media(fileId=file_id)
    req.headers["Range"] = f"bytes={start}-{end}"
    data = req.execute()
    if isinstance(data, str):
        data = data.encode()
    if not isinstance(data, (bytes, bytearray)):
        raise IOError("Drive no devolvió bytes del video.")
    return bytes(data)


def iter_chunks(read_range, file_id, start, end):
    pos = start
    while pos <= end:
        last = min(end, pos + PLAYBACK_CHUNK - 1)
        data = read_range(file_id, pos, last)
        expected = last - pos + 1
        if not isinstance(data, (bytes, bytearray)) or len(data) < expected:
            raise IOError("Drive no devolvió el rango del video.")
        yield bytes(data[:expected])
        pos = last + 1


def parse_range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise ValueError("rango")
    if not match.group(1):
        length = int(match.group(2))
        if length <= 0:
            raise ValueError("rango")
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(match.group(1))
        end = size - 1 if not match.group(2) else min(size - 1, int(match.group(2)))
    if start >= size or end < start or start < 0:
        raise ValueError("rango")
    return start, end, True


def _valid_row(row):
    if not isinstance(row, dict):
        return False
    uid = row.get("uid")
    ext = row.get("ext")
    if not isinstance(uid, str) or not re.fullmatch(r"[0-9a-f]{32}", uid):
        return False
    if ext not in EXTS or row.get("name") != f"{uid}.{ext}":
        return False
    if not isinstance(row.get("file_id"), str) or not row["file_id"]:
        return False
    if isinstance(row.get("size"), bool) or not isinstance(row.get("size"), int):
        return False
    if row["size"] < 1 or row["size"] > MAX_BYTES:
        return False
    if row.get("mime") != EXTS[ext] or not isinstance(row.get("playable"), bool):
        return False
    try:
        duration = float(row.get("duration"))
        ts = float(row.get("ts"))
    except (TypeError, ValueError):
        return False
    if duration <= 0 or duration > MAX_SECONDS or ts <= 0:
        return False
    return True


def load_rows():
    delivery = deps.delivery()
    folder = deps.data_folder()
    if not delivery or not folder:
        raise RuntimeError("No hay Drive para leer los videos.")
    found = delivery.list_files(folder, f"name = '{escaped(MANIFEST_NAME)}'")
    if not found:
        return []
    if len(found) > 1:
        raise RuntimeError("Hay más de un registro de videos.")
    raw = delivery.service.files().get_media(fileId=found[0]["id"]).execute()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("videos"), list):
        raise RuntimeError("El registro de videos no es válido.")
    rows = []
    for row in data["videos"]:
        if _valid_row(row):
            rows.append(_present(row))
        else:
            print("[video] fila ignorada en el registro")
    return rows


def save_rows(rows):
    delivery = deps.delivery()
    folder = deps.data_folder()
    payload = json.dumps({"videos": rows}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    delivery.write(folder, MANIFEST_NAME, payload, "application/json")


def _event_ok(ts):
    delivery = deps.delivery()
    if delivery is None:
        return False
    try:
        # /videos/<id> no pasa por el cierre de evento; leé la config guardada.
        config = delivery.load_config()
    except Exception:
        return False
    if not config:
        return False
    return delivery.accepts(float(ts), config) and (time.time() - float(ts)) <= deps.retention()


def visible_videos():
    with _lock:
        vetoes = deps.vetoes()
        rows = []
        for row in load_rows():
            if row["uid"] in vetoes or not _event_ok(row["ts"]):
                continue
            rows.append(row)
        rows.sort(key=lambda row: row["ts"])
        return rows


def clean_guest_text(value, limit):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if "<" in text or ">" in text:
        return ""
    return text[:limit]


def _present(row):
    row["nombre"] = clean_guest_text(row.get("nombre"), NAME_LIMIT)
    row["saludo"] = clean_guest_text(row.get("saludo"), GREETING_LIMIT)
    row["seen"] = row.get("seen") is True
    return row


def public_video(row):
    return {
        "uid": row["uid"],
        "ext": row["ext"],
        "mime": row["mime"],
        "size": row["size"],
        "duration": row["duration"],
        "ts": row["ts"],
        "playable": row["playable"],
        "nombre": row.get("nombre") or "",
        "saludo": row.get("saludo") or "",
        "seen": row.get("seen") is True,
    }


def loop_items():
    photos = list(deps.photo_items())
    videos = []
    for row in visible_videos():
        videos.append({
            "kind": "video",
            "src": f"/videos/{row['uid']}",
            "uid": row["uid"],
            "mime": row["mime"],
            "duration": row["duration"],
            "ts": row["ts"],
        })
    items = list(photos) + videos
    items.sort(key=lambda item: (float(item.get("ts") or 0), 0 if item.get("kind") == "photo" else 1))
    return items


def _find_named(folder_id, name):
    return deps.delivery().list_files(folder_id, f"name = '{escaped(name)}'")


def client_ip():
    header = (request.headers.get("CF-Connecting-IP") or "").strip()
    if header:
        return header.split(",")[0].strip()
    return request.remote_addr or "?"


def video_rate_limited():
    """Tres sesiones de video por IP cada 10 minutos."""
    now = time.time()
    ip = client_ip()
    with _video_hits_lock:
        hits = [stamp for stamp in _video_hits.get(ip, []) if now - stamp < VIDEO_WINDOW_SECONDS]
        if len(hits) >= VIDEO_LIMIT:
            _video_hits[ip] = hits
            return True
        hits.append(now)
        _video_hits[ip] = hits
        if len(_video_hits) > 5000:
            _video_hits.clear()
        return False


def _box_header(header, remaining):
    if len(header) < 8 or remaining < 8:
        return None
    size = int.from_bytes(header[:4], "big")
    kind = header[4:8]
    header_len = 8
    if size == 1:
        if len(header) < 16 or remaining < 16:
            return None
        size = int.from_bytes(header[8:16], "big")
        header_len = 16
    elif size == 0:
        size = remaining
    if size < header_len or size > remaining:
        return None
    return kind, size, header_len


def _read_box(read_at, start, length):
    data = read_at(start, length)
    if not isinstance(data, (bytes, bytearray)):
        return b""
    return bytes(data[:length])


def declares_hevc(read_at, size):
    """True si el MP4/MOV tiene la marca hvc1 o hev1 en ftyp o moov.

    Solo lee encabezados y esas cajas. No baja el mdat.
    """
    if not isinstance(size, int) or size < 8:
        return False
    offset = 0
    for _guard in range(10000):
        if offset + 8 > size:
            return False
        header = _read_box(read_at, offset, min(16, size - offset))
        parsed = _box_header(header, size - offset)
        if not parsed:
            return False
        kind, box_size, _header_len = parsed
        if kind in {b"ftyp", b"moov"}:
            chunk = _read_box(read_at, offset, min(box_size, 2 * 1024 * 1024))
            if b"hvc1" in chunk or b"hev1" in chunk:
                return True
        offset += box_size
        if offset >= size:
            return False
    return False


def _reject_hevc_file(file_id, size):
    def read_at(start, length):
        if start < 0 or start >= size or length <= 0:
            return b""
        end = min(size - 1, start + length - 1)
        return deps.read_range(file_id, start, end)

    if not declares_hevc(read_at, size):
        return
    try:
        deps.drive().files().delete(fileId=file_id).execute()
    except Exception:
        traceback.print_exc()
    raise ScreenFormatError(SCREEN_FORMAT_MESSAGE)


def create_session(ext, size, duration):
    if deps.drive() is None:
        raise RuntimeError("No hay Drive para guardar el video.")
    folder = deps.video_folder()
    if not folder:
        raise RuntimeError("No hay carpeta de Drive para los videos.")
    uid = uuid.uuid4().hex
    name = f"{uid}.{ext}"
    metadata = {
        "name": name,
        "mimeType": EXTS[ext],
        "parents": [folder],
        "appProperties": {"screenUid": uid, "kind": "pantalla-video"},
    }
    body = json.dumps(metadata).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": EXTS[ext],
        "X-Upload-Content-Length": str(size),
        "Origin": guest_origin(),
    }
    status, response_headers, _content = deps.drive_request(UPLOAD_URL, "POST", body, headers)
    if status not in (200, 201):
        print(f"[video] sesión rechazada status={status}")
        raise RuntimeError("Drive no abrió la sesión de subida.")
    upload_url = public_upload_url(response_headers.get("location"))
    print(f"[video] sesion uid={uid} bytes={size} ext={ext}")
    return {"ok": True, "uid": uid, "uploadUrl": upload_url}


def register_upload(uid, size, duration, nombre="", saludo=""):
    if not re.fullmatch(r"[0-9a-f]{32}", uid or ""):
        raise ValueError("video")
    if deps.drive() is None:
        raise RuntimeError("No hay Drive para guardar el video.")
    folder = deps.video_folder()
    if not folder:
        raise RuntimeError("No hay carpeta de Drive para los videos.")
    with _lock:
        rows = load_rows()
        current = next((row for row in rows if row["uid"] == uid), None)
        if current:
            if current["size"] != size:
                raise ValueError("video")
            return current
        matches = []
        ext = None
        for candidate, _mime in EXTS.items():
            named = _find_named(folder, f"{uid}.{candidate}")
            if len(named) > 1:
                raise RuntimeError("Hay videos duplicados con el mismo nombre.")
            if named:
                matches.append(named[0])
                ext = candidate
        if len(matches) > 1:
            raise RuntimeError("Hay videos duplicados con el mismo nombre.")
        if len(matches) != 1:
            raise LookupError("todavía no llegó")
        stored = matches[0]
        if stored.get("mimeType") == FOLDER:
            raise RuntimeError("El archivo de Drive no es un video.")
        try:
            stored_size = int(stored.get("size") or 0)
        except (TypeError, ValueError):
            stored_size = 0
        if stored_size != size:
            raise ValueError("video")
        _reject_hevc_file(stored["id"], size)
        row = {
            "uid": uid,
            "name": f"{uid}.{ext}",
            "file_id": stored["id"],
            "ext": ext,
            "mime": EXTS[ext],
            "size": size,
            "duration": duration,
            "ts": round(time.time(), 3),
            "playable": True,
            "nombre": clean_guest_text(nombre, NAME_LIMIT),
            "saludo": clean_guest_text(saludo, GREETING_LIMIT),
            "seen": False,
        }
        # None = publicar el original. Una copia transcodificada se rechaza hasta
        # que un VPS enseñe a esta función a guardarla sin borrar el original.
        if transcode_for_screen(row, deps.drive()) is not None:
            raise RuntimeError("Este servidor no publica videos transcodificados.")
        rows.append(row)
        save_rows(rows)
        print(f"[video] registrado uid={uid} bytes={size}")
        return row


def _marker(uid):
    cache = deps.cache_dir()
    marker = cache / ("moderation-" + uid + ".json")
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"uid": uid}), encoding="utf-8")
    temporary.replace(marker)


def delete_video(uid):
    if not re.fullmatch(r"[0-9a-f]{32}", uid or ""):
        raise ValueError("video")
    with _lock:
        rows = load_rows()
        current = next((row for row in rows if row["uid"] == uid), None)
        ids = set()
        if current and current.get("file_id"):
            ids.add(current["file_id"])
        folder = deps.video_folder()
        if folder:
            for ext in EXTS:
                for stored in _find_named(folder, f"{uid}.{ext}"):
                    if stored.get("id"):
                        ids.add(stored["id"])
        _marker(uid)
        delivery = deps.delivery()
        delivery.save_veto(uid, ids)
        result = delivery.moderate_photo(uid)
        save_rows([row for row in rows if row["uid"] != uid])
        return result


def mark_seen(uid):
    if not re.fullmatch(r"[0-9a-f]{32}", uid or ""):
        raise ValueError("video")
    with _lock:
        rows = load_rows()
        current = next((row for row in rows if row["uid"] == uid), None)
        if not current:
            raise LookupError("video")
        current["seen"] = True
        save_rows(rows)
        return public_video(current)


def _playback_row(uid):
    if not re.fullmatch(r"[0-9a-f]{32}", uid or ""):
        return None
    with _lock:
        if uid in deps.vetoes():
            return None
        row = next((item for item in load_rows() if item["uid"] == uid), None)
        if not row or not _event_ok(row["ts"]):
            return None
        return dict(row)


def register_screen_videos(app, injected):
    global deps
    deps = injected

    @app.route("/api/videos/session", methods=["POST"])
    def video_session():
        deps.release_event_lock()
        if request.content_length and request.content_length > 4096:
            return jsonify(ok=False, error="No se pudo preparar el video."), 413
        payload = request.get_json(silent=True) or {}
        try:
            ext = parse_ext(payload.get("ext"))
            size = parse_size(payload.get("size"))
            duration = parse_duration(payload.get("duration"))
            if payload.get("mime") != EXTS[ext]:
                raise ValueError("formato")
        except ValueError as error:
            reason = str(error)
            if reason == "tamaño":
                message = "Ese video pesa más de 100 MB. Probá con uno más corto."
            elif reason == "duración":
                message = "Ese video dura más de 60 segundos. Recortalo y probalo de nuevo."
            else:
                message = "Tiene que ser mp4, mov o webm."
            return jsonify(ok=False, error=message), 400
        if video_rate_limited():
            return jsonify(ok=False, error="Ya mandaste 3 videos en los últimos 10 minutos. Esperá un poco y probá de nuevo."), 429
        try:
            body = create_session(ext, size, duration)
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, error="No hay Drive para guardar el video."), 503
        return jsonify(body)

    @app.route("/api/videos/register", methods=["POST"])
    def video_register():
        deps.release_event_lock()
        if request.content_length and request.content_length > 4096:
            return jsonify(ok=False, error="No se pudo registrar el video."), 413
        payload = request.get_json(silent=True) or {}
        try:
            uid = str(payload.get("uid") or "")
            size = parse_size(payload.get("size"))
            duration = parse_duration(payload.get("duration"))
            nombre = clean_guest_text(payload.get("nombre"), NAME_LIMIT)
            saludo = clean_guest_text(payload.get("saludo"), GREETING_LIMIT)
            row = register_upload(uid, size, duration, nombre, saludo)
        except LookupError:
            return jsonify(ok=False, error="El video todavía no está en Drive. Reintentá."), 409
        except ScreenFormatError:
            return jsonify(ok=False, error=SCREEN_FORMAT_MESSAGE), 400
        except ValueError:
            return jsonify(ok=False, error="No se pudo registrar el video."), 400
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, error="No se pudo registrar el video."), 503
        try:
            deps.deliver_video(row["file_id"])
        except Exception:
            # El loop ya tiene el video. La copia al álbum se reintenta al cerrar la fiesta.
            traceback.print_exc()
            print("[video] copia al álbum pendiente; el original sigue en Videos pantalla.")
        return jsonify(ok=True)

    @app.route("/api/videos")
    def video_list():
        try:
            rows = [public_video(row) for row in visible_videos()]
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, videos=[], error="No se pudo verificar la moderación."), 503
        return jsonify(videos=rows)

    @app.route("/api/loop-items")
    def video_loop_items():
        try:
            items = loop_items()
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, items=[], error="No se pudo verificar la moderación."), 503
        return jsonify(items=items)

    @app.route("/api/admin/videos", methods=["GET", "POST"])
    def admin_video_list():
        if not deps.check_admin():
            return jsonify(ok=False), 403
        if request.method == "POST":
            if request.form.get("action") != "visto":
                return jsonify(ok=False), 400
            try:
                mark_seen(request.form.get("uid", ""))
            except LookupError:
                return jsonify(ok=False), 404
            except ValueError:
                return jsonify(ok=False), 400
            except Exception:
                traceback.print_exc()
                return jsonify(ok=False), 503
            return jsonify(ok=True)
        try:
            rows = [public_video(row) for row in visible_videos()]
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, videos=[], error="No se pudo verificar la moderación."), 503
        rows.sort(key=lambda row: row["ts"], reverse=True)
        return jsonify(videos=rows)

    @app.route("/api/admin/borrar-video", methods=["POST"])
    def video_delete():
        if not deps.check_admin():
            return jsonify(ok=False), 403
        try:
            delete_video(request.form.get("uid", ""))
        except ValueError:
            return jsonify(ok=False), 400
        except Exception:
            traceback.print_exc()
            return jsonify(ok=False, error="No se completó la eliminación del video. Volvé a intentar."), 503
        return jsonify(ok=True)

    @app.route("/videos/<uid>", methods=["GET", "HEAD"])
    def video_stream(uid):
        try:
            row = _playback_row(uid)
        except Exception:
            traceback.print_exc()
            abort(503)
        if not row:
            abort(404)
        size = row["size"]
        try:
            start, end, partial = parse_range(request.headers.get("Range"), size)
        except ValueError:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.args.get("descarga") == "1":
            headers["Content-Disposition"] = f'attachment; filename="{row["name"]}"'
        if request.method == "HEAD":
            return Response(status=206 if partial else 200, mimetype=row["mime"], headers=headers)

        def generate():
            try:
                yield from iter_chunks(deps.read_range, row["file_id"], start, end)
            except Exception:
                traceback.print_exc()
                return

        return Response(generate(), status=206 if partial else 200, mimetype=row["mime"], headers=headers)
