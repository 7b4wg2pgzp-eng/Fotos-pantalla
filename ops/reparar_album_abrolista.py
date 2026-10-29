#!/usr/bin/env python3
"""Renueva en forma interactiva el OAuth del album de Oracle desde la Mac.

Solo modifica GOOGLE_REFRESH_TOKEN de /etc/abrolista-album.env (con respaldo),
reinicia abrolista-album y comprueba que la consulta a Drive vuelva a funcionar.
No modifica Abrolista, Fotos en Pantalla de Render ni archivos de Google Drive.
"""
import base64
import hashlib
import json
import secrets
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SSH_TARGET = 'abrolista'  # Atajo ya configurado en ~/.ssh/config de la Mac
ENV_FILE = '/etc/abrolista-album.env'
SCOPES = 'https://www.googleapis.com/auth/drive'

VERSION = 'MAC-CURL-3'

def https_json(url, post=None, bearer=None):
    """Usa curl del sistema con TLS verificado, sin credenciales en argumentos ni archivos."""
    if post is not None and bearer is not None:
        raise ValueError('Métodos HTTPS incompatibles')
    cmd = ['curl', '--silent', '--show-error', '--fail', '--max-time', '30',
           '--proto', '=https']
    payload = None
    if post is not None:
        cmd += ['--header', 'Content-Type: application/x-www-form-urlencoded',
                '--data-binary', '@-', url]
        payload = post
    elif bearer is not None:
        # curl lee las cabeceras desde stdin: el access token no aparece en ps.
        cmd += ['--config', '-']
        payload = ('url = ' + json.dumps(url) + '\n' +
                   'header = ' + json.dumps('Authorization: Bearer ' + bearer) + '\n').encode()
    else:
        cmd += [url]
    try:
        result = subprocess.run(cmd, input=payload, capture_output=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Error al ejecutar curl: ' + str(exc)) from exc
    if result.returncode:
        reason = result.stderr.decode(errors='replace').strip()[-450:]
        raise RuntimeError('curl no pudo completar HTTPS: ' + reason)
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError('Google no devolvió JSON válido por HTTPS.') from exc


REMOTE_READ = r'''
import hashlib, json, pathlib, shlex, sys
p = pathlib.Path('/etc/abrolista-album.env')
if not p.is_file():
    sys.exit('No existe el archivo privado del álbum.')
def parse(lines):
    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        try:
            parts = shlex.split(val.strip(), comments=False)
        except ValueError:
            sys.exit('Formato inválido del archivo de entorno.')
        result[key] = parts[0] if parts else ''
    return result
v = parse(p.read_text().splitlines())
keys = ('GOOGLE_CLIENT_ID','GOOGLE_CLIENT_SECRET','GOOGLE_REFRESH_TOKEN','GDRIVE_FOLDER_ID','GDRIVE_DATA_FOLDER_ID')
if not all(v.get(k) for k in keys):
    sys.exit('Falta una variable de OAuth o de Drive en el servidor.')
print(json.dumps({
    'client_id':v['GOOGLE_CLIENT_ID'],
    'client_secret':v['GOOGLE_CLIENT_SECRET'],
    'root_ids':[v['GDRIVE_FOLDER_ID'],v['GDRIVE_DATA_FOLDER_ID']],
    'old_hash':hashlib.sha256(v['GOOGLE_REFRESH_TOKEN'].encode()).hexdigest()
}))
'''

REMOTE_UPDATE = r'''
import hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile, datetime, urllib.error, urllib.request
path = pathlib.Path('/etc/abrolista-album.env')
payload=json.load(sys.stdin)
token=payload.get('new_refresh_token','')
if not isinstance(token,str) or len(token)<30 or '\n' in token or '\r' in token:
    sys.exit('Nuevo token inválido; no se alteró el servidor.')
old=path.read_text()
lines=old.splitlines(keepends=True)
positions=[i for i,line in enumerate(lines) if re.match(r'^\s*GOOGLE_REFRESH_TOKEN\s*=',line)]
if len(positions)!=1:
    sys.exit('No hay exactamente una variable GOOGLE_REFRESH_TOKEN; no se modificó nada.')
line=lines[positions[0]]
value=line.split('=',1)[1].strip()
import shlex
parts=shlex.split(value,comments=False)
previous=parts[0] if parts else ''
if hashlib.sha256(previous.encode()).hexdigest()!=payload.get('old_hash'):
    sys.exit('Otro proceso cambió la autorización; operación cancelada.')
if subprocess.run(['systemctl','is-active','--quiet','abrolista-album.service']).returncode!=0:
    sys.exit('Servicio de álbum inactivo antes de modificar; operación cancelada.')
backup_dir=pathlib.Path('/var/backups/abrolista/renovaciones-oauth')
backup_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
os.chmod(backup_dir,0o700)
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup=backup_dir/('abrolista-album.env.'+stamp+'.bak')
if backup.exists():
    sys.exit('Ya existe el respaldo, operación cancelada.')
shutil.copy2(path,backup)
os.chmod(backup,0o600)
lines[positions[0]]='GOOGLE_REFRESH_TOKEN='+json.dumps(token)+'\n'
fd, tmp=tempfile.mkstemp(prefix='.album-oauth-',dir=str(path.parent))
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as writer:
        writer.writelines(lines)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(tmp,path)
except BaseException:
    try: os.unlink(tmp)
    except OSError: pass
    raise
subprocess.run(['systemctl','restart','abrolista-album.service'],check=True)
healthy=False
for _ in range(8):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8002/healthz',timeout=4) as response:
            healthy = response.status==200
        if healthy: break
    except Exception:
        import time;time.sleep(1)
if not healthy:
    sys.exit('Se cambió el token, pero el servicio no respondió al healthcheck; revisá journalctl.')
# Sin acceso privado a álbum alguno debe devolver 404; un OAuth roto produciría 500.
try:
    urllib.request.urlopen('http://127.0.0.1:8002/vicky-herrera',timeout=35)
    status=200
except urllib.error.HTTPError as ex:
    status=ex.code
except Exception:
    status=0
print(json.dumps({'updated':True,'health_ok':healthy,'album_unauthorized_status':status,
                  'gallery_drive_ok': status == 404,'backup_created':True}))
'''


def ssh_run(remote_code, stdin=None):
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12', '-T', SSH_TARGET,
           'sudo -n python3 -c ' + shlex.quote(remote_code)]
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True, timeout=120)


def main():
    print('REPARADOR VERSIÓN ' + VERSION)
    print('Reparación del álbum Abrolista. No se borran fotos.')
    print('0. Comprobando HTTPS de Google con curl de macOS...')
    try:
        https_json('https://accounts.google.com/.well-known/openid-configuration')
    except RuntimeError as exc:
        sys.exit('No se pudo validar HTTPS de Google con curl. ' + str(exc))
    print('HTTPS verificado con curl y certificados del sistema.')
    print('1. Revisando la conexión SSH y la configuración privada...')
    try:
        read=ssh_run(REMOTE_READ)
    except (OSError,subprocess.TimeoutExpired) as e:
        sys.exit('No se pudo conectar por SSH a Oracle: '+str(e))
    if read.returncode:
        sys.exit('SSH o sudo no disponible. '+read.stderr.strip()[-700:])
    try:
        config=json.loads(read.stdout)
    except ValueError:
        sys.exit('El servidor no devolvió la configuración esperada.')
    print('2. Abriendo Google para autorizar la cuenta técnica de Fotos en Pantalla...')
    print('   Usá la cuenta cuentadeproduccionmusical@gmail.com.')
    state=secrets.token_urlsafe(32)
    verifier=secrets.token_urlsafe(72)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    oauth_result={}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # no registrar códigos de autorización en logs
        def do_GET(self):
            parsed=urllib.parse.urlparse(self.path)
            params=urllib.parse.parse_qs(parsed.query)
            if params.get('state',[''])[0] != state:
                self.send_error(400,'Estado OAuth no válido');return
            oauth_result['code']=params.get('code',[''])[0]
            oauth_result['error']=params.get('error',[''])[0]
            self.send_response(200 if oauth_result['code'] else 400)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            self.wfile.write(b'<h2>Autorizacion recibida. Ya podes volver a la terminal.</h2>')

    server=HTTPServer(('127.0.0.1',0),Handler)
    server.timeout=240
    redirect=f'http://127.0.0.1:{server.server_port}/'
    authorization_url='https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode({
        'client_id':config['client_id'], 'redirect_uri':redirect, 'response_type':'code',
        'scope':SCOPES,'access_type':'offline','prompt':'consent','state':state,
        'code_challenge':challenge, 'code_challenge_method':'S256'
    })
    if not webbrowser.open(authorization_url):
        print('Abrí este enlace en el navegador de la Mac:',authorization_url)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if not oauth_result.get('code'):
        sys.exit('Google no entregó autorización: '+oauth_result.get('error','no se completó el ingreso'))
    post=urllib.parse.urlencode({
        'client_id':config['client_id'], 'client_secret':config['client_secret'],
        'code':oauth_result['code'],'code_verifier':verifier,
        'redirect_uri':redirect,'grant_type':'authorization_code'
    }).encode()
    try:
        token_response = https_json('https://oauth2.googleapis.com/token', post=post)
    except RuntimeError as exc:
        sys.exit('No se pudo canjear la autorización OAuth. ' + str(exc))
    new_refresh=token_response.get('refresh_token')
    access=token_response.get('access_token')
    if not new_refresh or not access:
        sys.exit('Google no entregó un refresh token nuevo. No se cambió el servidor.')
    print('3. Comprobando que la autorización nueva puede leer las carpetas reales...')
    for folder_id in config['root_ids']:
        url='https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(folder_id,safe='')+'?'+urllib.parse.urlencode({
            'fields':'id,name,mimeType','supportsAllDrives':'true'})
        try:
            info = https_json(url, bearer=access)
            if info.get('id') != folder_id:
                sys.exit('La cuenta Google no coincide con las carpetas del álbum. Sin cambios.')
        except RuntimeError as exc:
            sys.exit('La cuenta autorizada no puede abrir las carpetas del álbum. Sin cambios. ' + str(exc))
    print('4. Guardando la credencial en Oracle, con respaldo y reinicio limitado al álbum...')
    payload={'old_hash':config['old_hash'],'new_refresh_token':new_refresh}
    result=ssh_run(REMOTE_UPDATE,json.dumps(payload))
    if result.returncode:
        sys.exit('La actualización del servidor falló. '+result.stderr.strip()[-700:])
    try:
        status=json.loads(result.stdout)
    except ValueError:
        sys.exit('Respuesta inesperada del servidor; revisá el servicio.')
    if status.get('gallery_drive_ok'):
        print('ÁLBUM RECUPERADO: Drive responde, el servicio está activo y la ruta dejó de dar 500.')
        print('Verificá el link privado real de Vicky desde el panel de Abrolista.')
    else:
        print('El servicio reinició, pero la galería todavía devuelve HTTP',status.get('album_unauthorized_status'))
        print('Revisá: ssh abrolista "sudo journalctl -u abrolista-album -n 80 --no-pager"')
    print('Importante: Render Fotos-pantalla usa otra copia del token y aún necesita actualizarse.')
    print('La pantalla OAuth sigue en modo Prueba, como se había decidido; este acceso puede vencer otra vez en 7 días.')

if __name__=='__main__':
    main()