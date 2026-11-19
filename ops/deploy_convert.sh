#!/usr/bin/env bash
# Instala o actualiza el worker que pasa a H.264 los videos HEVC de /loop.
# Ejecutar EN el VPS como root: sudo bash deploy_convert.sh <SHA-de-40-caracteres>
# No toca el álbum, AbroLista, nginx ni /etc/abrolista-album.env.
# Requiere /etc/fotospantalla-convert.env creado a mano (ver abajo).
set -Eeuo pipefail
umask 022

REVISION=${1:-}
[[ ${REVISION} =~ ^[0-9a-f]{40}$ ]] || { echo 'Indicar el SHA completo como primer argumento.' >&2; exit 1; }
REPOSITORY=https://github.com/7b4wg2pgzp-eng/Fotos-pantalla.git
APP=/opt/fotospantalla-convert
UNIT=/etc/systemd/system/fotospantalla-convert.service
ENV_FILE=/etc/fotospantalla-convert.env
PY=/opt/abrolista-album/venv/bin/python

[[ ${EUID} -eq 0 ]] || { echo 'Ejecutar con sudo.' >&2; exit 1; }
[[ -x ${PY} ]] || { echo "No está ${PY} (venv del álbum)." >&2; exit 1; }
[[ -f /etc/abrolista-album.env ]] || { echo 'Falta /etc/abrolista-album.env.' >&2; exit 1; }
[[ -f ${ENV_FILE} ]] || {
  echo "Falta ${ENV_FILE}. Crearlo con:" >&2
  echo "  SCREEN_CONVERT_URL=https://fotospantalla.nicovasquez.com.ar" >&2
  echo "  SCREEN_CONVERT_TOKEN=<el mismo valor que en Render>" >&2
  echo "y: chown root:abrolista-album ${ENV_FILE}; chmod 0640 ${ENV_FILE}" >&2
  exit 1
}
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo 'Instalando ffmpeg...'
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends ffmpeg
fi
ENCODERS="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
case "${ENCODERS}" in *libx264*) ;; *) echo 'ffmpeg sin libx264.' >&2; exit 1 ;; esac

STAGE="$(mktemp -d)"
trap 'rm -rf -- "${STAGE}"' EXIT
git init -q "${STAGE}/src"
git -C "${STAGE}/src" remote add origin "${REPOSITORY}"
git -C "${STAGE}/src" fetch -q --depth 1 origin "${REVISION}"
git -C "${STAGE}/src" checkout -q --detach FETCH_HEAD
[[ "$(git -C "${STAGE}/src" rev-parse HEAD)" == "${REVISION}" ]] || { echo 'La revisión no coincide.' >&2; exit 1; }

install -d -m 0755 "${APP}"
install -m 0644 "${STAGE}/src/screen_convert_worker.py" "${APP}/screen_convert_worker.py.new"
"${PY}" -m py_compile "${APP}/screen_convert_worker.py.new"
mv -f "${APP}/screen_convert_worker.py.new" "${APP}/screen_convert_worker.py"
printf '%s\n' "${REVISION}" > "${APP}/REVISION"
install -m 0644 "${STAGE}/src/ops/fotospantalla-convert.service" "${UNIT}"

echo 'Probando Drive y la configuración...'
bash -c "set -a; . /etc/abrolista-album.env; . ${ENV_FILE}; set +a; HOME=/tmp exec runuser -u abrolista-album -- ${PY} ${APP}/screen_convert_worker.py --check"

systemctl daemon-reload
systemctl enable fotospantalla-convert.service
systemctl restart fotospantalla-convert.service
sleep 5
systemctl is-active --quiet fotospantalla-convert.service || { journalctl -u fotospantalla-convert -n 30 --no-pager >&2; exit 1; }
echo "Worker activo en ${REVISION}. Logs: journalctl -u fotospantalla-convert -f"
echo 'Para apagarlo: systemctl disable --now fotospantalla-convert'
