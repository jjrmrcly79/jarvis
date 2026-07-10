#!/bin/bash
# Reconstrucción LIMPIA del índice de Obsidian (borra y reindexa todo).
# Úsalo solo si la memoria quedó inconsistente o duplicada.
# Para el día a día usa `jarvis-sync` (incremental) — esto es el botón rojo.
PROJ="/Users/juangarces/dev/OpenJarvis"
VAULT="/Users/juangarces/Library/Mobile Documents/iCloud~md~obsidian/Documents"
DB="$HOME/.openjarvis/memory.db"
MARKER="$HOME/.openjarvis/.vault_sync"
[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"

echo "› Deteniendo núcleo (libera la base)…"
pkill -f "jarvis serve" 2>/dev/null; sleep 2

echo "› Borrando índice anterior…"
rm -f "$DB" "$DB-wal" "$DB-shm" "$MARKER"

echo "› Reindexando vault completo (puede tardar ~1-2 min)…"
( cd "$PROJ" && uv run jarvis memory index "$VAULT" )

# marca como sincronizado a partir de ahora
date +%s > "$MARKER"
echo "✓ Índice reconstruido. Abre con: jarvis-hud"
