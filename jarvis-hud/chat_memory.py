"""chat_memory.py — Memoria conversacional persistente de J.A.R.V.I.S.

Todo lo que pasa por el bot queda registrado y se destila a Obsidian:
- log_exchange()/log_event() → ~/.openjarvis/chat_log.jsonl (append, nunca truena)
- read_day(day)              → eventos de un día
- distill_day(day, complete) → escribe/actualiza la nota diaria en el vault
                               (Personal/Diario Jarvis/YYYY-MM-DD.md)

Diseño "no alucinar": las secciones de acciones y transcripción son
deterministas (salen del log tal cual); el LLM solo redacta el resumen y
propone compromisos detectados, y si falla la nota se escribe igual.
"""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

LOG_FILE = Path.home() / ".openjarvis" / "chat_log.jsonl"

VAULT = Path(os.environ.get(
    "VAULT",
    str(Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents")))
DIARY_DIR = VAULT / "Personal" / "Diario Jarvis"

_THINK_RE = re.compile(r"<think>[\s\S]*?</think>")


# ─── Registro (append-only, a prueba de fallos) ──────────────────────────────
def log_event(kind, chat_id=None, **fields):
    """Agrega una línea al log. NUNCA lanza: la memoria no debe tumbar al bot."""
    try:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "kind": kind, "chat_id": chat_id}
        rec.update(fields)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_exchange(chat_id, user_text, reply, mode="local"):
    """Registra un turno completo de conversación (pregunta + respuesta)."""
    log_event("chat", chat_id=chat_id, user=user_text, reply=reply, mode=mode)


def read_day(day=None):
    """Eventos del día (date o 'YYYY-MM-DD'). Líneas corruptas se ignoran."""
    day = day or date.today()
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    out = []
    try:
        with LOG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith('{"ts": "' + key):
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return out


# ─── Destilado diario → nota en Obsidian ─────────────────────────────────────
def _hhmm(ts):
    try:
        return ts[11:16]
    except Exception:
        return "--:--"


def _transcript_lines(events, max_chars=15000):
    """Transcripción legible del día (recorta lo más viejo si se pasa)."""
    lines = []
    for e in events:
        t = _hhmm(e.get("ts", ""))
        kind = e.get("kind")
        if kind == "chat":
            lines.append(f"[{t}] Juan: {e.get('user', '').strip()}")
            reply = _THINK_RE.sub("", e.get("reply", "")).strip()
            lines.append(f"[{t}] Jarvis: {reply}")
        elif kind == "nota":
            who = e.get("title") or "Inbox"
            lines.append(f"[{t}] 📝 Nota archivada → {e.get('area', '')} · {who}")
        elif kind == "accion":
            lines.append(f"[{t}] ⚙️ {e.get('detalle', '')}")
        elif kind == "brief":
            lines.append(f"[{t}] 📣 Brief enviado ({e.get('tipo', '')})")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "(…recortado…)\n" + text[-max_chars:]
    return text


def _actions_section(events):
    rows = []
    for e in events:
        t = _hhmm(e.get("ts", ""))
        if e.get("kind") == "accion":
            rows.append(f"- {t} — {e.get('detalle', '')}")
        elif e.get("kind") == "nota":
            who = e.get("title") or "Inbox"
            src = e.get("source", "")
            rows.append(f"- {t} — Nota archivada en **{e.get('area', '')} · {who}**"
                        + (f" ({src})" if src else ""))
    return "\n".join(rows) if rows else "*(sin acciones ejecutadas)*"


def distill_day(day=None, complete=None):
    """Escribe la nota diaria a partir del log. Idempotente: se regenera
    completa desde el log, así que correrla dos veces no duplica nada.

    `complete(msgs) -> str` es opcional (LLM para resumen + compromisos);
    si no viene o falla, la nota se escribe con secciones deterministas.
    """
    day = day or date.today()
    key = day.isoformat() if hasattr(day, "isoformat") else str(day)
    events = read_day(day)
    if not events:
        return {"ok": False, "reason": "sin actividad registrada"}

    chats = [e for e in events if e.get("kind") == "chat"]
    resumen = ""
    compromisos = ""
    if complete and chats:
        try:
            sys_p = (
                "Lee la transcripción del día entre Juan y su asistente Jarvis. "
                "Responde SOLO un JSON, empezando con '{' y terminando con '}':\n"
                '{"resumen": "3-5 frases con lo importante del día", '
                '"compromisos": ["cosas que Juan dijo que hará o quedó de hacer, '
                'con persona y fecha si las mencionó"]}\n'
                "En compromisos incluye SOLO lo dicho explícitamente en la "
                "transcripción (frases como «quedé de», «voy a», «hay que», "
                "«mándale», «le digo a»). Si no hay, lista vacía. No inventes."
            )
            raw = complete([{"role": "system", "content": sys_p},
                            {"role": "user",
                             "content": _transcript_lines(chats, max_chars=12000)}])
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                data = json.loads(m.group(0))
                resumen = (data.get("resumen") or "").strip()
                comp = [c.strip() for c in (data.get("compromisos") or []) if c.strip()]
                compromisos = "\n".join(f"- [ ] {c}" for c in comp)
        except Exception:
            pass

    if not resumen:
        resumen = (f"{len(chats)} intercambios con Jarvis este día. "
                   "(Resumen automático no disponible — ver transcripción.)")
    if not compromisos:
        compromisos = "*(ninguno detectado)*"

    transcript = _transcript_lines(events)
    quoted = "\n".join("> " + ln for ln in transcript.splitlines())

    md = (
        f"---\ntipo: diario-jarvis\nfecha: {key}\n---\n\n"
        f"# Diario Jarvis — {key}\n\n"
        f"## Resumen\n{resumen}\n\n"
        f"## Compromisos y seguimientos\n{compromisos}\n\n"
        f"## Acciones ejecutadas\n{_actions_section(events)}\n\n"
        f"> [!note]- 💬 Transcripción del día ({len(chats)} turnos)\n{quoted}\n"
    )
    try:
        DIARY_DIR.mkdir(parents=True, exist_ok=True)
        target = DIARY_DIR / f"{key}.md"
        target.write_text(md, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "reason": f"no pude escribir la nota: {e}"}
    return {"ok": True, "path": str(target),
            "rel": str(target.relative_to(VAULT)), "chats": len(chats)}
