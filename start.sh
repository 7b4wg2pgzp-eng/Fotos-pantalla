#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

PORT="${PORT:-5050}"
export EVENTPHOTOS_DIR="${EVENTPHOTOS_DIR:-$HOME/Mi unidad/EventPhotos}"

echo "Arrancando servidor Flask..."
python3 -u app.py > /tmp/eventphotos-app.log 2>&1 &
APP_PID=$!
sleep 2

echo "Arrancando túnel de Cloudflare (URL aleatoria, sin cuenta)..."
cloudflared tunnel --url "http://localhost:${PORT}" > /tmp/eventphotos-tunnel.log 2>&1 &
TUNNEL_PID=$!

URL=""
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' /tmp/eventphotos-tunnel.log | head -n1)
  if [ -n "$URL" ]; then
    break
  fi
  sleep 1
done

if [ -z "$URL" ]; then
  echo "No se pudo obtener la URL del túnel todavía."
  echo "Revisá /tmp/eventphotos-tunnel.log manualmente (puede tardar unos segundos más)."
  echo "PIDs -> flask:$APP_PID tunnel:$TUNNEL_PID"
  exit 1
fi

echo ""
echo "URL para invitados (datos móviles, sin wifi del salón): $URL"
echo ""

python3 make_qr.py "$URL"

echo ""
echo "Todo corriendo. Para bajarlo: ./stop.sh"
echo "PIDs -> flask:$APP_PID tunnel:$TUNNEL_PID"
