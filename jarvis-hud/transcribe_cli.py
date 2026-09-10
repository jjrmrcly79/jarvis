#!/usr/bin/env python3
"""CLI de transcripción — corre mlx-whisper en su PROPIO proceso (hilo principal).

El bot lo invoca como subproceso para evitar que mlx/Metal se cuelgue al correr
dentro de un hilo de `asyncio.to_thread`. Imprime el resultado en stdout tras un
marcador para no mezclarlo con logs/progreso de mlx.

Uso:  python transcribe_cli.py <ruta_audio>
Salida (stdout):  @@RESULT@@{"text": "..."}
"""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_notes as vn

MARK = "@@RESULT@@"

def main():
    if len(sys.argv) < 2:
        print(MARK + json.dumps({"error": "falta la ruta del audio"}))
        sys.exit(2)
    try:
        text = vn.transcribe(sys.argv[1])
        sys.stdout.write(MARK + json.dumps({"text": text}))
    except Exception as e:
        sys.stdout.write(MARK + json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
