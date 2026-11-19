"""Worker de conversión H.264 del VPS. Drive y la app son falsos; ffmpeg es real si está."""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import screen_convert_worker as worker

UID = "a" * 32
OTHER = "b" * 32


def registry(*rows):
    return {"videos": list(rows)}


def row(uid=UID, state="pending", ts=None, file_id="orig-id", size=1000):
    data = dict(uid=uid, name=uid + ".mov", file_id=file_id, ext="mov", mime="video/quicktime",
                size=size, duration=10.0, ts=time.time() if ts is None else ts, playable=True)
    if state:
        data["screen"] = {"state": state}
    return data


class FakeStore:
    def __init__(self, data, folder="copies"):
        self.data = data
        self.folder_id = folder
        self.copies = {}
        self.downloads = []
        self.uploads = []
        self.deleted = []
        self.fail_download = False

    def registry(self):
        return json.loads(json.dumps(self.data))

    def folder(self, name):
        assert name == worker.CONVERT_FOLDER_NAME
        return self.folder_id

    def existing_copy(self, folder, uid):
        return self.copies.get(uid)

    def download(self, file_id, path):
        self.downloads.append(file_id)
        if self.fail_download:
            raise IOError("drive caído")
        Path(path).write_bytes(b"original")
        return 8

    def upload(self, path, folder, uid, source_id):
        self.uploads.append((Path(path).read_bytes(), folder, uid, source_id))
        copy = dict(file_id="copy-" + uid[:4], size=os.path.getsize(path))
        self.copies[uid] = copy
        return copy

    def delete(self, file_id):
        self.deleted.append(file_id)


class FakeNotifier:
    def __init__(self, done=200, failed=200):
        self.done_status = done
        self.failed_status = failed
        self.calls = []

    def done(self, uid, file_id, size):
        self.calls.append(("done", uid, file_id, size))
        return self.done_status

    def failed(self, uid):
        self.calls.append(("failed", uid))
        return self.failed_status


def fake_transcode(source, target, long_side=1280, timeout=1200):
    Path(target).write_bytes(b"h264:" + Path(source).read_bytes())
    return dict(size=os.path.getsize(target), seconds=0.1, hdr=False)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden in tests"))
        network.start()
        self.addCleanup(network.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def make(self, store, notifier, transcoder=fake_transcode):
        return worker.Worker(store, notifier, self.temp.name, transcoder=transcoder)

    def test_pending_rows_are_oldest_first_and_ignore_others(self):
        now = time.time()
        data = registry(
            row(uid=OTHER, ts=now - 10),
            row(uid=UID, ts=now - 20),
            row(uid="c" * 32, state="ready"),
            row(uid="d" * 32, state=None),
            row(uid="e" * 32, ts=now - worker.MAX_AGE_SECONDS - 5),
            row(uid="NOT-A-UID"),
            row(uid="f" * 32, size=worker.MAX_BYTES + 1),
            "basura",
        )
        self.assertEqual([r["uid"] for r in worker.pending_rows(data, now)], [UID, OTHER])
        self.assertEqual([r["uid"] for r in worker.pending_rows(data, now, skip={UID})], [OTHER])
        self.assertEqual(worker.pending_rows({"nada": 1}), [])

    def test_ffmpeg_command_is_single_thread_720p_h264_faststart(self):
        cmd = worker.ffmpeg_command("in.mov", "out.mp4", 1280, has_audio=True, hdr=False)
        joined = " ".join(cmd)
        self.assertEqual(cmd.count("-threads"), 2)
        self.assertTrue(all(cmd[i + 1] == "1" for i, part in enumerate(cmd) if part == "-threads"))
        for expected in ("libx264", "veryfast", "yuv420p", "+faststart", "min(1280,iw)", "min(1280,ih)",
                         "-fpsmax 30", "-c:a aac", "-nostdin"):
            self.assertIn(expected, joined)
        self.assertNotIn("tonemap", joined)
        silent = worker.ffmpeg_command("in.mov", "out.mp4", 1280, has_audio=False, hdr=True)
        self.assertIn("-an", silent)
        self.assertNotIn("0:a:0", silent)
        self.assertIn("tonemap", " ".join(silent))

    def test_converts_uploads_and_tells_the_app(self):
        store = FakeStore(registry(row()))
        notifier = FakeNotifier()
        job = self.make(store, notifier)
        self.assertEqual(job.run_once(), UID)
        self.assertEqual(store.downloads, ["orig-id"])
        self.assertEqual(store.uploads, [(b"h264:original", "copies", UID, "orig-id")])
        self.assertEqual(notifier.calls, [("done", UID, "copy-aaaa", 13)])
        # La app todavía no reescribió el registro: no se vuelve a convertir.
        self.assertIsNone(job.run_once())
        self.assertEqual(len(store.uploads), 1)
        self.assertEqual(os.listdir(self.temp.name), [])

    def test_reuses_a_copy_uploaded_before_a_crash(self):
        store = FakeStore(registry(row()))
        store.copies[UID] = dict(file_id="old-copy", size=99)
        notifier = FakeNotifier()
        self.make(store, notifier).run_once()
        self.assertEqual(store.downloads, [])
        self.assertEqual(notifier.calls, [("done", UID, "old-copy", 99)])

    def test_gives_up_after_two_tries_and_the_app_shows_the_original(self):
        store = FakeStore(registry(row()))
        store.fail_download = True
        notifier = FakeNotifier()
        job = self.make(store, notifier)
        job.run_once()
        self.assertEqual(notifier.calls, [])
        job.run_once()
        self.assertEqual(notifier.calls, [("failed", UID)])
        self.assertIsNone(job.run_once())
        self.assertEqual(len(store.downloads), 2)

    def test_app_unreachable_keeps_the_copy_and_retries_the_notice(self):
        store = FakeStore(registry(row()))
        notifier = FakeNotifier(done=503)
        job = self.make(store, notifier)
        job.run_once()
        notifier.done_status = 200
        job.run_once()
        self.assertEqual(len(store.uploads), 1)
        self.assertEqual([call[0] for call in notifier.calls], ["done", "done"])
        self.assertIsNone(job.run_once())

    def test_moderated_meanwhile_is_dropped(self):
        store = FakeStore(registry(row()))
        notifier = FakeNotifier(done=410)
        job = self.make(store, notifier)
        job.run_once()
        self.assertIsNone(job.run_once())

    def test_rejected_copy_is_deleted(self):
        store = FakeStore(registry(row()))
        notifier = FakeNotifier(done=400)
        job = self.make(store, notifier)
        job.run_once()
        self.assertEqual(store.deleted, ["copy-aaaa"])

    def test_waits_while_the_copy_folder_does_not_exist(self):
        store = FakeStore(registry(row()), folder=None)
        notifier = FakeNotifier()
        self.assertIsNone(self.make(store, notifier).run_once())
        self.assertEqual(store.downloads, [])

    def test_notifier_sends_the_token_only_to_https(self):
        with self.assertRaises(ValueError):
            worker.AppNotifier("http://evil.example", "x" * 40)
        seen = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            seen.append(request)
            return Response()

        notifier = worker.AppNotifier("https://fotos.example/", "x" * 40, opener=opener)
        self.assertEqual(notifier.done(UID, "copy-id", 12), 200)
        self.assertEqual(seen[0].full_url, f"https://fotos.example/api/convert/{UID}/listo")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer " + "x" * 40)
        self.assertEqual(json.loads(seen[0].data), {"file_id": "copy-id", "size": 12})

    def test_service_unit_is_gentle_with_the_shared_vm(self):
        unit = Path("ops/fotospantalla-convert.service").read_text(encoding="utf-8")
        for expected in ("Nice=", "MemoryMax=", "CPUWeight=", "EnvironmentFile=/etc/abrolista-album.env",
                         "EnvironmentFile=/etc/fotospantalla-convert.env", "screen_convert_worker.py"):
            self.assertIn(expected, unit)
        self.assertNotIn("TOKEN=", unit)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg no instalado")
class RealFfmpegTests(unittest.TestCase):
    def test_hevc_portrait_becomes_720p_h264_with_faststart(self):
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libx265" not in encoders or "libx264" not in encoders:
            self.skipTest("ffmpeg sin libx265/libx264")
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "in.mov")
            target = os.path.join(folder, "out.mp4")
            subprocess.run([
                "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
                "-x265-params", "log-level=error", "-c:a", "aac", "-shortest", source,
            ], check=True, timeout=120)
            stats = worker.transcode(source, target, long_side=1280, timeout=120)
            self.assertGreater(stats["size"], 0)
            out = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt",
                 "-of", "json", target], capture_output=True, text=True, check=True).stdout)["streams"]
            video = next(s for s in out if s["codec_type"] == "video")
            self.assertEqual((video["codec_name"], video["pix_fmt"]), ("h264", "yuv420p"))
            self.assertEqual((video["width"], video["height"]), (720, 1280))
            self.assertIn("aac", [s["codec_name"] for s in out])
            data = Path(target).read_bytes()
            self.assertLess(data.find(b"moov"), data.find(b"mdat"))


if __name__ == "__main__":
    unittest.main()
