#!/usr/bin/env python3
"""Fase 0b/0c — Barrido del backlog de Memos de voz (nocturno, reanudable).

Transcribe los memos hasta `--max-seconds` (default 1800 = 30 min), del MÁS CORTO
al más largo (hace primero lo más valioso). Por cada uno:
  - transcribe (mlx-whisper large-v3, hilo principal — modelo cacheado tras el 1º)
  - clasifica área/persona (determinista, sin LLM)
  - detecta si YA está integrado en la bóveda (match de frase literal)
  - guarda la transcripción como MD en la bóveda (searchable)
  - registra una línea en results.jsonl (checkpoint → reanudable)

NO borra ni mueve audio. Al terminar (o con `report`) genera el dashboard de triage.
Los memos > max-seconds se listan aparte (no se transcriben).

Uso:
  python voice_backlog.py [--max-seconds 1800]   # corre el barrido
  python voice_backlog.py report                  # regenera el dashboard del avance
"""
import os, sys, re, json, time, unicodedata
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_notes as vn

HOME = Path.home()
REC = HOME / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
INV = HOME / ".openjarvis/voice_inventory.json"
RESULTS = HOME / ".openjarvis/voice_backlog_results.jsonl"
VAULT = vn.VAULT
TRANSCR_DIR = VAULT / "Personal/Notas de voz/transcripciones"
REPORT = VAULT / "Personal/Notas de voz/Triage backlog.md"
MAX_SECONDS = 1800

def log(m): print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)

def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()

def load_done():
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            try:
                r = json.loads(line); done[r["file"]] = r
            except Exception:
                pass
    return done

def build_vault_index():
    """Texto normalizado de toda la bóveda para detectar transcripciones ya escritas."""
    parts = []
    for p in VAULT.rglob("*.md"):
        if "/transcripciones/" in str(p):   # no contar nuestras propias salidas
            continue
        try:
            parts.append(_norm(p.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            pass
    return " \n ".join(parts)

def is_integrated(text, index):
    """True si alguna frase larga del transcript aparece literal en la bóveda."""
    for sent in re.split(r"[.!?\n]+", text):
        w = sent.split()
        if len(w) >= 8:
            frag = _norm(" ".join(w[:12]))
            if len(frag) >= 30 and frag in index:
                return True
    return False

def transcript_path(date_iso, file_stem):
    dt = None
    if date_iso:
        try: dt = datetime.fromisoformat(date_iso)
        except Exception: dt = None
    y = (dt.strftime("%Y") if dt else "sin-fecha")
    name = (dt.strftime("%Y-%m-%d_%H%M") if dt else "sf") + "_" + file_stem[-8:]
    return TRANSCR_DIR / y / f"{name}.md"

def save_transcript(it, text, cls, integrated):
    p = transcript_path(it.get("date"), Path(it["file"]).stem)
    p.parent.mkdir(parents=True, exist_ok=True)
    fecha = (it.get("date") or "")[:16].replace("T", " ")
    dur = it.get("duration_s") or 0
    fm = (f"---\ntipo: transcripcion-voz\nfecha: {fecha}\n"
          f"duracion_min: {round(dur/60,1)}\narea: {vn.AREAS.get(cls['area'],{}).get('label','?')}\n"
          f"persona: {cls.get('person') or ''}\nconfianza: {cls['confidence']}\n"
          f"integrado_en_boveda: {str(integrated).lower()}\n"
          f"origen: {it['file']}\n---\n\n")
    body = f"# Nota de voz · {fecha}\n\n{text.strip()}\n"
    p.write_text(fm + body, encoding="utf-8")
    return str(p.relative_to(VAULT))

def run(max_seconds):
    if not REC.is_dir() or not list(REC.glob("*.m4a"))[:1]:
        log("✗ no veo la carpeta de Memos (¿Acceso a Disco Completo?). Abortando.")
        sys.exit(1)
    inv = json.loads(INV.read_text())
    items = [it for it in inv["items"]]
    todo = [it for it in items if (it.get("duration_s") or 0) <= max_seconds]
    skipped_long = [it for it in items if (it.get("duration_s") or 0) > max_seconds]
    todo.sort(key=lambda x: x.get("duration_s") or 0)   # del más corto al más largo

    done = load_done()
    log(f"backlog: {len(items)} memos · a transcribir ≤{max_seconds//60}min: {len(todo)} "
        f"· >límite (solo lista): {len(skipped_long)} · ya hechos: {len(done)}")
    log("indexando la bóveda para detección de duplicados…")
    index = build_vault_index()
    log(f"índice de bóveda: {len(index)//1000} k chars")

    t0 = time.time(); n_new = 0
    with RESULTS.open("a", encoding="utf-8") as out:
        for i, it in enumerate(todo, 1):
            if it["file"] in done:
                continue
            f = REC / it["file"]
            if not f.exists():
                continue
            dur = it.get("duration_s") or 0
            try:
                text = vn.transcribe(f)
            except Exception as e:
                log(f"⚠ {it['file']}: {e}")
                rec = {"file": it["file"], "date": it.get("date"), "duration_s": dur,
                       "error": str(e)[:200]}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
                continue
            text = text.strip()
            cls = vn.classify(text, complete=None) if text else {
                "area": "personal", "person": None, "confidence": "low", "resumen": ""}
            empty = len(text) < 15
            integrated = (not empty) and is_integrated(text, index)
            rel = save_transcript(it, text, cls, integrated) if not empty else None
            rec = {"file": it["file"], "date": it.get("date"), "duration_s": dur,
                   "chars": len(text), "area": cls["area"], "person": cls.get("person"),
                   "confidence": cls["confidence"], "empty": empty,
                   "integrated": integrated, "transcript": rel}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
            n_new += 1
            if n_new % 5 == 0 or dur > 600:
                el = time.time() - t0
                log(f"{i}/{len(todo)} · {n_new} nuevos · {el/60:.0f}min · "
                    f"último {it['file']} ({dur//60}min, {len(text)} car"
                    f"{' · YA EN BÓVEDA' if integrated else ''}{' · vacío' if empty else ''})")
    log(f"✓ barrido terminado: {n_new} nuevos en {(time.time()-t0)/60:.0f} min")
    write_report()

def write_report():
    done = load_done()
    inv = json.loads(INV.read_text())
    by_dur = {it["file"]: (it.get("duration_s") or 0) for it in inv["items"]}
    longs = sorted(((f, d) for f, d in by_dur.items() if d > MAX_SECONDS),
                   key=lambda x: -x[1])
    recs = list(done.values())
    empty = [r for r in recs if r.get("empty")]
    integ = [r for r in recs if r.get("integrated")]
    keep = [r for r in recs if not r.get("empty") and not r.get("integrated")]
    errs = [r for r in recs if r.get("error")]
    def gb_of(files):
        m = {it["file"]: it["size"] for it in inv["items"]}
        return sum(m.get(f, 0) for f in files)/1e9
    L = []
    L.append("# Triage backlog — Notas de voz\n")
    L.append(f"> Avance: {datetime.now():%Y-%m-%d %H:%M} · {len(recs)} procesados de "
             f"{inv['count']}. Nada borrado (revisa y aprobamos archivar).\n")
    L.append("## Cubetas")
    L.append(f"- 🟢 **Con contenido (no en bóveda):** {len(keep)} → transcripción guardada")
    L.append(f"- 🟡 **Ya integrados en la bóveda:** {len(integ)} → candidatos a archivar "
             f"(~{gb_of(r['file'] for r in integ):.1f} GB)")
    L.append(f"- ⚪ **Vacíos / silencio:** {len(empty)} → candidatos a archivar "
             f"(~{gb_of(r['file'] for r in empty):.1f} GB)")
    L.append(f"- 🔴 **Errores:** {len(errs)}")
    L.append(f"- ⏳ **Largos >{MAX_SECONDS//60}min (sin transcribir):** {len(longs)} "
             f"(~{gb_of(f for f,_ in longs):.1f} GB) → revisar manualmente\n")
    if integ:
        L.append("## 🟡 Ya integrados (archivar)")
        for r in sorted(integ, key=lambda x: x.get("date") or ""):
            L.append(f"- {(r.get('date') or '')[:16].replace('T',' ')} · "
                     f"{(r.get('duration_s') or 0)//60}min · `{r['file']}`")
        L.append("")
    if empty:
        L.append("## ⚪ Vacíos / silencio (archivar)")
        for r in sorted(empty, key=lambda x: x.get("date") or ""):
            L.append(f"- {(r.get('date') or '')[:16].replace('T',' ')} · `{r['file']}`")
        L.append("")
    L.append(f"## ⏳ Largos sin transcribir (top 30 por duración)")
    for f, d in longs[:30]:
        L.append(f"- {d/3600:.1f} h · `{f}`")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L), encoding="utf-8")
    log(f"dashboard → {REPORT}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        write_report()
    else:
        ms = MAX_SECONDS
        if "--max-seconds" in sys.argv:
            ms = int(sys.argv[sys.argv.index("--max-seconds") + 1])
        run(ms)
