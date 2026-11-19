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
        self.videos._video_hits.clear()
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
        self.reads.clear()
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

    def test_loop_plays_new_videos_without_approval(self):
        self.add_video()
        registered = self.register()
        self.assertEqual(registered.status_code, 200)
        items = self.client.get("/api/loop-items").json["items"]
        self.assertIn(UID, [item.get("uid") for item in items if item.get("kind") == "video"])
        self.assertNotIn("vj_output_page", self.screen.app.view_functions)
        self.assertNotIn("vj_estado", self.screen.app.view_functions)
        self.assertNotIn(b"setInterval(poll, 2000)", self.client.get("/vj").data)

    def test_moderator_lists_previews_and_can_mark_or_delete(self):
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
        self.assertEqual(self.client.post("/api/admin/videos", data={"action": "visto", "uid": UID}).status_code, 403)
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
        html = panel.get_data(as_text=True)
        self.assertIn("Descargar original", html)
        self.assertIn("Marcar visto", html)
        self.assertIn("Borrar", html)
        self.assertNotIn("Lanzar", html)
        self.assertNotIn("Repetir", html)
        self.assertNotIn("VJ_OUTPUT_TOKEN", html)
        self.assertNotIn("key=", html)
        self.assertEqual(self.client.post(
            "/api/admin/videos",
            data={"action": "lanzar", "uid": nuevo},
            headers={"X-Admin-Key": "test-key"},
        ).status_code, 400)
        visto = self.client.post(
            "/api/admin/videos",
            data={"action": "visto", "uid": UID},
            headers={"X-Admin-Key": "test-key"},
        )
        self.assertEqual(visto.status_code, 200)
        marked = self.client.get("/api/admin/videos", headers={"X-Admin-Key": "test-key"}).json["videos"]
        self.assertTrue(next(row for row in marked if row["uid"] == UID)["seen"])
        self.assertIn(nuevo, [item.get("uid") for item in self.client.get("/api/loop-items").json["items"]])
        gone = self.client.post("/api/admin/borrar-video", data={"uid": nuevo, "key": "test-key"})
        self.assertEqual(gone.status_code, 200)
        self.assertNotIn(nuevo, [item.get("uid") for item in self.client.get("/api/loop-items").json["items"]])
        original = self.client.get("/videos/" + UID + "?descarga=1")
        self.assertIn("attachment", original.headers["Content-Disposition"])
        self.assertIn(UID + ".mp4", original.headers["Content-Disposition"])

    def _mp4(self, *payloads):
        chunks = []
        for kind, payload in payloads:
            chunks.append((8 + len(payload)).to_bytes(4, "big") + kind + payload)
        return b"".join(chunks)

    def test_register_rejects_hevc_and_does_not_publish_it(self):
        hevc = self._mp4(
            (b"ftyp", b"isom" + b"\x00\x00\x00\x00"),
            (b"mdat", b"\x00" * 32),
            (b"moov", b"stsd" + b"hvc1"),
        )
        self.add_video(data=hevc)
        self.drive.rows[FILE_ID]["size"] = str(len(hevc))

        def read_range(file_id, start, end):
            self.reads.append((file_id, start, end))
            return hevc[start:end + 1]

        with patch.object(self.videos.deps, "read_range", read_range):
            response = self.register(size=len(hevc))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], self.videos.SCREEN_FORMAT_MESSAGE)
        self.assertEqual(self.client.get("/api/videos").json["videos"], [])
        self.assertNotIn(FILE_ID, self.drive.rows)
        self.assertTrue(any(end < start + 16 or start >= len(hevc) - 16 for _file, start, end in self.reads))
        plain = self._mp4((b"ftyp", b"isom" + b"\x00\x00\x00\x00"), (b"moov", b"stsd" + b"avc1"))
        self.assertFalse(self.videos.declares_hevc(lambda start, length: plain[start:start + length], len(plain)))
        self.assertTrue(self.videos.declares_hevc(lambda start, length: hevc[start:start + length], len(hevc)))

    def test_video_session_allows_three_per_ip_each_ten_minutes(self):
        for _turn in range(3):
            self.assertEqual(self.session().status_code, 200)
        blocked = self.session()
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("3 videos", blocked.json["error"])
        self.assertEqual(len(self.drive_calls), 3)
        again = self.client.post(
            "/api/videos/session",
            json=dict(ext="mp4", size=len(VIDEO_BYTES), mime="video/mp4", duration=12.5),
            headers={"CF-Connecting-IP": "203.0.113.8", "Origin": "https://evil.example"},
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(self.drive_calls), 4)

    def test_drive_calls_do_not_hold_the_event_lock(self):
        seen = []

        def probe():
            locked = []

            def other_thread():
                free = self.screen._event_lock.acquire(blocking=False)
                locked.append(free)
                if free:
                    self.screen._event_lock.release()

            thread = __import__("threading").Thread(target=other_thread)
            thread.start()
            thread.join()
            seen.append(locked[0])

        def drive_request(url, method, body, headers):
            probe()
            return 200, {"location": "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=test"}, b""

        with patch.object(self.videos.deps, "drive_request", drive_request):
            self.assertEqual(self.session().status_code, 200)
        self.add_video()

        probed = []

        def read_range(file_id, start, end):
            if not probed:
                probe()
                probed.append(True)
            return VIDEO_BYTES[start:end + 1]

        with patch.object(self.videos.deps, "read_range", read_range):
            self.assertEqual(self.register().status_code, 200)
        self.assertEqual(seen, [True, True])
        with patch.object(self.screen._event_source, "ensure", return_value=None):
            closed = self.session()
        self.assertEqual(closed.status_code, 409)


if __name__ == "__main__":
    unittest.main()
