#!/bin/bash
PORT="${PORT:-5050}"
lsof -ti:"$PORT" | xargs kill -9 2>/dev/null
pkill -f "cloudflared tunnel" 2>/dev/null
echo "Servidor y túnel detenidos."
