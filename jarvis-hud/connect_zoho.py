#!/usr/bin/env python3
"""Conecta Zoho Mail (IMAP) a la memoria de Jarvis.

Reusa el conector IMAP de OpenJarvis apuntado a imap.zoho.com, descarga los
correos recientes y los indexa en el MISMO almacén de memoria que tu Obsidian
(~/.openjarvis/memory.db), para que el chat del HUD los pueda buscar.

Credenciales (en tu entorno, NUNCA en el código):
  ZOHO_EMAIL          tu correo Zoho (ej. juan@nexiasoluciones.com.mx)
  ZOHO_APP_PASSWORD   contraseña de aplicación de Zoho (no tu pass normal)
  ZOHO_IMAP_HOST      opcional (default imap.zoho.com; prueba imappro.zoho.com)

Uso:
  python3 connect_zoho.py --test          # solo prueba el login IMAP
  python3 connect_zoho.py --days 60       # indexa correos de los últimos 60 días
  python3 connect_zoho.py --all           # indexa todo (hasta 5000)
"""
import argparse, os, sys, time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DB = Path.home() / ".openjarvis" / "memory.db"
MARKER = Path.home() / ".openjarvis" / ".zoho_sync"


def get_creds():
    em = os.environ.get("ZOHO_EMAIL", "").strip()
    pw = os.environ.get("ZOHO_APP_PASSWORD", "").strip()
    host = os.environ.get("ZOHO_IMAP_HOST", "imap.zoho.com").strip()
    if not em or not pw:
        print("✗ Falta ZOHO_EMAIL o ZOHO_APP_PASSWORD en el entorno.", file=sys.stderr)
        print("  Configúralas (en TU terminal, no aquí):", file=sys.stderr)
        print('    export ZOHO_EMAIL="tucorreo@dominio.com"', file=sys.stderr)
        print('    export ZOHO_APP_PASSWORD="xxxx-xxxx-xxxx"', file=sys.stderr)
        sys.exit(2)
    return em, pw, host


def build_connector(em, pw, host):
    from openjarvis.connectors.gmail_imap import GmailIMAPConnector
    return GmailIMAPConnector(email_address=em, app_password=pw, imap_host=host)


def cmd_test(em, pw, host):
    import imaplib
    print(f"› Probando login IMAP en {host} como {em} …")
    try:
        imap = imaplib.IMAP4_SSL(host)
        imap.login(em, pw)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "ALL")
        n = len(data[0].split())
        imap.logout()
        print(f"✓ Conexión OK — {n} correos en INBOX.")
    except Exception as e:
        print(f"✗ Falló: {e}", file=sys.stderr)
        print("  Revisa: (1) IMAP habilitado en Zoho, (2) contraseña de aplicación,",
              file=sys.stderr)
        print("  (3) host correcto (imap.zoho.com o imappro.zoho.com).", file=sys.stderr)
        sys.exit(1)


def cmd_index(em, pw, host, since):
    import openjarvis.tools.storage  # registra backends
    from openjarvis.core.registry import MemoryRegistry

    conn = build_connector(em, pw, host)
    mem = MemoryRegistry.create("sqlite", db_path=str(DB))

    when = "todos" if since is None else f"desde {since.date()}"
    print(f"› Descargando e indexando correos ({when}) …")
    n = 0
    for doc in conn.sync(since=since):
        ts = doc.timestamp.isoformat() if getattr(doc, "timestamp", None) else ""
        header = (f"Correo Zoho\nDe: {doc.author}\nAsunto: {doc.title}\n"
                  f"Fecha: {ts}\n\n")
        body = (doc.content or "").strip()
        if not body and not doc.title:
            continue
        source = f"zoho:{doc.title or '(sin asunto)'} — {doc.author}"[:200]
        # purga previa de este correo (evita duplicados al re-sincronizar)
        try:
            import sqlite3
            c = sqlite3.connect(str(DB)); c.execute("PRAGMA busy_timeout=8000")
            c.execute("DELETE FROM documents_fts WHERE rowid IN "
                      "(SELECT rowid FROM documents WHERE source=?)", (source,))
            c.execute("DELETE FROM documents WHERE source=?", (source,))
            c.commit(); c.close()
        except Exception:
            pass
        mem.store(header + body[:8000], source=source,
                  metadata={"type": "email", "from": doc.author, "subject": doc.title})
        n += 1
        if n % 50 == 0:
            print(f"  … {n} correos")
    if hasattr(mem, "close"):
        mem.close()
    MARKER.write_text(str(time.time()))
    print(f"✓ {n} correos indexados en la memoria de Jarvis.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="solo probar login")
    ap.add_argument("--all", action="store_true", help="indexar todo el historial")
    ap.add_argument("--days", type=int, default=60, help="días hacia atrás (default 60)")
    args = ap.parse_args()

    em, pw, host = get_creds()
    if args.test:
        cmd_test(em, pw, host); return
    since = None if args.all else datetime.now() - timedelta(days=args.days)
    cmd_index(em, pw, host, since)


if __name__ == "__main__":
    main()
