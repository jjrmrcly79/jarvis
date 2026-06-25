#!/bin/bash
# Lanzador de J.A.R.V.I.S — núcleo local + interfaz HUD
# Uso: ./launch.sh   (o el alias `jarvis-hud`)
PROJ="/Users/juangarces/dev/OpenJarvis"
CORE_PORT=8000
HUD_PORT=8090

# carga el entorno de Rust (necesario para el almacén de memoria / RAG)
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

echo "› Verificando Ollama…"
if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "  Ollama no responde — arrancándolo…"
  ollama serve >/tmp/ollama.log 2>&1 &
  for i in $(seq 1 20); do curl -s http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
fi
echo "  Ollama ✓"

echo "› Sincronizando notas nuevas/editadas de Obsidian…"
python3 "$PROJ/jarvis-hud/sync_vault.py" 2>&1 | sed 's/^/  /'

echo "› Verificando núcleo Jarvis (servidor REST)…"
if ! curl -s http://127.0.0.1:$CORE_PORT/health >/dev/null 2>&1; then
  echo "  Arrancando núcleo…"
  ( cd "$PROJ" && nohup uv run jarvis serve --host 127.0.0.1 --port $CORE_PORT >/tmp/jarvis-serve.log 2>&1 & )
  for i in $(seq 1 40); do curl -s http://127.0.0.1:$CORE_PORT/health >/dev/null 2>&1 && break; sleep 1; done
fi
echo "  Núcleo ✓"

echo "› Arrancando HUD (puerto $HUD_PORT, sin service worker)…"
# mata cualquier instancia previa del HUD
pkill -f "serve_hud.py" 2>/dev/null
# usa el python del venv (trae Piper para la voz neuronal /tts); si no existe, cae a python3
HUD_PY="$PROJ/.venv/bin/python"; [ -x "$HUD_PY" ] || HUD_PY="python3"
( cd "$PROJ" && nohup "$HUD_PY" jarvis-hud/serve_hud.py >/tmp/jarvis-hud.log 2>&1 & )
for i in $(seq 1 20); do curl -s http://127.0.0.1:$HUD_PORT/ >/dev/null 2>&1 && break; sleep 0.5; done
echo "  HUD ✓"

echo "› Abriendo interfaz…"
open "http://127.0.0.1:$HUD_PORT/"
echo ""
echo "  J.A.R.V.I.S en línea → http://127.0.0.1:$HUD_PORT/"
echo "  (logs: /tmp/jarvis-serve.log · /tmp/jarvis-hud.log · /tmp/ollama.log)"
