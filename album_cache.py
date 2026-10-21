"""Bounded, disposable thumbnail bytes shared by album workers; never authorization."""
import hashlib
import os
from pathlib import Path
import tempfile

MAX_BYTES = 128 * 1024 * 1024
MAX_IMAGE = 256 * 1024


class ThumbnailCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def read(self, key):
        path = self.directory / key
        try:
            with path.open('rb') as handle:
                payload = handle.read(MAX_IMAGE + 33)
            if not 32 < len(payload) <= MAX_IMAGE + 32:
                return None
            digest, data = payload[:32], payload[32:]
            if hashlib.sha256(data).digest() != digest:
                return None
            os.utime(path, None)
            return data
        except OSError:
            return None

    def discard(self, key):
        try:
            (self.directory / key).unlink(missing_ok=True)
        except OSError:
            pass  # Never authorize a photo because local cleanup failed.

    def write(self, key, data):
        if not data or len(data) > MAX_IMAGE:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix='.tmp-', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(hashlib.sha256(data).digest() + data)
            os.replace(temporary, self.directory / key)
            # Atomic writes and age-based eviction are safe across synchronous workers.
            files = []
            for path in self.directory.iterdir():
                if len(path.name) != 64 or any(c not in '0123456789abcdef' for c in path.name):
                    continue
                try:
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
                except OSError:
                    continue
            total = sum(size for _, size, _ in files)
            for _, size, path in sorted(files):
                if total <= MAX_BYTES:
                    break
                try:
                    path.unlink(missing_ok=True)
                    total -= size
                except OSError:
                    continue
        except OSError:
            pass  # Drive remains the persistent source if the optional cache is unavailable.
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
