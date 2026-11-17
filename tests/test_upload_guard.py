"""Límites de subida, preview y clave del moderador. Drive queda mockeado."""
import importlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from delivery import Delivery, validate_config
from test_moderation import FakeDrive


def jpeg_bytes(size=(64, 48), color=(20, 40, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def noisy_jpeg(size=(2200, 1400)):
    raw = __import__("os").urandom(size[0] * size[1] * 3)
    img = Image.frombytes("RGB", size, raw)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


class UploadGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.import_dir = tempfile.TemporaryDirectory()
        with patch.dict("os.environ", {
            "EVENTPHOTOS_DIR": cls.import_dir.name,
            "EVENTPHOTOS_CACHE": cls.import_dir.name,
        }, clear=True), patch("threading.Thread.start"):
            cls.screen = importlib.import_module("app")

    @classmethod
    def tearDownClass(cls):
        cls.import_dir.cleanup()

    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in tests"))
        network.start()
        self.addCleanup(network.stop)
        self.drive = FakeDrive()
        now = datetime.now(timezone.utc)
        self.config = validate_config(dict(
            name="Fiesta ficticia",
            start=(now - timedelta(hours=1)).isoformat(),
            end=(now + timedelta(hours=1)).isoformat(),
            enabled=True,
        ))
        self.delivery = Delivery(self.drive, "root", lambda: "data", "state.json")
        self.delivery.config = self.config
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache"
        self.originals = Path(self.temp.name) / "local"
        self.cache.mkdir()
        self.originals.mkdir()
        self.screen._upload_hits.clear()
        self.addCleanup(self.screen._upload_hits.clear)
        limit = patch.object(self.screen, "UPLOAD_LIMIT", 40)
        limit.start()
        self.addCleanup(limit.stop)
        content = patch.dict(self.screen.app.config, {"MAX_CONTENT_LENGTH": 40 * 1024 * 1024})
        content.start()
        self.addCleanup(content.stop)
        for name, value in dict(
            _drive_service=self.drive,
            _delivery=self.delivery,
            CACHE_DIR=self.cache,
            PHOTOS_DIR=self.originals,
            ADMIN_KEY="test-key",
            MESSAGES_FILE=self.cache / "mensajes.json",
        ).items():
            patcher = patch.object(self.screen, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        ensure = patch.object(self.screen._event_source, "ensure", return_value=self.config)
        ensure.start()
        self.addCleanup(ensure.stop)
        for name, value in dict(GDRIVE_FOLDER_ID="root", _update_delivery=lambda *args, **kwargs: None).items():
            patcher = patch.object(self.screen, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.screen.app.test_client()

    def drive_names(self):
        return [row["name"] for row in self.drive.rows.values()]

    def upload(self, payload, filename="foto.jpg", ip="203.0.113.10", **form):
        data = dict(form)
        if payload is not None:
            data["photos"] = (io.BytesIO(payload), filename)
        return self.client.post("/api/upload", data=data, headers={"CF-Connecting-IP": ip})

    def test_health_does_not_touch_drive(self):
        self.drive.fail_read = True
        self.drive.fail_write = True
        with patch.object(self.screen, "get_subfolder", side_effect=AssertionError("drive")), \
                patch.object(self.screen, "_restore_from_drive", side_effect=AssertionError("drive")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})
        self.assertLess(len(response.data), 80)

    def test_admin_without_env_key_cannot_choose_one(self):
        self.drive.fail_read = True
        self.drive.fail_write = True
        with patch.object(self.screen, "ADMIN_KEY", ""):
            page = self.client.get("/admin")
            posted = self.client.post("/admin", data={"nueva": "clave123", "repetir": "clave123"})
        self.assertEqual(page.status_code, 403)
        self.assertIn("ADMIN_KEY", page.get_data(as_text=True))
        self.assertNotIn("Elegí tu clave", page.get_data(as_text=True))
        self.assertEqual(posted.status_code, 403)
        self.assertEqual(posted.content_type, "text/plain; charset=utf-8")
        self.assertFalse(any(name.startswith("admin-") for name in self.drive_names()))

    def test_admin_login_keeps_working_with_env_key(self):
        self.drive.fail_read = True
        login = self.client.get("/admin")
        self.assertEqual(login.status_code, 200)
        self.assertIn("Necesitás la clave", login.get_data(as_text=True))
        self.assertNotIn("Elegí tu clave", login.get_data(as_text=True))
        ok = self.client.post("/admin", data={"key": "test-key"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("Comprobar respaldo", ok.get_data(as_text=True))
        bad = self.client.post("/admin", data={"key": "otra-clave"})
        self.assertEqual(bad.status_code, 403)
        self.assertIn("Clave incorrecta", bad.get_data(as_text=True))

    def test_heic_from_a_phone_is_a_real_image(self):
        buf = io.BytesIO()
        Image.new("RGB", (80, 60), (12, 34, 56)).save(buf, format="HEIF")
        response = self.upload(buf.getvalue(), filename="iphone.HEIC")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        stored = [row for row in self.drive.rows.values() if row["name"].lower().endswith(".heic")]
        self.assertEqual(len(stored), 1)
        self.assertTrue(response.get_json()["saved"][0].endswith(".jpg"))

    def test_text_renamed_as_jpeg_never_reaches_drive(self):
        before = set(self.drive.rows)
        response = self.upload(b"esto no es una foto", filename="malo.jpg")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(set(self.drive.rows), before)
        self.assertEqual(list(self.cache.glob("*.jpg")), [])

    def test_real_photo_is_resized_before_drive_and_is_cacheable(self):
        original = noisy_jpeg()
        response = self.upload(original, filename="fiesta.jpg", nombre="Ana", mensaje="Hola")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["mensaje"])
        stored = [row for row in self.drive.rows.values()
                  if row["name"].endswith(".jpg") and not row["name"].startswith("preview-")]
        self.assertEqual(len(stored), 1)
        self.assertEqual(self.drive.data[stored[0]["id"]], original)
        self.assertTrue(any(name.startswith("preview-") for name in self.drive_names()))
        preview_name = body["saved"][0]
        preview_path = self.cache / preview_name
        self.assertTrue(preview_path.is_file())
        with Image.open(preview_path) as preview:
            self.assertEqual(preview.format, "JPEG")
            self.assertTrue(preview.info.get("progressive"))
            self.assertLessEqual(max(preview.size), self.screen.PREVIEW_MAX_PX + 80)
            self.assertGreater(preview.size[0], 1000)
        self.assertLess(preview_path.stat().st_size, len(original))
        served = self.client.get("/photos/" + preview_name)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.headers["Cache-Control"], "private, max-age=43200, immutable")
        served.close()

    def test_drive_failure_does_not_publish_a_preview(self):
        with patch.object(self.screen, "save_original", side_effect=RuntimeError("drive caido")):
            response = self.upload(jpeg_bytes(), filename="foto.jpg")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(list(self.cache.glob("*.jpg")), [])
        self.assertFalse(any(name.startswith("preview-") or name.endswith(".jpg") for name in self.drive_names()))

    def test_rate_limit_uses_cloudflare_ip(self):
        with patch.object(self.screen, "UPLOAD_LIMIT", 3):
            for n in range(3):
                ok = self.upload(jpeg_bytes(color=(n, n, n)), filename=f"{n}.jpg", ip="198.51.100.7")
                self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
            blocked = self.upload(jpeg_bytes(), filename="de-mas.jpg", ip="198.51.100.7")
            other = self.upload(jpeg_bytes(), filename="otro.jpg", ip="198.51.100.8")
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("muchas fotos", blocked.get_json()["error"])
        self.assertEqual(other.status_code, 200)
        originals = [name for name in self.drive_names()
                     if name.endswith(".jpg") and not name.startswith("preview-")]
        self.assertEqual(len(originals), 4)

    def test_oversized_request_returns_friendly_413(self):
        self.screen.app.config["MAX_CONTENT_LENGTH"] = 600
        response = self.upload(b"x" * 4000, filename="grande.jpg")
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("pesado", response.get_json()["error"])
        self.assertFalse(any(name.endswith(".jpg") for name in self.drive_names()))

    def test_greeting_without_photo_still_works(self):
        response = self.client.post(
            "/api/upload",
            data={"nombre": "Nico", "mensaje": "Feliz cumple"},
            headers={"CF-Connecting-IP": "203.0.113.20"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["mensaje"])
        self.assertEqual(response.get_json()["saved"], [])
        self.assertIn("mensajes.json", self.drive_names())


if __name__ == "__main__":
    unittest.main()
