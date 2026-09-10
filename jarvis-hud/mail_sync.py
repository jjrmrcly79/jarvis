#!/usr/bin/env python3
"""Indexa el correo local de Mail.app (Mac) en la memoria de Jarvis.

Lee los .emlx de ~/Library/Mail (todas las cuentas configuradas en Mail.app) y
los indexa en ~/.openjarvis/memory.db, igual que tu Obsidian. Así el chat/bot
puede buscar tu correo SIN IMAP ni nube.

Requiere "Acceso a Disco Completo" para Terminal.app
(Ajustes → Privacidad y Seguridad → Acceso a disco completo → Terminal).

Uso:
  python3 mail_sync.py --test         # solo cuenta correos accesibles
  python3 mail_sync.py --days 180     # indexa correos de los últimos 180 días
  python3 mail_sync.py --all --max 5000
"""
import argparse, email as email_lib, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openjarvis.connectors.gmail_imap import (_decode_subject, _extract_text_body,
                                              _parse_date)

MAIL_DIR = Path.home() / "Library" / "Mail"
DB = Path.home() / ".openjarvis" / "memory.db"
SKIP = ("Trash", "Deleted Messages", "Junk", "Spam", "Papelera")


def emlx_files():
    if not MAIL_DIR.exists():
        return []
    out = []
    for p in MAIL_DIR.rglob("*.emlx"):
        if any(s in part for part in p.parts for s in SKIP):
            continue
        out.append(p)
    return out


def parse_emlx(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    nl = data.find(b"\n")
    if nl == -1:
        return None
    try:
        length = int(data[:nl].strip())
        msg_bytes = data[nl + 1: nl + 1 + length]
    except ValueError:
        msg_bytes = data[nl + 1:]
    try:
        return email_lib.message_from_bytes(msg_bytes)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--max", type=int, default=4000)
    args = ap.parse_args()

    try:
        files = emlx_files()
    except PermissionError:
        print("✗ Sin permiso para leer ~/Library/Mail.", file=sys.stderr)
        print("  Da 'Acceso a Disco Completo' a Terminal y reábrelo.", file=sys.stderr)
        sys.exit(2)

    if not MAIL_DIR.exists() or (not files and not args.test):
        print("✗ No encuentro correos en ~/Library/Mail (¿permiso de disco? "
              "¿Mail.app con cuentas?).", file=sys.stderr)
        # diagnóstico de permiso
        try:
            list(MAIL_DIR.iterdir())
        except PermissionError:
            print("  → Es falta de 'Acceso a Disco Completo' para Terminal.", file=sys.stderr)
            sys.exit(2)
        sys.exit(1)

    print(f"· {len(files)} correos .emlx accesibles en Mail.app")
    if args.test:
        return

    cutoff = None if args.all else datetime.now(timezone.utc) - timedelta(days=args.days)
    import openjarvis.tools.storage  # registra backends
    from openjarvis.core.registry import MemoryRegistry
    import sqlite3
    mem = MemoryRegistry.create("sqlite", db_path=str(DB))

    n = 0
    for p in files:
        if n >= args.max:
            break
        msg = parse_emlx(p)
        if msg is None:
            continue
        ts = _parse_date(msg)
        if cutoff and ts and ts.tzinfo and ts < cutoff:
            continue
        subject = _decode_subject(msg.get("Subject", "")) or "(sin asunto)"
        sender = msg.get("From", "")
        body = (_extract_text_body(msg) or "").strip()
        if not body and subject == "(sin asunto)":
            continue
        source = f"mail:{subject} — {sender}"[:200]
        header = f"Correo (Mail.app)\nDe: {sender}\nAsunto: {subject}\nFecha: {ts}\n\n"
        try:
            c = sqlite3.connect(str(DB)); c.execute("PRAGMA busy_timeout=8000")
            c.execute("DELETE FROM documents_fts WHERE rowid IN "
                      "(SELECT rowid FROM documents WHERE source=?)", (source,))
            c.execute("DELETE FROM documents WHERE source=?", (source,))
            c.commit(); c.close()
        except Exception:
            pass
        mem.store(header + body[:8000], source=source,
                  metadata={"type": "email", "from": sender, "subject": subject})
        n += 1
        if n % 100 == 0:
            print(f"  … {n} indexados")
    if hasattr(mem, "close"):
        mem.close()
    print(f"✓ {n} correos indexados en la memoria de Jarvis.")


if __name__ == "__main__":
    main()
