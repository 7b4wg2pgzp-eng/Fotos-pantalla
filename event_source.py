"""Resuelve fiestas de Abrolista por hora local. Nunca inventa un evento."""
import os
import json
import time
import threading
import urllib.request
from urllib.parse import urlparse
from datetime import datetime, timezone
from delivery import parse_date, validate_config

class EventSource:
    def __init__(self, delivery, reset_runtime, fetch=None, close_runtime=None, closure_messages=None):
        self.delivery, self.reset_runtime = delivery, reset_runtime
        self.close_runtime = close_runtime
        self.closure_messages = closure_messages
        self.fetch = fetch or self.fetch_remote
        self.lock = threading.RLock()
        self.rows=[]
        self.checked=0
        self.ready=False

    def fetch_remote(self):
        url = os.environ.get('ABROLISTA_EVENT_SOURCE_URL','')
        token = os.environ.get('ABROLISTA_EVENT_SOURCE_TOKEN','')
        if urlparse(url).scheme != 'https' or urlparse(url).netloc != 'abrolista.com' or not token:
            raise RuntimeError('Falta configurar la conexión privada con Abrolista.')
        req=urllib.request.Request(url,headers={'X-EventPhotos-Key':token,'Accept':'application/json','User-Agent':'Abrolista-EventPhotos/1.0'})
        with urllib.request.urlopen(req,timeout=12) as response:
            data=json.loads(response.read(131072))
        if not data.get('ok') or not isinstance(data.get('events'),list):
            raise RuntimeError('Abrolista no devolvió una agenda válida.')
        return data['events']

    def refresh(self):
        now=time.time()
        if not self.ready or now-self.checked >= 60:
            self.rows=self.fetch()
            self.checked=now
            self.ready=True
        return self.rows

    def active(self, now=None):
        with self.lock:
            now=now or datetime.now(timezone.utc)
            rows=self.refresh()
            active=[r for r in rows if parse_date(r['start']) <= now < parse_date(r['end'])]
            if len(active)>1:
                raise RuntimeError('Hay dos eventos de Abrolista en el mismo horario. No se mezclaron sus archivos.')
            return active[0] if active else None

    def ensure(self, messages, now=None):
        with self.lock, self.delivery.lock:
            now = now or datetime.now(timezone.utc)
            event=self.active(now)
            previous=self.delivery.load_config()
            # El cierre se procesa en el primer acceso después de que termina el
            # evento, aunque NO exista otro evento activo. La fase pendiente evita
            # sobrescribir saludos archivados si una limpieza queda interrumpida.
            if (previous and previous.get('enabled') and not previous.get('closed')
                    and parse_date(previous['end']) <= now):
                if not previous.get('close_pending'):
                    self.delivery.reconcile(self.closure_messages() if self.closure_messages else messages)
                    previous=dict(previous,close_pending=True)
                    self.delivery.save_config(previous)
                if self.close_runtime:
                    self.close_runtime(previous)
                previous=dict(previous,close_pending=False,closed=True)
                self.delivery.save_config(previous)
            if not event:
                return None
            config=validate_config(dict(name=event['name'],start=event['start'],end=event['end'],enabled=True,album_slug=event.get('album_slug','')))
            # ID de Abrolista estable: un cambio de nombre no vuelve a limpiar el evento en curso.
            config['source_id']=str(event['id'])
            previous=self.delivery.load_config()
            same = (previous and previous.get('source_id') == config['source_id']
                    and previous['start'] == config['start'] and previous['end'] == config['end'])
            if same and not previous.get('reset_pending'):
                return previous
            if previous and previous.get('enabled') and not same and not previous.get('closed'):
                self.delivery.reconcile(messages)
                self.delivery.on_messages(messages)
            # Transición persistente: si falla la limpieza se reanuda sin perder el archivo de saludos anterior.
            config['reset_pending']=True
            self.delivery.save_config(config)
            self.reset_runtime(config, previous)
            config['reset_pending']=False
            self.delivery.save_config(config)
            return self.delivery.config

    def status(self):
        event=self.active()
        rows=self.refresh()
        now=datetime.now(timezone.utc)
        upcoming=[e for e in rows if parse_date(e['start'])>now]
        return dict(active=event,next_event=upcoming[0] if upcoming else None,source='Abrolista')
