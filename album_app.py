"""Galería permanente: Drive y Abrolista local, sin cargar la app de pantalla."""
import os
import time
from flask import Flask, jsonify
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pillow_heif import register_heif_opener
from delivery import Delivery
from gallery import Gallery, register_gallery


class ResidentGallery(Gallery):
    def manifests(self):
        with self.lock:
            now = time.monotonic()
            if now >= getattr(self, '_manifests_until', 0):
                self._manifests = super().manifests()
                self._manifests_until = now + 15
            return self._manifests


def create_app(service=None):
    register_heif_opener()
    required = ('GALLERY_LINK_SECRET', 'ABROLISTA_EVENT_SOURCE_TOKEN',
                'GDRIVE_FOLDER_ID', 'GDRIVE_DATA_FOLDER_ID')
    if any(not os.environ.get(key) for key in required):
        raise RuntimeError('Falta configurar el entorno privado del álbum.')
    if len(os.environ['GALLERY_LINK_SECRET']) < 32:
        raise RuntimeError('La clave de enlaces del álbum no es válida.')
    if service is None:
        credentials = Credentials(
            None, refresh_token=os.environ['GOOGLE_REFRESH_TOKEN'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=os.environ['GOOGLE_CLIENT_ID'],
            client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
            scopes=['https://www.googleapis.com/auth/drive'])
        service = build('drive', 'v3', credentials=credentials, cache_discovery=False)
    app = Flask(__name__)
    delivery = Delivery(service, os.environ['GDRIVE_FOLDER_ID'],
                        lambda: os.environ['GDRIVE_DATA_FOLDER_ID'], 'unused-gallery-state.json')
    gallery = ResidentGallery(delivery, os.environ['GALLERY_LINK_SECRET'],
                              album_source_url='http://127.0.0.1:8000/api/eventphotos/album')
    register_gallery(app, gallery)
    app.extensions['album_gallery'] = gallery

    @app.get('/healthz')
    def health():
        return jsonify(ok=True)

    return app
