#!/usr/bin/env bash
# Deploy transaccional de la galería permanente en Oracle Ubuntu.
# Ejecutar DESDE el VPS como root: sudo bash ops/deploy_album.sh
# No modifica /etc/*.env, nginx, Abrolista ni la base de datos.
set -Eeuo pipefail
umask 027

APP_ROOT=/opt/abrolista-album
CURRENT="${APP_ROOT}/current"
RELEASES="${APP_ROOT}/releases"
VENV="${APP_ROOT}/venv"
SERVICE=abrolista-album.service
# SHA exacto del cambio aprobado y probado en GitHub (2026-10-24).
REVISION=${1:-}
[[ ${REVISION} =~ ^[0-9a-f]{40}$ ]] || { echo 'Indicar el SHA completo auditado como primer argumento.' >&2; exit 1; }
REPOSITORY=https://github.com/7b4wg2pgzp-eng/Fotos-pantalla.git

if [[ ${EUID} -ne 0 ]]; then
  echo 'Es necesario ejecutar con sudo (en el servidor de Abrolista).' >&2
  exit 1
fi
for command in git curl systemctl readlink cp install ln mv mktemp; do
  command -v "${command}" >/dev/null || { echo "Falta ${command}" >&2; exit 1; }
done
[[ -L "${CURRENT}" && -d "${RELEASES}" && -x "${VENV}/bin/python" ]] || {
  echo 'No coincide con la instalación de álbum permanente esperada. No se modificó nada.' >&2
  exit 1
}
systemctl is-active --quiet "${SERVICE}" || {
  echo 'Servicio del álbum no está activo. Revisar antes de desplegar.' >&2
  exit 1
}
OLD="$(readlink -f "${CURRENT}")"
[[ -f "${OLD}/album_app.py" && -f "${OLD}/gallery.py" ]] || {
  echo 'La versión anterior no coincide con la instalación prevista.' >&2
  exit 1
}
STAGE="$(mktemp -d)"
LINK_CHANGED=0
NEW="${RELEASES}/$(date -u +%Y%m%d-%H%M%S)-${REVISION:0:8}"
cleanup() {
  status=$?
  if [[ ${status} -ne 0 && ${LINK_CHANGED} -eq 1 ]]; then
    echo 'Falló el despliegue. Restaurando la versión anterior...' >&2
    ln -s "${OLD}" "${CURRENT}.rollback.$$"
    mv -Tf "${CURRENT}.rollback.$$" "${CURRENT}"
    systemctl restart "${SERVICE}" || true
  fi
  rm -rf -- "${STAGE}"
  exit "${status}"
}
trap cleanup EXIT

echo 'Descargando la revisión exacta desde GitHub...'
git init -q "${STAGE}/src"
git -C "${STAGE}/src" remote add origin "${REPOSITORY}"
git -C "${STAGE}/src" fetch -q --depth 1 origin "${REVISION}"
git -C "${STAGE}/src" checkout -q --detach FETCH_HEAD
[[ "$(git -C "${STAGE}/src" rev-parse HEAD)" == "${REVISION}" ]] || {
  echo 'La revisión descargada no coincide con la esperada.' >&2
  exit 1
}

echo 'Preparando una publicación aislada, conservando secretos y versiones anteriores...'
[[ ! -e "${NEW}" ]] || { echo "Ya existe la versión ${NEW}" >&2; exit 1; }
cp -a -- "${OLD}" "${NEW}"
for file in album_app.py gallery.py delivery.py album_media.py album_cache.py prepare_album_thumbnails.py; do
  install -m 0644 "${STAGE}/src/${file}" "${NEW}/${file}"
done
install -d "${NEW}/templates" "${NEW}/static" "${NEW}/tests"
install -m 0644 "${STAGE}/src/templates/gallery.html" "${NEW}/templates/gallery.html"
install -m 0644 "${STAGE}/src/static/gallery.css" "${NEW}/static/gallery.css"
install -m 0644 "${STAGE}/src/static/gallery.js" "${NEW}/static/gallery.js"
install -m 0644 "${STAGE}/src/static/album_loader.js" "${NEW}/static/album_loader.js"
install -m 0644 "${STAGE}/src/tests/"test_*.py "${NEW}/tests/"
printf '%s\n' "${REVISION}" > "${NEW}/REVISION"

echo 'Comprobando sintaxis y pruebas de la galería antes del reinicio...'
(cd "${NEW}" && PYTHONDONTWRITEBYTECODE=1 "${VENV}/bin/python" -m unittest discover -q -s tests -p 'test_album_*.py')
(cd "${NEW}" && PYTHONDONTWRITEBYTECODE=1 "${VENV}/bin/python" -m py_compile album_app.py delivery.py gallery.py)

echo 'Activando la nueva versión...'
ln -s "${NEW}" "${CURRENT}.new.$$"
mv -Tf "${CURRENT}.new.$$" "${CURRENT}"
LINK_CHANGED=1
systemctl restart "${SERVICE}"
success=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if systemctl is-active --quiet "${SERVICE}" && curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8002/healthz | grep -q '"ok":true'; then
    success=1
    break
  fi
  sleep 1
done
[[ ${success} -eq 1 ]] || { echo 'Fallo la comprobación local /healthz.' >&2; exit 1; }
LINK_CHANGED=0
echo "Publicado: ${REVISION}"
echo 'La versión anterior sigue disponible para reversión:'
echo "${OLD}"
echo 'Pendiente: comprobación visual de un enlace privado real y carga Drive en producción.'
