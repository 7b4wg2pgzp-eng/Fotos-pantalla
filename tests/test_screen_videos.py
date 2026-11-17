"""Videos de pantalla con Drive en memoria. No hay red ni archivos reales."""
import importlib
import io
import json
import socket
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from delivery import Delivery, validate_config
from test_moderation import FakeDrive

UID = "c" * 32
OTHER = "d" * 32
VIDEO_BYTES = b"0123456789"
FILE_ID = "drive-file-secret"


class Resp(dict):
    def __init__(self, status, **headers):
        super().__init__(headers)
        self.status = status


class ScreenVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_dir = tempfile.TemporaryDirectory()
        with patch.dict("os.environ", {
            "EVENTPHOTOS_DIR": cls.import_dir.name,
            "EVENTPHOTOS_CACHE": cls.import_dir.name,
        }, clear=True), patch("threading.Thread.start"):
            cls.screen = importlib.import_module("app")
            cls.videos = importlib.import_module("screen_videos")

    @classmethod
    def tearDownClass(cls):
        cls.import_dir.cleanup()

    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in tests"))
        network.start()
        self.addCleanup(network.stop)
        self.drive = FakeDrive()
        self.delivery = Delivery(self.drive, "root", lambda: "data", "state.json")
        now = datetime.now(timezone.utc)
        self.config = validate_config(dict(
            name="Fiesta ficticia",
            start=(now - timedelta(hours=1)).isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            enabled=True,
        ))
        self.delivery.config = self.config
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.photo = self.cache / (OTHER + ".jpg")
        self.photo.write_bytes(b"photo")
        older = now.timestamp() - 30
        self.photo.touch()
        import os
        os.utime(self.photo, (older, older))
        self.drive_calls = []

        def drive_request(url, method, body, headers):
            self.drive_calls.append(dict(url=url, method=method, body=body, headers=headers))
            return 200, {"location": "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=test"}, b""

        self.reads = []

        def read_range(file_id, start, end):
            self.reads.append((file_id, start, end))
            return VIDEO_BYTES[start:end + 1]

        for name, value in dict(
            _drive_service=self.drive,
            _delivery=self.delivery,
            CACHE_DIR=self.cache,
            ADMIN_KEY="test-key",
        ).items():
            patcher = patch.object(self.screen, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in dict(
            video_folder=lambda: "screen",
            data_folder=lambda: "data",
            drive_request=drive_request,
            read_range=read_range,
        ).items():
            patcher = patch.object(self.videos.deps, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ensure = patch.object(self.screen._event_source, "ensure", return_value=self.config)
        ensure.start()
        self.addCleanup(ensure.stop)
        self.videos.reset_vj_state()
        self.client = self.screen.app.test_client()

    def session(self, **extra):
        payload = dict(ext="mp4", size=len(VIDEO_BYTES), mime="video/mp4", duration=12.5)
        payload.update(extra)
        return self.client.post("/api/videos/session", json=payload, headers={"Origin": "https://evil.example"})

    def add_video(self, uid=UID, data=VIDEO_BYTES):
        self.drive.add(FILE_ID, uid + ".mp4", "screen", "video/mp4", data=data)
        self.drive.add("keep-photo", OTHER + ".jpg", "originals")

    def register(self, uid=UID, size=None, duration=12.5):
        return self.client.post("/api/videos/register", json=dict(
            uid=uid, size=len(VIDEO_BYTES) if size is None else size, duration=duration))

    def test_limits_and_folder_stay_away_from_album_originals(self):
        self.assertEqual(self.videos.MAX_BYTES, 100 * 1024 * 1024)
        self.assertEqual(self.videos.MAX_SECONDS, 60)
        self.assertEqual(self.videos.parse_duration(60), 60)
        self.assertEqual(self.videos.parse_duration(60.05), 60)
        with self.assertRaises(ValueError):
            self.videos.parse_duration(60.2)
        with self.assertRaises(ValueError):
            self.videos.parse_size(self.videos.MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            self.videos.parse_size(True)
        self.assertNotIn(self.videos.FOLDER_NAME, {"Videos", "Originales", "Previews", "Datos"})
        js = Path("static/video_upload.js").read_text(encoding="utf-8")
        self.assertIn(f"const VIDEO_MAX_BYTES = {self.videos.MAX_BYTES}", js)
        self.assertIn(f"const VIDEO_MAX_SECONDS = {self.videos.MAX_SECONDS}", js)
        proc = Path("Procfile").read_text(encoding="utf-8")
        self.assertIn("--workers 1", proc)
        self.assertIn("--threads 4", proc)
        self.assertIn("--timeout 120", proc)

    def test_session_is_metadata_only_and_binds_the_guest_origin(self):
        response = self.session()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json), {"ok", "uid", "uploadUrl"})
        self.assertTrue(response.json["uploadUrl"].startswith("https://www.googleapis.com/upload/drive/"))
        self.assertEqual(len(self.drive_calls), 1)
        call = self.drive_calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("uploadType=resumable", call["url"])
        self.assertLess(len(call["body"]), 1000)
        self.assertNotIn(b"0123456789", call["body"])
        meta = json.loads(call["body"])
        self.assertEqual(meta["parents"], ["screen"])
        self.assertEqual(meta["mimeType"], "video/mp4")
        self.assertTrue(meta["name"].endswith(".mp4"))
        self.assertEqual(call["headers"]["Origin"], "http://localhost")
        self.assertEqual(call["headers"]["X-Upload-Content-Length"], str(len(VIDEO_BYTES)))
        self.assertEqual(call["headers"]["X-Upload-Content-Type"], "video/mp4")
        self.assertNotIn("evil.example", response.get_data(as_text=True))

    def test_session_rejects_limits_foreign_url_and_missing_drive(self):
        self.assertEqual(self.session(size=self.videos.MAX_BYTES + 1).status_code, 400)
        self.assertEqual(self.session(duration=61).status_code, 400)
        self.assertEqual(self.session(ext="hevc", mime="video/mp4").status_code, 400)
        self.assertEqual(self.session(mime="video/webm").status_code, 400)
        self.assertEqual(self.drive_calls, [])
        self.drive_calls.clear()

        def foreign(*_args):
            self.drive_calls.append("called")
            return 200, {"location": "https://evil.example/upload/secret"}, b""

        with patch.object(self.videos.deps, "drive_request", foreign):
            response = self.session()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("evil.example", response.get_data(as_text=True))
        with patch.object(self.screen, "_drive_service", None):
            self.assertEqual(self.session().status_code, 503)

    def test_closed_event_does_not_open_a_drive_session(self):
        with patch.object(self.screen._event_source, "ensure", return_value=None):
            response = self.session()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.drive_calls, [])

    def test_register_publishes_like_a_photo_without_downloading_it(self):
        self.add_video()
        downloads = []
        original = self.drive.get_media

        def track(fileId):
            downloads.append(fileId)
            return original(fileId)

        self.drive.get_media = track
        with patch("subprocess.run") as run:
            first = self.register()
            second = self.register()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json["ok"] and second.json["ok"])
        run.assert_not_called()
        self.assertNotIn(FILE_ID, downloads)
        self.assertIsNone(self.videos.transcode_for_screen({"uid": UID}, self.drive))
        listed = self.client.get("/api/videos").json["videos"]
        self.assertEqual(listed[0]["uid"], UID)
        self.assertNotIn("file_id", listed[0])
        body = self.client.get("/api/loop-items").get_data(as_text=True)
        self.assertNotIn(FILE_ID, body)
        items = json.loads(body)["items"]
        self.assertEqual([item["kind"] for item in items], ["photo", "video"])
        self.assertEqual(items[0]["src"], "/photos/" + OTHER + ".jpg")
        self.assertEqual(items[1]["src"], "/videos/" + UID)
        self.assertEqual(items[1]["mime"], "video/mp4")
        photos = self.client.get("/api/photos").json["photos"]
        self.assertEqual(photos, ["/photos/" + OTHER + ".jpg"])

    def test_register_rejects_a_missing_or_different_file(self):
        missing = self.register()
        self.assertEqual(missing.status_code, 409)
        self.add_video()
        mismatch = self.register(size=len(VIDEO_BYTES) - 1)
        self.assertEqual(mismatch.status_code, 400)
        self.assertEqual(self.client.get("/api/videos").json["videos"], [])
        self.assertIn(FILE_ID, self.drive.rows)

    def test_playback_streams_only_the_requested_ranges(self):
        self.add_video()
        self.register()
        with patch.object(self.videos, "PLAYBACK_CHUNK", 4):
            full = self.client.get("/videos/" + UID)
            full_bytes = full.get_data()
            partial = self.client.get("/videos/" + UID, headers={"Range": "bytes=2-5"})
            partial_bytes = partial.get_data()
            invalid = self.client.get("/videos/" + UID, headers={"Range": "bytes=10-"})
            self.assertEqual(self.reads, [
                (FILE_ID, 0, 3), (FILE_ID, 4, 7), (FILE_ID, 8, 9), (FILE_ID, 2, 5),
            ])
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full_bytes, VIDEO_BYTES)
        self.assertEqual(full.mimetype, "video/mp4")
        self.assertEqual(full.headers["Cache-Control"], "private, no-store")
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial_bytes, b"2345")
        self.assertEqual(partial.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(invalid.status_code, 416)

    def test_delete_hides_the_screen_and_keeps_unrelated_photos(self):
        self.add_video()
        self.register()
        self.drive.fail_delete.add(FILE_ID)
        failed = self.client.post("/api/admin/borrar-video", data=dict(uid=UID, key="test-key"))
        self.assertEqual(failed.status_code, 503)
        self.assertIn(FILE_ID, self.drive.rows)
        self.assertNotIn(UID, [item.get("uid") for item in self.client.get("/api/loop-items").json["items"]])
        self.drive.fail_delete.clear()
        done = self.client.post("/api/admin/borrar-video", data=dict(uid=UID, key="test-key"))
        self.assertEqual(done.status_code, 200)
        self.assertTrue(done.json["ok"])
        self.assertNotIn(FILE_ID, self.drive.rows)
        self.assertIn("keep-photo", self.drive.rows)
        self.assertEqual(self.client.get("/videos/" + UID).status_code, 404)
        self.assertEqual(self.client.post("/api/admin/borrar-video", data=dict(uid="nope", key="test-key")).status_code, 400)
        self.assertEqual(self.client.post("/api/admin/borrar-video", data=dict(uid=UID)).status_code, 403)

    def test_photo_upload_does_not_accept_a_video(self):
        before = set(self.drive.rows)
        response = self.client.post("/api/upload", data={"photos": (io.BytesIO(b"not-the-video-bytes"), "clip.mp4")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(self.drive.rows), before)

    def test_pages_offer_video_without_changing_photo_routes(self):
        upload = self.client.get("/")
        loop = self.client.get("/loop")
        self.assertEqual(upload.status_code, 200)
        self.assertIn(b"Elegir video", upload.data)
        self.assertIn(b"/static/video_upload.js", upload.data)
        script = self.client.get("/static/video_upload.js")
        self.assertEqual(script.status_code, 200)
        script.close()
        self.assertIn(b"muted", loop.data)
        self.assertIn(b"playsinline", loop.data)
        self.assertIn(b"setInterval(showNext, HOLD_MS);", loop.data)
        self.assertIn(b"/static/screen_loop_videos.js", loop.data)
        self.assertNotIn(b"box-shadow", loop.data)
        player = Path("static/screen_loop_videos.js").read_text(encoding="utf-8")
        self.assertIn("const VIDEO_CAP_S = 60", player)
        self.assertIn("currentTime >= VIDEO_CAP_S", player)
        self.assertIn("Muro de fotos y videos en vivo", upload.get_data(as_text=True))
        self.assertIn(
            "Sumá tus fotos o un video corto y dejá tu saludo. Aparecen en la pantalla de la fiesta.",
            upload.get_data(as_text=True),
        )
        self.assertIn("¡Listo! Tu video ya va a la pantalla de la fiesta.", upload.get_data(as_text=True))
        self.assertIn("Fotos de la fiesta", upload.get_data(as_text=True))
        self.assertIn("Transmitir en vivo", upload.get_data(as_text=True))

    def test_drive_helpers_request_a_range_and_a_session_url(self):
        captured = {}

        class Http:
            def request(self, uri, method="GET", body=None, headers=None):
                captured.update(uri=uri, method=method, body=body, headers=headers)
                return Resp(200, Location="https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=abc"), b""

        class Service:
            _http = Http()

        status, headers, content = self.videos.drive_http(Service(), "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable", "POST", b"{}", {})
        self.assertEqual(status, 200)
        self.assertEqual(content, b"")
        self.assertEqual(captured["body"], b"{}")
        self.assertEqual(self.videos.public_upload_url(headers["location"]).startswith("https://www.googleapis.com/"), True)

        class Media:
            def __init__(self):
                self.headers = {}

            def execute(self):
                start, end = self.headers["Range"].removeprefix("bytes=").split("-")
                return b"abcdef"[int(start):int(end) + 1]

        class Files:
            def __init__(self):
                self.media = Media()

            def get_media(self, fileId):
                self.file_id = fileId
                return self.media

        files = Files()

        class Api:
            def files(self):
                return files

        self.assertEqual(self.videos.read_drive_range(Api(), "id", 1, 3), b"bcd")
        self.assertEqual(files.media.headers["Range"], "bytes=1-3")
        with self.assertRaises(RuntimeError):
            self.videos.public_upload_url("http://www.googleapis.com/upload/drive/v3/files?upload_id=x")

    def test_vj_lists_newest_first_and_output_uses_its_own_token(self):
        nuevo = "e" * 32
        clock = {"t": time.time()}

        def now():
            return clock["t"]

        self.add_video()
        with patch.object(self.videos.time, "time", now):
            primero = self.client.post("/api/videos/register", json=dict(
                uid=UID, size=len(VIDEO_BYTES), duration=12.5, nombre="Ana", saludo="Bravo",
            ))
            clock["t"] += 30
            self.drive.add("file-nuevo", nuevo + ".mp4", "screen", "video/mp4", data=VIDEO_BYTES)
            segundo = self.client.post("/api/videos/register", json=dict(
                uid=nuevo, size=len(VIDEO_BYTES), duration=8, saludo="Más nuevo",
            ))
        self.assertEqual(primero.status_code, 200)
        self.assertEqual(segundo.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/videos").status_code, 403)
        self.assertEqual(self.client.get("/vj").status_code, 403)
        self.assertEqual(self.client.get("/vj/estado?token=test-key").status_code, 403)
        listed = self.client.get("/api/admin/videos", headers={"X-Admin-Key": "test-key"})
        self.assertEqual(listed.status_code, 200)
        rows = listed.json["videos"]
        self.assertEqual([row["uid"] for row in rows], [nuevo, UID])
        self.assertEqual(rows[1]["nombre"], "Ana")
        self.assertEqual(rows[1]["saludo"], "Bravo")
        self.assertEqual(rows[0]["saludo"], "Más nuevo")
        self.assertNotIn("file_id", rows[0])
        panel = self.client.post("/admin/videos", data={"key": "test-key"})
        self.assertEqual(panel.status_code, 200)
        self.assertIn("Lanzar", panel.get_data(as_text=True))
        self.assertIn("Repetir", panel.get_data(as_text=True))
        self.assertIn("Descargar original", panel.get_data(as_text=True))
        self.assertNotIn("key=", panel.get_data(as_text=True))
        with patch.dict("os.environ", {"VJ_OUTPUT_TOKEN": "salida-secreta"}):
            opened = self.client.post("/admin/videos", data={"key": "test-key"})
            html = opened.get_data(as_text=True)
            self.assertIn("http://localhost/vj?token=salida-secreta", html)
            self.assertNotIn("token=test-key", html)
            self.assertNotIn("ADMIN_KEY", html)
            cerrado = self.client.get("/vj?token=salida-secreta")
            self.assertEqual(self.client.get("/vj").status_code, 403)
            self.assertEqual(self.client.get("/vj?token=otra").status_code, 403)
        self.assertEqual(cerrado.status_code, 200)
        salida = cerrado.get_data(as_text=True)
        self.assertIn("muted", salida)
        self.assertIn("background: #000", salida)
        self.assertIn("setInterval(poll, 2000)", salida)
        self.assertNotIn("controls", salida)
        self.assertNotIn("Lanzar", salida)
        self.assertNotIn("test-key", salida)
        self.assertNotIn("box-shadow", salida)
        launched = self.client.post(
            "/api/admin/vj",
            data={"action": "lanzar", "uid": nuevo},
            headers={"X-Admin-Key": "test-key"},
        )
        self.assertEqual(launched.status_code, 200)
        self.assertEqual(launched.json["estado"]["mode"], "once")
        self.assertEqual(launched.json["estado"]["uid"], nuevo)
        with patch.dict("os.environ", {"VJ_OUTPUT_TOKEN": "salida-secreta"}):
            estado = self.client.get("/vj/estado?token=salida-secreta")
        self.assertEqual(estado.json["uid"], nuevo)
        self.assertEqual(estado.json["mode"], "once")
        self.assertNotIn("file_id", estado.json)
        repetir = self.client.post(
            "/api/admin/vj",
            data={"action": "repetir", "uid": UID},
            headers={"X-Admin-Key": "test-key"},
        )
        self.assertEqual(repetir.json["estado"]["mode"], "loop")
        self.assertEqual(repetir.json["estado"]["uid"], UID)
        visto = self.client.post(
            "/api/admin/vj",
            data={"action": "visto", "uid": UID},
            headers={"X-Admin-Key": "test-key"},
        )
        self.assertEqual(visto.status_code, 200)
        marked = self.client.get("/api/admin/videos", headers={"X-Admin-Key": "test-key"}).json["videos"]
        self.assertTrue(next(row for row in marked if row["uid"] == UID)["seen"])
        stopped = self.client.post(
            "/api/admin/vj",
            data={"action": "parar"},
            headers={"X-Admin-Key": "test-key"},
        )
        self.assertEqual(stopped.json["estado"]["mode"], "stop")
        self.assertIsNone(stopped.json["estado"]["uid"])
        self.client.post(
            "/api/admin/vj",
            data={"action": "lanzar", "uid": nuevo},
            headers={"X-Admin-Key": "test-key"},
        )
        gone = self.client.post("/api/admin/borrar-video", data={"uid": nuevo, "key": "test-key"})
        self.assertEqual(gone.status_code, 200)
        self.assertNotIn(nuevo, [item.get("uid") for item in self.client.get("/api/loop-items").json["items"]])
        self.assertEqual(self.videos.vj_public_status()["mode"], "stop")
        original = self.client.get("/videos/" + UID + "?descarga=1")
        self.assertIn("attachment", original.headers["Content-Disposition"])
        self.assertIn(UID + ".mp4", original.headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main()
