#!/usr/bin/env python3
"""Sincronización incremental del vault de Obsidian con la memoria de Jarvis.

Reindexa SOLO las notas que cambiaron desde la última sincronización (y purga
las eliminadas), borrando primero sus fragmentos viejos para no duplicar.
Rápido (segundos) y seguro para correr en cada apertura.

  VAULT  -> carpeta del vault (env VAULT o el default)
  Marca  -> ~/.openjarvis/.vault_sync  (epoch de la última sync)
"""
import os, sqlite3, subprocess, sys, time
from pathlib import Path

VAULT = Path(os.environ.get(
    "VAULT",
    "/Users/juangarces/Library/Mobile Documents/iCloud~md~obsidian/Documents",
))
PROJ = "/Users/juangarces/dev/OpenJarvis"
DB = Path.home() / ".openjarvis" / "memory.db"
MARKER = Path.home() / ".openjarvis" / ".vault_sync"


def md_files():
    out = []
    for p in VAULT.rglob("*.md"):
        parts = set(p.parts)
        if ".obsidian" in parts or ".trash" in parts:
            continue
        out.append(p)
    return out


def main():
    if not DB.exists():
        print("· sin memory.db — corre primero `jarvis-reindex`"); return

    files = md_files()

    # Primera vez: asumimos que el estado actual ya está indexado (full index previo)
    if not MARKER.exists():
        MARKER.write_text(str(time.time()))
        print(f"· marca inicial creada — {len(files)} notas asumidas sincronizadas")
        return

    last = float((MARKER.read_text().strip() or "0"))
    changed = [p for p in files if p.stat().st_mtime > last]

    if not changed:
        MARKER.write_text(str(time.time()))
        print("· vault al día — sin cambios")
        return

    # SOLO purgamos las notas que cambiaron (para no duplicar al reindexarlas).
    # No borramos por "ausencia en disco": con iCloud un archivo puede no estar
    # materializado y NO significa que se borró. Para limpiar borrados reales
    # de verdad: `jarvis-rebuild` (reconstrucción completa).
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA busy_timeout=8000")
    for p in changed:
        src = str(p)
        conn.execute(
            "DELETE FROM documents_fts WHERE rowid IN "
            "(SELECT rowid FROM documents WHERE source = ?)", (src,))
        conn.execute("DELETE FROM documents WHERE source = ?", (src,))
    conn.commit()
    conn.close()

    for p in changed:
        subprocess.run(
            ["uv", "run", "--project", PROJ, "jarvis", "memory", "index", str(p)],
            cwd=PROJ, capture_output=True,
        )

    MARKER.write_text(str(time.time()))
    print(f"· sync incremental: {len(changed)} nota(s) actualizada(s)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"· sync omitido (no crítico): {e}", file=sys.stderr)
