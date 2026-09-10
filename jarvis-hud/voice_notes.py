"""voice_notes.py — Transcripción local + clasificación + archivado de notas de voz.

Motor compartido por el bot de Telegram y el vigilante de Memos de voz.
- transcribe(path)            → texto (mlx-whisper large-v3, español)
- classify(texto, complete)   → {area, person, resumen, confidence, candidates}
- file_note(area, person, …)  → escribe/actualiza el MD de la persona
- archive_audio(path)         → mueve el audio a ~/.openjarvis/voz_archivo/ (reversible)

Diseño "no alucinar": la clasificación se apoya en un registro REAL de personas
de la bóveda; el LLM solo ayuda y siempre hay fallback determinista.
"""
import os, re, json, shutil, unicodedata
from pathlib import Path
from datetime import datetime

# mlx-whisper llama a `ffmpeg` por subprocess; bajo launchd el PATH es mínimo.
# Asegura que las rutas de Homebrew estén disponibles para encontrarlo.
for _p in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _p not in os.environ.get("PATH", "").split(os.pathsep) and os.path.isdir(_p):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

VAULT = Path(os.environ.get(
    "VAULT",
    str(Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents")))

# Bóveda del cliente Mapartel (rol Gerente de Operaciones, arranque 2026-08-03).
# Vive en iCloud Drive, FUERA del vault raíz de Obsidian.
MAPARTEL_ROOT = Path(os.environ.get(
    "JARVIS_MAPARTEL_VAULT",
    str(Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/NEXIA/Mapartel(cliente)")))

# --- Áreas y dónde vive el MD de cada persona ---------------------------------
# "sesiones": si el área lo define y la nota NO es de una persona, se archiva
# como transcripción cruda fechada ahí (convención 70_Sesiones/Crudas del rol).
AREAS = {
    "personal":     {"label": "Personal",
                     "personas": VAULT / "Personal/05-Relaciones/personas"},
    "nexia":        {"label": "Nexia",
                     "personas": VAULT / "Nexia/Personas"},
    "mapartel":     {"label": "Mapartel (GO)",
                     "personas": MAPARTEL_ROOT / "50_Personas (Restringido)",
                     "sesiones": MAPARTEL_ROOT / "70_Sesiones/Crudas"},
    "villacatania": {"label": "Villa Catania",
                     "personas": VAULT / "VillaCataniaVault/Comunidad/01_Directorio"},
}

# Pistas de área por palabra clave (fallback determinista, sin acentos/minúsculas)
AREA_HINTS = {
    "mapartel": ["mapartel", "jorge", "ivonne", "pamela", "maribel",
                 "juan carlos", "nayeli", "lidia", "junta de las 8", "junta 8:30",
                 "gemba", "piso", "produccion", "smt", "pth", "ola 2", "crisol",
                 "horacio", "hora por hora", "hora x hora", "tablero", "paro",
                 "almacen", "inventario", "programa de produccion", "oee",
                 "scrap", "advanta", "erp", "linea base", "mandato", "compuerta",
                 "gerencia", "gerente de operaciones", "direccion", "operadora",
                 "turno", "capacitacion", "mantenimiento", "calidad"],
    "nexia": ["nexia", "cliente", "bohn", "borgwarner", "lensys",
              "capistrano", "consultoria", "lean", "vsm", "smed", "heijunka",
              "supabase", "n8n", "app", "automatizacion", "pdca", "factura",
              "daniel", "prospecto", "proceso", "kpi"],
    "villacatania": ["villa catania", "catania", "condominio", "fraccionamiento",
                     "asamblea", "cuota", "vecino", "vecinos", "administracion",
                     "comite", "mantenimiento", "caseta", "porton", "alberca",
                     "areas comunes", "cuota de mantenimiento", "junta vecinal"],
    "personal": ["familia", "esposa", "hijo", "hija", "mama", "papa", "casa",
                 "doctor", "salud", "personal", "amigo", "amiga"],
}

ARCHIVE_DIR = Path.home() / ".openjarvis" / "voz_archivo"
WHISPER_REPO = os.environ.get("JARVIS_WHISPER_REPO",
                              "mlx-community/whisper-large-v3-mlx")


# --- utilidades ----------------------------------------------------------------
def _strip(s: str) -> str:
    """minúsculas sin acentos para comparar."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", (name or "").strip())
    return name[:80] or "Sin nombre"


# --- transcripción -------------------------------------------------------------
def transcribe(path) -> str:
    """Transcribe un audio (ogg/m4a/wav) a texto en español con mlx-whisper."""
    import mlx_whisper
    res = mlx_whisper.transcribe(str(path), path_or_hf_repo=WHISPER_REPO,
                                 language="es", verbose=False)
    return (res.get("text") or "").strip()


# --- registro de personas reales de la bóveda ---------------------------------
def people_registry() -> dict:
    """{nombre_normalizado: {'name', 'area', 'path'}} escaneando las 3 carpetas."""
    reg = {}
    for area_key, cfg in AREAS.items():
        d = cfg["personas"]
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            name = p.stem
            if name.startswith("_") or name.lower().startswith("inbox"):
                continue
            # notas fechadas o de reglas (p.ej. "00 - LEEME", "2026-07-27 - …")
            # no son fichas de persona — típico en 50_Personas (Restringido)
            if re.match(r"^\d", name):
                continue
            reg[_strip(name)] = {"name": name, "area": area_key, "path": p}
    return reg


def _match_person(text: str, reg: dict):
    """Devuelve (entry, area_key) si el texto menciona a una persona conocida."""
    t = _strip(text)
    best = None
    for key, entry in reg.items():
        # nombre completo, o primer nombre con frontera de palabra
        full = key
        first = key.split()[0] if key else ""
        if full and re.search(r"\b" + re.escape(full) + r"\b", t):
            return entry, entry["area"]
        if len(first) >= 4 and re.search(r"\b" + re.escape(first) + r"\b", t):
            best = best or (entry, entry["area"])
    return best if best else (None, None)


def _hint_area(text: str):
    t = _strip(text)
    scores = {k: 0 for k in AREA_HINTS}
    for area, words in AREA_HINTS.items():
        for w in words:
            if w in t:
                scores[area] += 1
    top = max(scores, key=scores.get)
    return (top, scores[top]) if scores[top] > 0 else (None, 0)


def classify(text: str, complete=None) -> dict:
    """Clasifica el texto en {area, person, resumen, confidence, candidates}.

    confidence: 'high'  → persona conocida mencionada (área y persona ciertas)
                'medium'→ área por LLM o pistas, persona propuesta
                'low'   → sin certeza → el bot debe preguntar con botones
    `complete(msgs)` es opcional (la llamada al LLM); si falla, todo determinista.
    """
    reg = people_registry()
    person_entry, person_area = _match_person(text, reg)

    # Guard de sesiones largas: una grabación extensa es una junta/sesión, no
    # una nota sobre la persona mencionada — no forzar área/persona por nombre.
    es_sesion = len(text or "") > 900
    if es_sesion:
        person_entry, person_area = None, None

    # 1) LLM (ayuda, no manda): área + persona + resumen breve
    llm = {}
    if complete:
        try:
            sys_p = ("Eres un clasificador. Lee la nota y responde SOLO un JSON: "
                     '{"area":"personal|nexia|mapartel|villacatania","persona":"nombre o null",'
                     '"resumen":"1 frase"}. '
                     "mapartel=planta Mapartel donde Juan es Gerente de Operaciones: "
                     "juntas, piso, producción, almacén, dirección (Jorge/Ivonne/"
                     "Pamela/Maribel), Horacio, capacidad, inventario. "
                     "nexia=consultoría NEXIA/otros clientes/apps propias. "
                     "villacatania=condominio/vecinos/asamblea/cuotas. "
                     "personal=familia/salud/amigos/casa. "
                     "Si es la grabación de una junta o sesión de trabajo, persona=null.")
            raw = complete([{"role": "system", "content": sys_p},
                            {"role": "user", "content": text[:2000]}])
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                llm = json.loads(m.group(0))
        except Exception:
            llm = {}

    hint_area, hint_score = _hint_area(text)

    # --- decidir área ---
    if person_entry:
        area = person_area
        confidence = "high"
    elif llm.get("area") in AREAS:
        area = llm["area"]
        confidence = "medium" if (hint_area in (None, area)) else "low"
    elif hint_area:
        area = hint_area
        confidence = "medium" if hint_score >= 2 else "low"
    else:
        area = "personal"
        confidence = "low"

    # --- decidir persona ---
    if person_entry:
        person = person_entry["name"]
    else:
        cand = (llm.get("persona") or "").strip()
        person = cand if cand and cand.lower() not in ("null", "none", "") else None

    # candidatos del área (para botones)
    candidates = [e["name"] for k, e in reg.items() if e["area"] == area]

    return {
        "area": area,
        "person": person,
        "resumen": (llm.get("resumen") or "").strip(),
        "confidence": confidence,
        "candidates": sorted(candidates)[:6],
    }


# --- archivado del texto en el MD de la persona -------------------------------
def file_note(area: str, person: str | None, text: str,
              when: datetime | None = None, source: str = "voz",
              resumen: str = "", icon: str = "🎙️") -> dict:
    """Agrega una sección fechada al MD de la persona (lo crea si no existe).
    Si person es None → 'Inbox de voz.md' del área, EXCEPTO si el área define
    "sesiones": ahí la nota se guarda como transcripción cruda fechada
    (convención 70_Sesiones/Crudas de la Gerencia de Operaciones Mapartel)."""
    if area not in AREAS:
        return {"ok": False, "error": f"área desconocida: {area}"}
    when = when or datetime.now()

    # --- sesión cruda (área con carpeta de sesiones y sin persona) -----------
    if not person and AREAS[area].get("sesiones"):
        folder = AREAS[area]["sesiones"]
        folder.mkdir(parents=True, exist_ok=True)
        tema = _safe_filename(resumen)[:60] if resumen else f"Nota de voz {when:%H%M}"
        target = folder / f"{when:%Y-%m-%d} - CRUDA - {tema}.md"
        n = 2
        while target.exists():
            target = folder / f"{when:%Y-%m-%d} - CRUDA - {tema} ({n}).md"
            n += 1
        fm = (f"---\ntipo: transcripcion\narea: general\nfecha: {when:%Y-%m-%d}\n"
              f"estado: crudo\norigen: {source}\n---\n\n"
              f"# {when:%Y-%m-%d} · {icon} {tema}\n\n"
              f"> Capturada por Jarvis ({source}, {when:%H:%M}). "
              f"Procesar a nota limpia en 70_Sesiones/Procesadas.\n\n"
              f"{text.strip()}\n")
        target.write_text(fm, encoding="utf-8")
        return {"ok": True, "path": str(target), "rel": _rel(target),
                "nuevo": True, "area_label": AREAS[area]["label"],
                "title": target.stem}

    folder = AREAS[area]["personas"]
    folder.mkdir(parents=True, exist_ok=True)

    if person:
        fname = _safe_filename(person)
        target = folder / f"{fname}.md"
        title = person
        tipo = "persona"
    else:
        target = folder / "Inbox de voz.md"
        title = "Inbox de voz"
        tipo = "inbox"

    nuevo = not target.exists()
    if nuevo:
        fm = (f"---\ntipo: {tipo}\narea: {AREAS[area]['label']}\n"
              f"creado: {when:%Y-%m-%d}\n")
        if area == "mapartel":
            fm += "restringido: true\n"
        fm += f"---\n\n# {title}\n"
        if area == "mapartel" and tipo == "persona":
            fm += ("\n> 🔒 Capa restringida — hechos con fecha, separados de la "
                   "interpretación. Nada de aquí se cita fuera de la carpeta.\n")
        target.write_text(fm, encoding="utf-8")

    head = f"## {when:%Y-%m-%d %H:%M} · {icon} {source}"
    if resumen:
        head += f" — {resumen}"
    body = f"\n{head}\n\n{text.strip()}\n"
    with target.open("a", encoding="utf-8") as f:
        f.write(body)

    return {"ok": True, "path": str(target), "rel": _rel(target),
            "nuevo": nuevo, "area_label": AREAS[area]["label"], "title": title}


def _rel(target: Path) -> str:
    """Ruta legible: relativa al vault de Obsidian o a la bóveda Mapartel."""
    for root, prefix in ((VAULT, ""), (MAPARTEL_ROOT, "Mapartel(cliente)/")):
        try:
            return prefix + str(target.relative_to(root))
        except ValueError:
            continue
    return str(target)


# --- archivar el audio (mover, reversible) ------------------------------------
def archive_audio(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": "el audio ya no existe"}
    year = "otros"
    m = re.match(r"(\d{4})", p.name)
    if m:
        year = m.group(1)
    dest_dir = ARCHIVE_DIR / year
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / p.name
    shutil.move(str(p), str(dest))
    return {"ok": True, "dest": str(dest)}
