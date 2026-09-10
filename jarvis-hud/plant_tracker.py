"""plant_tracker.py — Tracker diario del rol en planta (Gerencia de Operaciones Mapartel).

Lee la nota-tracker del día en TRACKER_DIR (un archivo `AAAA-MM-DD - *.md` con
checkboxes; la hora al inicio del texto marca cuándo debe estar hecha) y
calcula hechas / abiertas / ATRASADAS para que el bot dé retroalimentación en
los checkpoints del día. Este módulo solo LEE — el palomeo usa los flujos
existentes (chat «cierra …», HUD, Obsidian).

Convención de la nota:
  - [ ] 8:30 Junta de piso observada con notas
  - [ ] Sesión con Maribel (hora por confirmar)     ← sin hora: nunca "atrasada"
Las secciones de semana llevan 📅 AAAA-MM-DD (las ve el brief), no horas.
"""
import os
import re
from datetime import date, datetime
from pathlib import Path

VAULT = Path(os.environ.get(
    "VAULT",
    str(Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents")))
TRACKER_DIR = Path(os.environ.get(
    "JARVIS_TRACKER_DIR",
    str(VAULT / "Nexia/Consultoria Industrial/Clientes/Mapartel/06_Seguimiento/Tracker Diario")))
# minutos de gracia después de la hora marcada antes de considerarla atrasada
GRACE_MIN = int(os.environ.get("JARVIS_TRACKER_GRACE_MIN", "20"))

OPEN_RE = re.compile(r"^\s*[-*]\s+\[ \]\s*(.+?)\s*$")
DONE_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s*(.+?)\s*$")
# hora AL INICIO del texto del checkbox (evita falsos positivos tipo "junta 8:30")
HORA_RE = re.compile(r"^(?:⏰\s*)?([01]?\d|2[0-3]):([0-5]\d)\b")


def _mins(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def today_file(d: date | None = None) -> Path | None:
    d = d or date.today()
    if not TRACKER_DIR.is_dir():
        return None
    hits = sorted(TRACKER_DIR.glob(f"{d.isoformat()} - *.md"))
    return hits[0] if hits else None


def parse(path: Path) -> list[dict]:
    items = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return items
    for idx, ln in enumerate(lines, start=1):
        mo, done = OPEN_RE.match(ln), False
        if not mo:
            mo, done = DONE_RE.match(ln), True
        if not mo:
            continue
        text = mo.group(1).strip()
        hm = HORA_RE.match(text)
        hora = f"{int(hm.group(1)):02d}:{hm.group(2)}" if hm else None
        items.append({"text": text, "hora": hora, "done": done, "line": idx})
    return items


def day_status(now: datetime | None = None) -> dict | None:
    """None si no hay tracker para hoy; si hay, el corte del día."""
    now = now or datetime.now()
    f = today_file(now.date())
    if not f:
        return None
    items = parse(f)
    now_m = now.hour * 60 + now.minute
    hechas = [i for i in items if i["done"]]
    abiertas = [i for i in items if not i["done"]]
    atrasadas = [i for i in abiertas
                 if i["hora"] and _mins(i["hora"]) + GRACE_MIN < now_m]
    proximas = sorted((i for i in abiertas
                       if i["hora"] and _mins(i["hora"]) + GRACE_MIN >= now_m),
                      key=lambda i: _mins(i["hora"]))
    return {"file": f, "items": items, "hechas": hechas, "abiertas": abiertas,
            "atrasadas": atrasadas, "proximas": proximas,
            "sin_hora": [i for i in abiertas if not i["hora"]]}


def feedback(now: datetime | None = None, only_if_late: bool = True) -> str | None:
    """Mensaje de checkpoint. None si no hay tracker o (only_if_late) sin atrasos."""
    now = now or datetime.now()
    st = day_status(now)
    if not st:
        return None
    if only_if_late and not st["atrasadas"]:
        return None
    lines = [f"🏭 *Checkpoint de planta* · {now:%H:%M} — {st['file'].stem}"]
    if st["atrasadas"]:
        lines.append("\n⚠️ *Atrasado contra la agenda:*")
        lines += [f"  · {i['text']}" for i in st["atrasadas"][:8]]
        lines.append("\nSi ya está hecho, palomee con «pendientes de mapartel» → "
                     "«cierra …». Si no, ¿qué lo está bloqueando?")
    else:
        lines.append("\n✅ Sin atrasos. Va bien, Juanchi.")
    lines.append(f"\n✅ {len(st['hechas'])} hechas · ⬜ {len(st['abiertas'])} abiertas"
                 + (f" ({len(st['sin_hora'])} sin hora)" if st["sin_hora"] else ""))
    if st["proximas"]:
        lines.append(f"⏭ Siguiente: {st['proximas'][0]['text']}")
    return "\n".join(lines)


def resumen(now: datetime | None = None) -> str:
    """Estatus completo a demanda (/dia)."""
    now = now or datetime.now()
    st = day_status(now)
    if not st:
        return ("📋 No hay tracker para hoy. Cree la nota "
                f"`{now:%Y-%m-%d} - Tracker….md` en\n`{TRACKER_DIR}`\n"
                "con checkboxes «- [ ] HH:MM actividad» y lo vigilo.")
    marca = {True: "✅", False: "⬜"}
    lines = [f"📋 *{st['file'].stem}* · corte {now:%H:%M}\n"]
    atras = {id(i) for i in st["atrasadas"]}
    for i in st["items"]:
        icon = "⚠️" if id(i) in atras else marca[i["done"]]
        lines.append(f"{icon} {i['text']}")
    lines.append(f"\n✅ {len(st['hechas'])}/{len(st['items'])} · "
                 f"⚠️ {len(st['atrasadas'])} atrasadas")
    lines.append("_Palomee desde Obsidian o con «pendientes de mapartel» → «cierra …»._")
    return "\n".join(lines)
