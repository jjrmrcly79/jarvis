"""onboarding.py — Entrevista inicial del segundo cerebro de J.A.R.V.I.S.

El bot entrevista a Juan (6 pasos) y con las respuestas escribe el NÚCLEO del
segundo cerebro en el vault: Personal/Segundo Cerebro/Perfil (Jarvis).md.
Ese perfil después se inyecta como contexto en los briefs y el estatus del día
(profile_context), para que Jarvis priorice según las metas reales de Juan.

Diseño "no alucinar": el perfil es literal lo que Juan contestó (sin LLM);
re-correr /onboarding regenera el archivo (las respuestas viejas se conservan
al final como historial fechado).
"""
import os
import re
from datetime import datetime
from pathlib import Path

VAULT = Path(os.environ.get(
    "VAULT",
    str(Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents")))
PROFILE = VAULT / "Personal" / "Segundo Cerebro" / "Perfil (Jarvis).md"

STEPS = [
    {"key": "metas", "title": "🎯 Metas del trimestre",
     "q": "1/6 · 🎯 ¿Cuáles son tus 3 metas principales de este trimestre, Juanchi? "
          "(una por línea)"},
    {"key": "nexia", "title": "🏭 Nexia — prioridades",
     "q": "2/6 · 🏭 En NEXIA, ¿qué es lo más importante ahora? "
          "(clientes/proyectos prioritarios y por qué)"},
    {"key": "personal", "title": "🏠 Personal — qué cuidar",
     "q": "3/6 · 🏠 En lo PERSONAL (salud, familia, finanzas), "
          "¿qué quiere cuidar o lograr?"},
    {"key": "villacatania", "title": "🏘️ Villa Catania",
     "q": "4/6 · 🏘️ En VILLA CATANIA, ¿cuál es su rol y qué hay pendiente?"},
    {"key": "personas", "title": "👥 Personas clave",
     "q": "5/6 · 👥 ¿Quiénes son las personas clave con las que trata? "
          "(«nombre — quién es», una por línea)"},
    {"key": "vigilar", "title": "🛡️ Qué debo vigilar",
     "q": "6/6 · 🛡️ ¿Qué quiere que yo vigile y le recuerde sin que me lo pida? "
          "(ej. pendientes vencidos, correos de alguien, hábitos, cobros)"},
]


def profile_exists() -> bool:
    return PROFILE.exists()


def write_profile(data: dict) -> dict:
    """Escribe/regenera el perfil. Si ya existía, conserva el anterior al final
    como historial fechado (nunca se pierde una respuesta vieja)."""
    now = datetime.now()
    old_body = ""
    if PROFILE.exists():
        try:
            old = PROFILE.read_text(encoding="utf-8")
            # quita frontmatter e historiales previos anidados para no crecer sin fin
            old = re.sub(r"^---[\s\S]*?---\n", "", old).strip()
            old = old.split("\n## 📜 Historial")[0].strip()
            if old:
                old_body = (f"\n## 📜 Historial (perfil anterior, "
                            f"reemplazado {now:%Y-%m-%d})\n\n"
                            + "\n".join("> " + ln for ln in old.splitlines()) + "\n")
        except OSError:
            pass

    secs = []
    for step in STEPS:
        ans = (data.get(step["key"]) or "").strip()
        secs.append(f"## {step['title']}\n{ans if ans else '*(sin respuesta)*'}\n")

    md = (f"---\ntipo: perfil-jarvis\nactualizado: {now:%Y-%m-%d %H:%M}\n---\n\n"
          f"# Perfil (Jarvis) — núcleo del segundo cerebro\n\n"
          f"> Generado por el onboarding de J.A.R.V.I.S. Re-correr `/onboarding` "
          f"lo actualiza (el anterior queda en el historial).\n\n"
          + "\n".join(secs) + old_body)
    try:
        PROFILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE.write_text(md, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "path": str(PROFILE), "rel": str(PROFILE.relative_to(VAULT))}


def profile_context(max_chars: int = 2500):
    """Bloque de contexto con el perfil para inyectar en briefs/estatus.
    None si no hay perfil todavía."""
    if not PROFILE.exists():
        return None
    try:
        body = PROFILE.read_text(encoding="utf-8")
    except OSError:
        return None
    body = re.sub(r"^---[\s\S]*?---\n", "", body)
    body = body.split("\n## 📜 Historial")[0].strip()   # solo el perfil vigente
    if len(body) > max_chars:
        body = body[:max_chars] + "\n(…recortado)"
    return ("PERFIL REAL de Juan (núcleo de su segundo cerebro — metas y "
            "prioridades que ÉL definió). Úsalo como lente para priorizar y "
            "relacionar lo demás; no lo repitas completo:\n" + body)
