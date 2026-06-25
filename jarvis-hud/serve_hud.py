#!/usr/bin/env python3
"""Servidor del HUD de J.A.R.V.I.S.

Sirve la interfaz HUD en un puerto propio (sin service worker que interfiera)
y hace de proxy de la API local de OpenJarvis (mismo origen -> sin CORS).

  HUD  ->  http://127.0.0.1:8090/
  /v1/*, /health  ->  reenviado a  http://127.0.0.1:8000
"""
import os, re, sys, json, urllib.request, urllib.error, urllib.parse
from datetime import date
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HUD_PORT = int(os.environ.get("HUD_PORT", "8090"))
CORE = os.environ.get("JARVIS_CORE", "http://127.0.0.1:8000")
HUD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
PROXY_PREFIXES = ("/v1", "/health", "/dashboard", "/agents", "/models")

# ---------- TTS (Piper · voz neuronal local, $0, offline) ----------
import io, wave, threading
TTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts")
TTS_MODEL = os.environ.get(
    "JARVIS_TTS_MODEL", os.path.join(TTS_DIR, "es_AR-daniela-high.onnx"))
# length_scale > 1 = más pausada/elegante; ajustable sin tocar código
TTS_LENGTH_SCALE = float(os.environ.get("JARVIS_TTS_SPEED", "1.06"))
_tts_voice = None
_tts_lock = threading.Lock()  # la sesión onnxruntime no es segura en concurrencia


def _get_tts_voice():
    global _tts_voice
    if _tts_voice is None:
        from piper import PiperVoice
        _tts_voice = PiperVoice.load(TTS_MODEL)
    return _tts_voice


# "J.A.R.V.I.S" se deletrea por los puntos -> decirlo como palabra "Jarvis"
_JARVIS_RE = re.compile(r"\bJ\.?A\.?R\.?V\.?I\.?S\b", re.I)


def _tts_normalize(text):
    """Normaliza el texto solo para la voz (no afecta lo que se muestra en pantalla)."""
    return _JARVIS_RE.sub("Jarvis", text or "")


def synth_wav_bytes(text):
    """Sintetiza `text` a WAV (bytes) con la voz Piper. Carga el modelo una vez."""
    from piper.config import SynthesisConfig
    voice = _get_tts_voice()
    cfg = SynthesisConfig(length_scale=TTS_LENGTH_SCALE)
    buf = io.BytesIO()
    with _tts_lock:
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(_tts_normalize(text), wf, cfg)
    return buf.getvalue()

VAULT = Path(os.environ.get(
    "VAULT",
    "/Users/juangarces/Library/Mobile Documents/iCloud~md~obsidian/Documents",
))

# ---------- Escáner determinista de pendientes (checkboxes de Obsidian) ----------
OPEN_RE = re.compile(r"^\s*[-*]\s+\[ \]\s*(.+?)\s*$")
DONE_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s")
DATE_RE = re.compile(r"(?:📅|due:?|vence:?|fecha:?)\s*(\d{4}-\d{2}-\d{2})", re.I)
ANY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")
CLEAN_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")  # [[nota|alias]] -> nota


def _project_of(p: Path) -> str:
    try:
        rel = p.relative_to(VAULT)
    except ValueError:
        return "(raíz)"
    return rel.parts[0] if len(rel.parts) > 1 else "(raíz)"


def _clean(text: str) -> str:
    text = CLEAN_LINK.sub(r"\1", text)
    text = re.sub(r"#\S+", "", text)                 # quita tags
    text = re.sub(r"(?:📅|due:?|vence:?)\s*\d{4}-\d{2}-\d{2}", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _md_files():
    for p in VAULT.rglob("*.md"):
        parts = set(p.parts)
        if ".obsidian" in parts or ".trash" in parts:
            continue
        yield p


def scan_tasks():
    today = date.today().isoformat()
    projects, dated, recent = {}, [], []
    total_open = 0
    for p in _md_files():
        proj = _project_of(p)
        pr = projects.setdefault(proj, {"open": 0, "done": 0})
        try:
            mtime = p.stat().st_mtime
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for idx, ln in enumerate(lines, start=1):
            mo = OPEN_RE.match(ln)
            if mo:
                pr["open"] += 1
                total_open += 1
                d = DATE_RE.search(ln) or ANY_DATE.search(ln)
                due = d.group(1) if d else None
                item = {"text": _clean(mo.group(1)), "project": proj,
                        "file": p.stem, "due": due, "mtime": mtime,
                        "path": str(p), "line": idx}
                if due:
                    dated.append(item)
                recent.append(item)
            elif DONE_RE.match(ln):
                pr["done"] += 1
    dated.sort(key=lambda x: x["due"])
    recent.sort(key=lambda x: x["mtime"], reverse=True)
    proj_list = sorted(
        [{"name": k, **v} for k, v in projects.items() if v["open"] or v["done"]],
        key=lambda x: x["open"], reverse=True,
    )
    overdue = [t for t in dated if t["due"] < today]
    due_today = [t for t in dated if t["due"] == today]
    return {
        "today": today,
        "total_open": total_open,
        "projects": proj_list,
        "overdue": overdue[:20],
        "due_today": due_today[:20],
        "upcoming": [t for t in dated if t["due"] > today][:20],
        "recent": recent[:15],
    }


def all_open_tasks(limit: int = 4000):
    """Todas las tareas abiertas del vault con path+line, para agrupar en el HUD."""
    out = []
    for p in _md_files():
        proj = _project_of(p)
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for idx, ln in enumerate(lines, start=1):
            mo = OPEN_RE.match(ln)
            if mo:
                d = DATE_RE.search(ln) or ANY_DATE.search(ln)
                out.append({"text": _clean(mo.group(1)), "project": proj,
                            "file": p.stem, "due": d.group(1) if d else None,
                            "path": str(p), "line": idx})
                if len(out) >= limit:
                    return {"tasks": out, "truncated": True}
    return {"tasks": out, "truncated": False}


# ---------- Reporte de avance por proyecto (se escribe al cerrar pendientes) ----------
NEXIA_DIR = VAULT / "Nexia"
PROGRESS_MD = NEXIA_DIR / "Corporativo" / "Avance de Proyectos (auto).md"
CLOSE_LOG = Path.home() / ".openjarvis" / "closed_tasks.jsonl"


def _client_or_top(p: Path) -> str:
    """Etiqueta de proyecto: cliente bajo Clientes/<X>, si no la carpeta top-level."""
    try:
        parts = list(p.relative_to(VAULT).parts)
    except ValueError:
        return "(otros)"
    if "Clientes" in parts:
        i = parts.index("Clientes")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[0] if len(parts) > 1 else "(raíz)"


def project_status():
    """{proyecto: {'open':int,'done':int}} escaneando todo el vault una vez."""
    stats = {}
    for p in _md_files():
        name = _client_or_top(p)
        s = stats.setdefault(name, {"open": 0, "done": 0})
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for ln in fh:
                    if OPEN_RE.match(ln):
                        s["open"] += 1
                    elif DONE_RE.match(ln):
                        s["done"] += 1
        except OSError:
            continue
    return stats


def _log_closures(items):
    """Anexa los cierres a un log jsonl para la sección 'últimos cerrados'."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        CLOSE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(CLOSE_LOG, "a", encoding="utf-8") as fh:
            for it in items:
                proj = _client_or_top(Path(it.get("path", "")))
                fh.write(json.dumps({"ts": ts, "project": proj,
                                     "text": it.get("text", "")}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _recent_closures(limit=25):
    if not CLOSE_LOG.is_file():
        return []
    try:
        with open(CLOSE_LOG, "r", encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:][::-1]


def write_progress_report():
    """Reescribe el MD de avance por proyecto en la bóveda de Nexia. Best-effort."""
    from datetime import datetime
    try:
        stats = project_status()
        rows = sorted(stats.items(), key=lambda kv: kv[1]["open"], reverse=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        out = [
            "# Avance de Proyectos — JARVIS",
            "",
            f"> Generado automáticamente por OpenJarvis al cerrar pendientes. "
            f"Última actualización: **{now}**. No editar a mano (se sobrescribe).",
            "",
            "## Resumen por proyecto",
            "",
            "| Proyecto | Abiertas | Hechas | % avance |",
            "|---|---:|---:|---:|",
        ]
        tot_o = tot_d = 0
        for name, s in rows:
            o, d = s["open"], s["done"]
            tot_o += o
            tot_d += d
            pct = round(100 * d / (o + d)) if (o + d) else 0
            out.append(f"| {name} | {o} | {d} | {pct}% |")
        pct_t = round(100 * tot_d / (tot_o + tot_d)) if (tot_o + tot_d) else 0
        out.append(f"| **TOTAL** | **{tot_o}** | **{tot_d}** | **{pct_t}%** |")
        out += ["", "## Últimos pendientes cerrados", ""]
        rc = _recent_closures()
        if rc:
            for r in rc:
                out.append(f"- {r.get('ts','')} · **[{r.get('project','?')}]** {r.get('text','')}")
        else:
            out.append("_Aún no hay cierres registrados._")
        out.append("")
        target = PROGRESS_MD if NEXIA_DIR.is_dir() else (VAULT / "Avance de Proyectos (auto).md")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        return str(target)
    except Exception:
        return None


def close_task(path: str, line: int, text: str = ""):
    """Marca una tarea como hecha ([ ] -> [x]) en path:line del vault.

    Seguro: solo escribe dentro del VAULT y valida que la línea siga siendo
    ese pendiente abierto (mismo texto) antes de tocar nada. Devuelve
    {"ok": bool, "error"?: str}. Si la nota cambió, no escribe y avisa.
    """
    try:
        p = Path(path)
        p.resolve().relative_to(VAULT.resolve())  # impide escribir fuera del vault
    except Exception:
        return {"ok": False, "error": "ruta fuera del vault"}
    if not p.is_file():
        return {"ok": False, "error": "archivo no encontrado"}
    try:
        line = int(line)
    except (TypeError, ValueError):
        return {"ok": False, "error": "línea inválida"}
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    i = line - 1
    if i < 0 or i >= len(lines):
        return {"ok": False, "error": "la nota cambió (línea fuera de rango)"}
    mo = OPEN_RE.match(lines[i])
    if not mo:
        return {"ok": False, "error": "esa línea ya no es un pendiente abierto"}
    closed_text = _clean(mo.group(1))
    if text and closed_text.strip() != text.strip():
        return {"ok": False, "error": "la nota cambió (el texto no coincide)"}
    lines[i] = lines[i].replace("[ ]", "[x]", 1)  # solo el primer checkbox de la línea
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _log_closures([{"path": str(p), "text": closed_text}])
    write_progress_report()
    return {"ok": True}


def close_tasks_bulk(items):
    """Cierra varias tareas de golpe. Agrupa por archivo: abre/escribe cada nota
    UNA sola vez. Valida cada línea (mismo texto, abierta) antes de palomearla.
    items: [{path, line, text}]. Devuelve {ok, closed, failed:[{path,line,error}]}.
    """
    from collections import defaultdict
    by_file = defaultdict(list)
    for it in (items or []):
        by_file[it.get("path", "")].append(it)
    closed = 0
    failed = []
    done_all = []
    for path, its in by_file.items():
        try:
            p = Path(path)
            p.resolve().relative_to(VAULT.resolve())
        except Exception:
            failed += [{"path": path, "line": it.get("line"),
                        "error": "fuera del vault"} for it in its]
            continue
        if not p.is_file():
            failed += [{"path": path, "line": it.get("line"),
                        "error": "archivo no encontrado"} for it in its]
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError as e:
            failed += [{"path": path, "line": it.get("line"),
                        "error": str(e)} for it in its]
            continue
        dirty = False
        done_here = []
        for it in its:
            try:
                i = int(it.get("line", 0)) - 1
            except (TypeError, ValueError):
                failed.append({"path": path, "line": it.get("line"),
                               "error": "línea inválida"})
                continue
            if i < 0 or i >= len(lines):
                failed.append({"path": path, "line": it.get("line"),
                               "error": "fuera de rango"})
                continue
            mo = OPEN_RE.match(lines[i])
            if not mo:
                failed.append({"path": path, "line": it.get("line"),
                               "error": "ya no es pendiente abierto"})
                continue
            cur = _clean(mo.group(1))
            txt = it.get("text", "")
            if txt and cur.strip() != txt.strip():
                failed.append({"path": path, "line": it.get("line"),
                               "error": "el texto no coincide"})
                continue
            lines[i] = lines[i].replace("[ ]", "[x]", 1)
            dirty = True
            closed += 1
            done_here.append({"path": path, "text": cur})
        if dirty:
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.writelines(lines)
                done_all.extend(done_here)
            except OSError as e:
                failed.append({"path": path, "line": None, "error": "no se pudo guardar: " + str(e)})
    if done_all:
        _log_closures(done_all)
        write_progress_report()
    return {"ok": True, "closed": closed, "failed": failed}


def _entity_dirs():
    """Carpetas candidatas a proyecto/cliente (nombre legible, no 00_MOC/05_x)."""
    ents = {}
    for p in VAULT.rglob("*"):
        if not p.is_dir():
            continue
        parts = set(p.parts)
        if ".obsidian" in parts or ".trash" in parts:
            continue
        name = p.name
        if len(name) >= 4 and not name[0].isdigit():
            ents.setdefault(name.lower(), p)
    return ents


def entity_context(query: str, max_notes: int = 3, per_note_chars: int = 2500):
    """Para preguntas sobre un cliente/proyecto: devuelve su MOC + notas más
    RECIENTES con fecha, para que el modelo use el estado actual (no fragmentos
    viejos del RAG). Reproduce el método de MOC del usuario."""
    import re
    from datetime import datetime
    q = query.lower()
    ents = _entity_dirs()
    hit = None
    for name, path in sorted(ents.items(), key=lambda x: -len(x[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            hit = (name, path)
            break
    if not hit:
        return None
    name, folder = hit
    notes = [p for p in folder.rglob("*.md")
             if ".obsidian" not in p.parts and ".trash" not in p.parts]
    if not notes:
        return None
    moc = [p for p in notes if re.search(r"moc|indice|índice", p.name, re.I)]
    rest = sorted([p for p in notes if p not in moc],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    pick = (moc[:1] + rest)[:max_notes]
    blocks = []
    for p in pick:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")[:per_note_chars]
        except OSError:
            continue
        d = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
        blocks.append(f"### {p.name} (modificada {d})\n{txt}")
    if not blocks:
        return None
    return (f"DATOS REALES de las notas Obsidian sobre '{name}'. La nota MÁS RECIENTE "
            f"(fecha mayor) refleja el estado ACTUAL — prioriza esa sobre cualquier otra "
            f"información más antigua:\n\n" + "\n\n".join(blocks))


# ---------- Calendario de Mac (vía Calendar.app / osascript) ----------
_CAL_SCRIPT = '''on run argv
  set numDays to (item 1 of argv) as integer
  set out to ""
  set d0 to (current date) - (time of (current date))
  set d1 to d0 + (numDays * days)
  tell application "Calendar"
    repeat with cal in calendars
      try
        set evs to (every event of cal whose start date ≥ d0 and start date ≤ d1)
        repeat with e in evs
          set dd to (round (((start date of e) - d0) / days) rounding down)
          set out to out & dd & "\\t" & ((start date of e) as string) & "\\t" & (name of cal) & "\\t" & (summary of e) & linefeed
        end repeat
      end try
    end repeat
  end tell
  return out
end run'''


def calendar_context(days: int = 10, limit: int = 25):
    """Lee eventos próximos del Calendario de Mac (todos los calendarios).
    Devuelve un bloque ordenado por fecha para inyectar al chat. None si no hay
    eventos o si falta permiso de Automatización (Calendar)."""
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e", _CAL_SCRIPT, str(days)],
                           capture_output=True, text=True, timeout=40)
    except Exception:
        return None
    rows = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 4:
            try:
                dd = int(parts[0])
            except ValueError:
                dd = 999
            rows.append((dd, parts[1], parts[2], parts[3]))
    if not rows:
        return None
    rows.sort(key=lambda x: x[0])
    lines = []
    for dd, when, cal, summ in rows[:limit]:
        tag = "HOY" if dd == 0 else ("MAÑANA" if dd == 1 else f"en {dd} días")
        lines.append(f"- [{tag}] {summ} — {when} ({cal})")
    return (f"AGENDA REAL del Calendario de Mac (próximos {days} días, ordenada por fecha):\n"
            + "\n".join(lines))


# ---------- Crear evento en el Calendario de Mac (escritura, osascript) ----------
_CAL_CREATE_SCRIPT = '''on run argv
  set calName to item 1 of argv
  set evSummary to item 2 of argv
  set yr to (item 3 of argv) as integer
  set mo to (item 4 of argv) as integer
  set dy to (item 5 of argv) as integer
  set hr to (item 6 of argv) as integer
  set mi to (item 7 of argv) as integer
  set durMin to (item 8 of argv) as integer
  set evLoc to item 9 of argv
  set evNotes to item 10 of argv
  set startDate to current date
  set day of startDate to 1
  set year of startDate to yr
  set month of startDate to mo
  set day of startDate to dy
  set hours of startDate to hr
  set minutes of startDate to mi
  set seconds of startDate to 0
  set endDate to startDate + (durMin * minutes)
  tell application "Calendar"
    tell calendar calName
      set newEv to make new event with properties {summary:evSummary, start date:startDate, end date:endDate}
      if evLoc is not "" then set location of newEv to evLoc
      if evNotes is not "" then set description of newEv to evNotes
    end tell
    return ((start date of newEv) as string)
  end tell
end run'''


def create_calendar_event(summary, start_dt, end_dt=None, calendar="Calendario",
                          location="", notes="", duration_min=60):
    """Crea un evento en el Calendario de Mac (Calendar.app) vía osascript.

    Determinista: construye la fecha por componentes (zona local de la Mac),
    sin interpretar strings dependientes del idioma. ``start_dt`` y ``end_dt``
    son ``datetime`` locales. Devuelve un dict con el resultado real leído de
    vuelta del evento recién creado:
      {ok: True, when: <str>, calendar, summary, start, end}   (éxito)
      {ok: False, error: <str>}                                (fallo)
    """
    import subprocess
    from datetime import timedelta
    if end_dt is None:
        end_dt = start_dt + timedelta(minutes=duration_min)
    dur = max(1, int(round((end_dt - start_dt).total_seconds() / 60)))
    args = [str(calendar), str(summary),
            str(start_dt.year), str(start_dt.month), str(start_dt.day),
            str(start_dt.hour), str(start_dt.minute), str(dur),
            location or "", notes or ""]
    try:
        r = subprocess.run(["osascript", "-e", _CAL_CREATE_SCRIPT, *args],
                           capture_output=True, text=True, timeout=40)
    except Exception as e:
        return {"ok": False, "error": f"osascript falló: {e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip() or "error desconocido de Calendar.app"
        return {"ok": False, "error": err}
    return {"ok": True, "when": (r.stdout or "").strip(), "calendar": calendar,
            "summary": summary, "start": start_dt, "end": end_dt}


# ---------- Correos de hoy (Mail.app, solo lectura, osascript) ----------
_MAIL_TODAY_SCRIPT = '''on run
  set d0 to (current date)
  set hours of d0 to 0
  set minutes of d0 to 0
  set seconds of d0 to 0
  set out to ""
  tell application "Mail"
    set msgs to (messages of inbox whose date received ≥ d0)
    repeat with m in msgs
      try
        set out to out & (date received of m as string) & "\\t" & (sender of m) & "\\t" & (subject of m) & "\\t" & (read status of m) & linefeed
      end try
    end repeat
  end tell
  return out
end run'''


def mail_context(limit: int = 40):
    """Lee los correos recibidos HOY en la bandeja de Mail.app (todas las cuentas).
    Devuelve un bloque real para inyectar al chat y que Jarvis lo resuma. None si
    no hay correos o si falta acceso a la base de Mail.

    Lee directo el Envelope Index (SQLite) de Mail — instantáneo incluso con
    decenas de miles de correos. (AppleScript sobre la bandeja unificada se
    cuelga con inbox grandes.) Requiere Full Disk Access para el proceso.
    """
    import sqlite3
    from datetime import datetime
    try:
        from openjarvis.tools.mail_read import _find_envelope_index
        db = _find_envelope_index()
    except Exception:
        db = None
    if db is None:
        return None
    sql = (
        "SELECT m.read, m.date_received, m.subject_prefix, s.subject, "
        "COALESCE(NULLIF(a.comment, ''), a.address) "
        "FROM messages m "
        "LEFT JOIN addresses a ON a.ROWID = m.sender "
        "LEFT JOIN subjects s ON s.ROWID = m.subject "
        "LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox "
        "WHERE m.deleted = 0 AND mb.url LIKE '%/INBOX' "
        "ORDER BY m.date_received DESC LIMIT ?"
    )
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5.0)
        try:
            rows = conn.execute(sql, (max(1, limit),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    lines = []
    unread_n = 0
    for read_flag, epoch, prefix, subject, sender in rows:
        try:
            cuando = datetime.fromtimestamp(int(epoch)).strftime("%d-%b %H:%M")
        except (TypeError, ValueError, OSError):
            cuando = "?"
        subj = ((prefix or "") + (subject or "")).strip() or "(sin asunto)"
        mark = ""
        if not read_flag:
            mark = "•SIN LEER "
            unread_n += 1
        lines.append(f"- {mark}{(sender or '(desconocido)').strip()} — {subj} ({cuando})")
    return (f"CORREOS REALES de la bandeja de entrada en Mail.app (los {len(rows)} "
            f"más recientes; {unread_n} sin leer). Responde la pregunta del usuario "
            f"usando SOLO estos datos reales — no inventes ni sugieras comandos de "
            f"terminal. Si pregunta por no leídos, lista los marcados '•SIN LEER':\n"
            + "\n".join(lines))


# ---------- Recordatorios de Mac (Reminders.app, lectura + escritura) ----------
_REM_READ_SCRIPT = '''on run
  set out to ""
  tell application "Reminders"
    repeat with l in lists
      set lname to name of l
      repeat with r in (reminders of l whose completed is false)
        set dd to ""
        try
          if (due date of r) is not missing value then set dd to ((due date of r) as string)
        end try
        set out to out & lname & "\\t" & (name of r) & "\\t" & dd & linefeed
      end repeat
    end repeat
  end tell
  return out
end run'''


def reminders_context(limit: int = 60):
    """Lee los recordatorios pendientes (no completados) de Reminders.app,
    agrupados por lista, para inyectar al chat. None si no hay o falta permiso."""
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e", _REM_READ_SCRIPT],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    rows = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            lname, name = parts[0], parts[1]
            due = parts[2] if len(parts) >= 3 else ""
            rows.append((lname, name, due))
    if not rows:
        return None
    groups = {}
    for lname, name, due in rows[:limit]:
        groups.setdefault(lname, []).append((name, due))
    lines = []
    for lname, items in groups.items():
        lines.append(f"[{lname}]")
        for name, due in items:
            d = ""
            if due:
                d = f" (vence {due.split(' at ')[0] if ' at ' in due else due})"
            lines.append(f"  - {name}{d}")
    return (f"RECORDATORIOS REALES pendientes de Reminders.app "
            f"({len(rows)} en total, agrupados por lista):\n" + "\n".join(lines))


# ---------- Versiones estructuradas (JSON) para el dashboard del HUD ----------
# Reutilizan las mismas consultas que las funciones *_context (que devuelven texto
# para el chat); estas devuelven listas de dicts para pintarlas como UI.

def agenda_rows(days: int = 7, limit: int = 12):
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e", _CAL_SCRIPT, str(days)],
                           capture_output=True, text=True, timeout=40)
    except Exception:
        return []
    out = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 4:
            try:
                dd = int(parts[0])
            except ValueError:
                dd = 999
            out.append({"days": dd, "when": parts[1], "cal": parts[2], "title": parts[3]})
    out.sort(key=lambda x: x["days"])
    return out[:limit]


def reminders_rows(limit: int = 20):
    import subprocess
    try:
        r = subprocess.run(["osascript", "-e", _REM_READ_SCRIPT],
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    out = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            due = parts[2] if len(parts) >= 3 else ""
            if due and " at " in due:
                due = due.split(" at ")[0]
            out.append({"list": parts[0], "name": parts[1], "due": due})
    return out[:limit]


# Fuentes de correo a mostrar. Se leen de un archivo LOCAL fuera del repo
# (~/.openjarvis/mail_accounts.json) para no exponer correos/cuentas en el código.
# Formato: {"sources":[{"label","url_like","owner"}]} — ver mail_accounts.example.json.
# iCloud guarda su recibido en INBOX; Gmail lo guarda en [Gmail]/Todos (All Mail),
# de donde se excluye lo que envió el propio dueño (owner).
MAIL_ACCOUNTS_FILE = Path.home() / ".openjarvis" / "mail_accounts.json"


def _mail_sources():
    try:
        data = json.loads(MAIL_ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return [(s["label"], s["url_like"], s.get("owner"))
                for s in data.get("sources", []) if s.get("label") and s.get("url_like")]
    except Exception:
        return []


# Remitentes ocultados a mano por el usuario (botón 🚫 del panel). Persistente.
HIDDEN_SENDERS_FILE = Path.home() / ".openjarvis" / "mail_hidden_senders.txt"


def load_hidden_senders():
    try:
        return {l.strip() for l in HIDDEN_SENDERS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    except Exception:
        return set()


def add_hidden_sender(sender):
    s = (sender or "").strip()
    if not s or s in load_hidden_senders():
        return
    HIDDEN_SENDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HIDDEN_SENDERS_FILE, "a", encoding="utf-8") as f:
        f.write(s + "\n")


def remove_hidden_sender(sender):
    s = (sender or "").strip()
    cur = load_hidden_senders()
    if s in cur:
        cur.discard(s)
        HIDDEN_SENDERS_FILE.write_text(("\n".join(sorted(cur)) + "\n") if cur else "", encoding="utf-8")


def mail_rows(limit: int = 8, include_promo: bool = False):
    """Correos recientes de las 3 cuentas (iCloud INBOX + 2 Gmail desde All Mail,
    excluyendo lo enviado). Por defecto OCULTA promociones/newsletters y los
    remitentes que el usuario bloqueó. Devuelve (items, ocultos_promo, bloqueados)."""
    import sqlite3
    from datetime import datetime
    try:
        from openjarvis.tools.mail_read import _find_envelope_index
        db = _find_envelope_index()
    except Exception:
        db = None
    if db is None:
        return [], 0, 0
    blocklist = load_hidden_senders()
    out = []
    hidden = 0   # promociones filtradas por metadatos
    blocked = 0  # remitentes ocultados a mano por el usuario
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5.0)
    except Exception:
        return [], 0, 0
    try:
        for label, url_like, owner in _mail_sources():
            sql = (
                "SELECT m.read, m.date_received, m.subject_prefix, s.subject, "
                "COALESCE(NULLIF(a.comment, ''), a.address), "
                "m.unsubscribe_type, m.brand_indicator, m.list_id_hash, a.address "
                "FROM messages m "
                "LEFT JOIN addresses a ON a.ROWID = m.sender "
                "LEFT JOIN subjects s ON s.ROWID = m.subject "
                "LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox "
                "WHERE m.deleted = 0 AND mb.url LIKE ? "
                + ("AND (a.address IS NULL OR a.address <> ?) " if owner else "")
                + "ORDER BY m.date_received DESC LIMIT ?"
            )
            params = [url_like] + ([owner] if owner else []) + [max(limit * 10, 100)]
            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception:
                rows = []
            kept = 0
            for read_flag, epoch, prefix, subject, sender, unsub, brand, lst, addr in rows:
                snd = (sender or "(desconocido)").strip()
                if snd in blocklist:
                    blocked += 1
                    continue
                # promo/newsletter: trae "darse de baja", o remitente de marca, o List-Id
                is_promo = (unsub not in (None, 0)) or (brand is not None) or (lst not in (None, 0))
                if is_promo and not include_promo:
                    hidden += 1
                    continue
                try:
                    cuando = datetime.fromtimestamp(int(epoch)).strftime("%d-%b %H:%M")
                except (TypeError, ValueError, OSError):
                    cuando = "?"
                subj = ((prefix or "") + (subject or "")).strip() or "(sin asunto)"
                out.append({"unread": not read_flag, "sender": snd,
                            "subject": subj, "when": cuando, "account": label})
                kept += 1
                if kept >= limit:
                    break
    finally:
        conn.close()
    return out, hidden, blocked


_REM_CREATE_SCRIPT = '''on run argv
  set listName to item 1 of argv
  set remName to item 2 of argv
  set remBody to item 3 of argv
  set hasDue to item 4 of argv
  tell application "Reminders"
    if (exists list listName) then
      set tgt to list listName
    else
      set tgt to default list
    end if
    if remBody is "" then
      set newR to make new reminder at end of tgt with properties {name:remName}
    else
      set newR to make new reminder at end of tgt with properties {name:remName, body:remBody}
    end if
    if hasDue is "1" then
      set dd to current date
      set day of dd to 1
      set year of dd to ((item 5 of argv) as integer)
      set month of dd to ((item 6 of argv) as integer)
      set day of dd to ((item 7 of argv) as integer)
      set hours of dd to ((item 8 of argv) as integer)
      set minutes of dd to ((item 9 of argv) as integer)
      set seconds of dd to 0
      set due date of newR to dd
    end if
    return (name of tgt)
  end tell
end run'''


_REM_LISTS_CACHE = []


def reminder_lists(refresh: bool = False):
    """Nombres de las listas de Reminders.app (cacheado en memoria)."""
    global _REM_LISTS_CACHE
    if _REM_LISTS_CACHE and not refresh:
        return _REM_LISTS_CACHE
    import subprocess
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Reminders" to get name of every list'],
            capture_output=True, text=True, timeout=20)
        names = [s.strip() for s in r.stdout.split(",") if s.strip()]
        if names:
            _REM_LISTS_CACHE = names
    except Exception:
        pass
    return _REM_LISTS_CACHE


def create_reminder(name, list_name="Actividades", due_dt=None, notes=""):
    """Crea un recordatorio en Reminders.app. Si la lista no existe, cae a la
    lista por defecto del sistema. ``due_dt`` (datetime) es opcional. Devuelve
    {ok, list, name, due} o {ok: False, error}."""
    import subprocess
    has_due = "1" if due_dt else "0"
    comps = ([str(due_dt.year), str(due_dt.month), str(due_dt.day),
              str(due_dt.hour), str(due_dt.minute)] if due_dt
             else ["0", "0", "0", "0", "0"])
    args = [str(list_name or "Actividades"), str(name), notes or "", has_due, *comps]
    try:
        r = subprocess.run(["osascript", "-e", _REM_CREATE_SCRIPT, *args],
                           capture_output=True, text=True, timeout=40)
    except Exception as e:
        return {"ok": False, "error": f"osascript falló: {e}"}
    if r.returncode != 0:
        err = (r.stderr or "").strip() or "error desconocido de Reminders.app"
        return {"ok": False, "error": err}
    return {"ok": True, "list": (r.stdout or "").strip() or list_name,
            "name": name, "due": due_dt}


def project_tasks(name: str, limit: int = 50):
    name_l = name.lower()
    out = []
    for p in _md_files():
        if _project_of(p).lower() != name_l:
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for idx, ln in enumerate(lines, start=1):
            mo = OPEN_RE.match(ln)
            if mo:
                d = DATE_RE.search(ln) or ANY_DATE.search(ln)
                out.append({"text": _clean(mo.group(1)), "file": p.stem,
                            "due": d.group(1) if d else None,
                            "path": str(p), "line": idx})
    out.sort(key=lambda x: (x["due"] is None, x["due"] or ""))
    return {"project": name, "open": len(out), "tasks": out[:limit]}


# ======================= Notas de voz — backlog para el dashboard =============
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice_notes as _vn

VOICE_REC = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
VOICE_INV = Path.home() / ".openjarvis/voice_inventory.json"
VOICE_RESULTS = Path.home() / ".openjarvis/voice_backlog_results.jsonl"
VOICE_ARCHIVED = Path.home() / ".openjarvis/voice_archived.json"
VOICE_PID = Path.home() / ".openjarvis/backlog.pid"
VOICE_MAX = 1800


def _voice_archived():
    try:
        return set(json.loads(VOICE_ARCHIVED.read_text()))
    except Exception:
        return set()


def _voice_running():
    try:
        pid = int(VOICE_PID.read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _voice_load():
    inv = json.loads(VOICE_INV.read_text()) if VOICE_INV.exists() else {"items": [], "count": 0}
    size = {it["file"]: it.get("size", 0) for it in inv["items"]}
    dur = {it["file"]: (it.get("duration_s") or 0) for it in inv["items"]}
    date_ = {it["file"]: it.get("date") for it in inv["items"]}
    results = {}
    if VOICE_RESULTS.exists():
        for line in VOICE_RESULTS.read_text().splitlines():
            try:
                r = json.loads(line); results[r["file"]] = r
            except Exception:
                pass
    return inv, size, dur, date_, results


def voice_buckets():
    inv, size, dur, date_, results = _voice_load()
    archived = _voice_archived()

    def mk(f):
        r = results.get(f, {})
        return {"file": f, "date": (date_.get(f) or "")[:16].replace("T", " "),
                "dur_min": round(dur.get(f, 0) / 60, 1),
                "gb": round(size.get(f, 0) / 1e9, 3),
                "area": r.get("area"), "person": r.get("person"),
                "transcript": r.get("transcript")}

    keep, integ, empty, err, longs = [], [], [], [], []
    for it in inv["items"]:
        f = it["file"]
        if f in archived:
            continue
        d = dur.get(f, 0)
        if d > VOICE_MAX:
            longs.append(mk(f)); continue
        r = results.get(f)
        if not r:
            continue  # aún no procesado por el barrido
        if r.get("error"):
            err.append(mk(f))
        elif r.get("empty"):
            empty.append(mk(f))
        elif r.get("integrated"):
            integ.append(mk(f))
        else:
            keep.append(mk(f))

    def bk(lst):
        return {"count": len(lst), "gb": round(sum(x["gb"] for x in lst), 2),
                "items": sorted(lst, key=lambda x: x["date"])}

    return {"running": _voice_running(), "processed": len(results),
            "total": inv.get("count", len(inv["items"])),
            "archived": len(archived),
            "buckets": {"keep": bk(keep), "integrated": bk(integ),
                        "empty": bk(empty), "long": bk(longs), "error": bk(err)}}


def voice_archive(files):
    """Mueve los .m4a indicados a ~/.openjarvis/voz_archivo/ (reversible)."""
    _, size, _, _, _ = _voice_load()
    archived = _voice_archived()
    moved, freed, errors = 0, 0, []
    for f in files:
        res = _vn.archive_audio(VOICE_REC / f)
        if res.get("ok"):
            moved += 1
            freed += size.get(f, 0)
            archived.add(f)
        else:
            errors.append({"file": f, "error": res.get("error")})
    VOICE_ARCHIVED.write_text(json.dumps(sorted(archived)))
    return {"ok": True, "moved": moved, "freed_gb": round(freed / 1e9, 2), "errors": errors}


VOICE_PAGE = r"""<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>J.A.R.V.I.S - Notas de voz</title>
<style>
:root{--bg:#0a0e14;--card:#121823;--ac:#36d1dc;--mut:#7b8794}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:#e6edf3;font:15px/1.5 -apple-system,system-ui,sans-serif;padding:18px}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:20px;margin:0;color:var(--ac)}
#status{color:var(--mut);font-size:14px}
.live{color:#36d1dc;animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.4}}
button{background:#1c2738;color:#e6edf3;border:1px solid #2a3a52;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:14px}
button:hover{border-color:var(--ac)}
button.arch{background:#3a1c1c;border-color:#5a2a2a}
#buckets{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid #1e2836;border-radius:12px;padding:14px;text-align:center}
.card .h{font-size:14px;margin-bottom:6px}
.card .n{font-size:30px;font-weight:700}
.card .s{color:var(--mut);font-size:12px;min-height:28px}
.card .gb{color:var(--ac);font-size:13px;margin:6px 0}
#detail{margin-top:22px}
.actions{display:flex;gap:14px;align-items:center;margin:10px 0;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:6px 8px;border-bottom:1px solid #1a2230}
h2{font-size:16px}
</style></head><body>
<header>
  <h1>Notas de voz - limpia del backlog</h1>
  <div id=status>cargando...</div>
  <button id=refresh>Actualizar</button>
</header>
<div id=buckets></div>
<div id=detail></div>
<script>
const NAMES={keep:['Con contenido','no estan en la boveda'],integrated:['Ya en la boveda','candidatos a archivar'],empty:['Vacios / silencio','candidatos a archivar'],long:['Largos >30min','sin transcribir - revisar'],error:['Errores','revisar']};
let DATA=null;
async function load(){
  const r=await fetch('/voice/data'); DATA=await r.json();
  const st=document.getElementById('status');
  if(DATA.error){st.textContent='Error: '+DATA.error;return;}
  const pct=DATA.total?Math.round(DATA.processed/DATA.total*100):0;
  st.innerHTML=(DATA.running?'<span class=live>barrido en curso</span> - ':'')+'procesados '+DATA.processed+'/'+DATA.total+' ('+pct+'%) - archivados '+DATA.archived;
  const el=document.getElementById('buckets'); el.innerHTML='';
  for(const k of ['keep','integrated','empty','long','error']){
    const bk=DATA.buckets[k]; if(!bk) continue;
    const c=document.createElement('div'); c.className='card';
    c.innerHTML='<div class=h>'+NAMES[k][0]+'</div><div class=n>'+bk.count+'</div><div class=s>'+NAMES[k][1]+'</div><div class=gb>'+bk.gb+' GB</div>'+(bk.count?'<button data-show="'+k+'">Ver / archivar</button>':'');
    el.appendChild(c);
  }
  document.getElementById('detail').innerHTML='';
}
function show(k){
  const bk=DATA.buckets[k]; const d=document.getElementById('detail');
  let h='<h2>'+NAMES[k][0]+' - '+bk.count+' - '+bk.gb+' GB</h2>';
  h+='<div class=actions><label><input type=checkbox id=all> seleccionar todo</label><button class=arch data-arch="'+k+'">Archivar seleccionados</button></div><table>';
  bk.items.forEach(function(it){ h+='<tr><td><input type=checkbox class=sel value="'+it.file+'"></td><td>'+(it.date||'')+'</td><td>'+it.dur_min+'min</td><td>'+(it.area||'')+(it.person?' - '+it.person:'')+'</td><td>'+(it.transcript?'doc':'')+'</td></tr>'; });
  h+='</table>'; d.innerHTML=h; d.scrollIntoView({behavior:'smooth'});
}
async function archiveSel(k){
  const files=[].slice.call(document.querySelectorAll('.sel:checked')).map(function(x){return x.value;});
  if(!files.length){alert('Selecciona al menos un audio');return;}
  if(!confirm('Archivar '+files.length+' audio(s)? Se mueven a ~/.openjarvis/voz_archivo/ (reversible).'))return;
  const r=await fetch('/voice/archive',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:files})});
  const res=await r.json();
  if(res.ok){alert('Archivados '+res.moved+' - liberados '+res.freed_gb+' GB');load();}else{alert('Error: '+(res.error||'?'));}
}
document.addEventListener('click',function(e){
  const t=e.target;
  if(t.id==='refresh'){load();}
  else if(t.dataset.show){show(t.dataset.show);}
  else if(t.dataset.arch){archiveSel(t.dataset.arch);}
  else if(t.id==='all'){document.querySelectorAll('.sel').forEach(function(x){x.checked=t.checked;});}
});
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencio
        pass

    def _is_proxy(self):
        return self.path.startswith(PROXY_PREFIXES)

    def _serve_hud(self):
        try:
            with open(HUD_FILE, "rb") as f:
                body = f.read()
        except OSError as e:
            self.send_error(500, f"No se encontró el HUD: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # impide que cualquier service worker viejo cachee esta ruta
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        url = CORE + self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=method)
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:
            msg = f'{{"error":"núcleo local inalcanzable: {e}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _serve_tts(self):
        """Devuelve WAV con la voz de Jarvis. Acepta POST {text} o GET ?text=."""
        try:
            text = ""
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    text = (json.loads(raw) or {}).get("text", "")
                except Exception:
                    text = ""
            if not text:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                text = qs.get("text", [""])[0]
            text = (text or "").strip()
            if not text:
                self.send_error(400, "falta texto")
                return
            audio = synth_wav_bytes(text)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(audio)
        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _serve_tasks(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            proj = qs.get("project", [None])[0]
            data = project_tasks(proj) if proj else scan_tasks()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _serve_tasks_all(self):
        try:
            body = json.dumps(all_open_tasks(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({"error": str(e), "tasks": []}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _serve_context(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            ctx = entity_context(q) if q else None
            body = json.dumps({"context": ctx}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({"context": None, "error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _serve_calendar(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            days = int(urllib.parse.parse_qs(parsed.query).get("days", ["10"])[0])
            ctx = calendar_context(days=days)
            body = json.dumps({"context": ctx}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            msg = json.dumps({"context": None, "error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_agenda(self):
        try:
            self._send_json({"items": agenda_rows()})
        except Exception as e:
            self._send_json({"items": [], "error": str(e)}, 500)

    def _serve_reminders(self):
        try:
            self._send_json({"items": reminders_rows()})
        except Exception as e:
            self._send_json({"items": [], "error": str(e)}, 500)

    def _serve_mail(self):
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            promo = qs.get("promo", ["0"])[0] in ("1", "true", "yes")
            items, hidden, blocked = mail_rows(include_promo=promo)
            unread = sum(1 for x in items if x.get("unread"))
            accounts = sorted({x.get("account") for x in items if x.get("account")})
            self._send_json({"items": items, "unread": unread, "hidden": hidden,
                             "blocked": blocked, "accounts": accounts,
                             "blockedList": sorted(load_hidden_senders())})
        except Exception as e:
            self._send_json({"items": [], "unread": 0, "hidden": 0,
                             "blocked": 0, "error": str(e)}, 500)

    def _mail_hide_post(self, unhide=False):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            sender = (data.get("sender") or "").strip()
            if not sender:
                self._send_json({"ok": False, "error": "falta sender"}, 400)
                return
            (remove_hidden_sender if unhide else add_hidden_sender)(sender)
            self._send_json({"ok": True, "blocked": sorted(load_hidden_senders())})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _serve_voice_data(self):
        try:
            self._send_json(voice_buckets())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_voice_page(self):
        body = VOICE_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _voice_archive_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            files = data.get("files") or []
            self._send_json(voice_archive(files))
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _tasks_close_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            res = close_task(
                data.get("path", ""), data.get("line", 0), data.get("text", "")
            )
            self._send_json(res, 200 if res.get("ok") else 409)
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _tasks_close_bulk_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            self._send_json(close_tasks_bulk(data.get("items") or []))
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def do_GET(self):
        if self.path.startswith("/calendar"):
            self._serve_calendar()
        elif self.path.startswith("/context"):
            self._serve_context()
        elif self.path.startswith("/tasks/all"):
            self._serve_tasks_all()
        elif self.path.startswith("/tasks"):
            self._serve_tasks()
        elif self.path.rstrip("/") == "/voz":
            self._serve_voice_page()
        elif self.path.startswith("/voice/data"):
            self._serve_voice_data()
        elif self.path.startswith("/agenda"):
            self._serve_agenda()
        elif self.path.startswith("/reminders"):
            self._serve_reminders()
        elif self.path.startswith("/mail"):
            self._serve_mail()
        elif self.path.startswith("/tts"):
            self._serve_tts()
        elif self._is_proxy():
            self._proxy("GET")
        else:
            self._serve_hud()

    def do_POST(self):
        if self.path.startswith("/tasks/close_bulk"):
            self._tasks_close_bulk_post()
        elif self.path.startswith("/tasks/close"):
            self._tasks_close_post()
        elif self.path.startswith("/voice/archive"):
            self._voice_archive_post()
        elif self.path.startswith("/mail/unhide"):
            self._mail_hide_post(unhide=True)
        elif self.path.startswith("/mail/hide"):
            self._mail_hide_post()
        elif self.path.startswith("/tts"):
            self._serve_tts()
        elif self._is_proxy():
            self._proxy("POST")
        else:
            self.send_error(404)


if __name__ == "__main__":
    print(f"J.A.R.V.I.S HUD -> http://127.0.0.1:{HUD_PORT}/   (núcleo: {CORE})")
    try:
        ThreadingHTTPServer(("127.0.0.1", HUD_PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
