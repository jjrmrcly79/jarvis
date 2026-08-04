#!/usr/bin/env python3
"""J.A.R.V.I.S — Bot de Telegram (puente a tu Jarvis local).

Habla con tu Jarvis desde el celular cuando no estás en la Mac.
- Long-polling (no necesita IP pública ni abrir puertos).
- Usa tu núcleo local (Ollama) CON memoria de Obsidian + pendientes.
- Candado por chat ID: solo TÚ puedes usarlo (allowlist).

La Mac debe estar encendida con el núcleo corriendo (`jarvis-hud` o el servicio).

Config por entorno (opcional):
  JARVIS_TG_TOKEN    token del bot (default: el de tu config)
  JARVIS_CORE        URL del núcleo (default http://127.0.0.1:8000)
Allowlist: ~/.openjarvis/telegram_allowed.txt  (un chat ID por línea)
"""
import os, sys, json, re, asyncio, socket, time, uuid, urllib.request
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serve_hud   # reutiliza el escáner de pendientes (scan_tasks/project_tasks)
import voice_notes as vn  # transcripción + clasificación + archivado de notas de voz
import chat_memory as cm  # log persistente de conversaciones + diario en Obsidian
import onboarding as ob   # entrevista inicial → Perfil (Jarvis).md en el vault

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)
from telegram.error import NetworkError, TimedOut

TOKEN = os.environ.get("JARVIS_TG_TOKEN")
if not TOKEN:
    sys.exit("Falta JARVIS_TG_TOKEN en el entorno "
             "(configúralo en el plist de launchd o expórtalo antes de correr).")
CORE = os.environ.get("JARVIS_CORE", "http://127.0.0.1:8000")
MODEL = os.environ.get("JARVIS_MODEL", "qwen3.5:27b")
ALLOW_FILE = Path.home() / ".openjarvis" / "telegram_allowed.txt"

# --- Modo dios (Claude API) -------------------------------------------------
# Híbrido: Ollama por defecto (local, $0); "modo dios" enruta a Claude para
# tareas pesadas. Se prende/apaga por chat con /dios y /normal.
GOD_MODEL = os.environ.get("JARVIS_GOD_MODEL", "claude-opus-4-8")
GOD_MODE = {}            # chat_id -> bool (modo dios activo en ese chat)
_claude_client = None    # cliente Anthropic perezoso (se crea al primer uso)


def _claude_available():
    """True si hay API key de Anthropic y el SDK está instalado."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _get_claude():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic()  # lee ANTHROPIC_API_KEY del entorno
    return _claude_client

SYSTEM = ("Eres Jarvis (con la voz de Angie), la asistente personal de Juan, por "
          "Telegram. Hablas español colombiano, cálido y coloquial, como una amiga "
          "paisa de confianza: lo llamas 'Juanchi' o 'Juancho' (NUNCA 'señor' ni "
          "'jefe'), saludas con «¿qué más, Juanchi?» o parecido y te despides con "
          "«chaíto» (nunca 'chao', 'adiós' ni 'buenas noches' a secas). Usas jerga "
          "COLOMBIANA con naturalidad y sin exagerar: parce, listo, de una, bacano, "
          "chévere, hágale pues, qué pena (para disculparte), con mucho gusto, a la "
          "orden. PROHIBIDA la jerga de otros países: nada de 'qué onda', 'órale', "
          "'güey', 'tío', 'che'. Ejemplos de tu estilo: «¿Qué más, Juanchi? ¿Cómo va "
          "todo?» · «Listo parce, de una.» · «Uy, qué pena Juancho, eso no lo "
          "encontré.» · «Chaíto, que descanses.» Puedes usar 'usted' paisa o tutear, "
          "como salga natural. Concisa (1-4 frases salvo que pidan detalle). Nunca "
          "inventas datos: si te dan DATOS REALES de notas/pendientes, úsalos tal "
          "cual.")

histories = {}   # chat_id -> [mensajes]
LAST_TASKS = {}  # chat_id -> [tarea|None] última lista numerada mostrada (None = ya cerrada)

# --- estado de notas de voz ---
PENDING = {}        # token -> estado de una nota en clasificación
PENDING_NAME = {}   # chat_id -> token (esperando que el usuario escriba un nombre)
VOICE_TMP = Path.home() / ".openjarvis" / "voz_tmp"

# --- voz de salida (Jarvis responde hablando) ---
import shutil
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
VOICE_REPLY = {}                 # chat_id -> bool (responder con voz). Default: ON
VOICE_MAX_CHARS = 700            # respuestas más largas van solo en texto
_MD_RE = re.compile(r"[*_`#>]")  # quita markdown antes de sintetizar


def _tts_voice_enabled(chat_id):
    return VOICE_REPLY.get(chat_id, True)


async def synth_voice_ogg(text):
    """Texto -> nota de voz OGG/Opus (bytes) con la voz Piper de Jarvis.
    Devuelve None si algo falla; el bot nunca debe romperse por la voz."""
    clean = _MD_RE.sub("", text or "").strip()
    if not clean or len(clean) > VOICE_MAX_CHARS:
        return None
    try:
        wav = await asyncio.to_thread(serve_hud.synth_wav_bytes, clean)
    except Exception as e:
        print(f"[tts] síntesis falló: {e}", file=sys.stderr)
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        ogg, err = await proc.communicate(input=wav)
        if proc.returncode != 0 or not ogg:
            print(f"[tts] ffmpeg rc={proc.returncode}: {err.decode('utf-8','replace')[-200:]}",
                  file=sys.stderr)
            return None
        return ogg
    except Exception as e:
        print(f"[tts] ffmpeg error: {e}", file=sys.stderr)
        return None


async def reply_with_voice(update, chat_id, text):
    """Envía la respuesta en texto y, si la voz está activa, también como nota de voz."""
    await update.message.reply_text(text)
    if not _tts_voice_enabled(chat_id):
        return
    ogg = await synth_voice_ogg(text)
    if ogg:
        try:
            await update.message.reply_voice(ogg)
        except Exception as e:
            print(f"[tts] reply_voice falló: {e}", file=sys.stderr)


def allowed_ids():
    try:
        return {l.strip() for l in ALLOW_FILE.read_text().splitlines() if l.strip()}
    except OSError:
        return set()


def is_task_query(t):
    import re
    return bool(re.search(r"pendient|tarea|to-?do|qu[eé] (tengo|hay|falta|debo|hacer)|"
                          r"checklist|por hacer|vencid", t, re.I))


def is_cal_query(t):
    import re
    return bool(re.search(r"agenda|calendario|evento|reuni[oó]n|junta|cita|"
                          r"qu[eé] tengo (hoy|ma[ñn]ana|esta semana|el|este)|"
                          r"agendad|disponib|libre (hoy|ma[ñn]ana)|mi d[ií]a", t, re.I))


# ─── Crear evento en el Calendario de Mac (acción real, no alucinada) ─────────
_CREATE_VERB = re.compile(
    r"\b(ag[eé]nda(?:me)?|agendar|cr[eé]a(?:me)?|pon(?:me|ga)?|"
    r"programa(?:r|me)?|ap[uú]nta(?:me)?|a[ñn][aá]de|agrega|registra)\b", re.I)
_EVENT_NOUN = re.compile(
    r"\b(reuni[oó]n|junta|cita|evento|llamada|comida|cena|caf[eé]|"
    r"meeting|recordatorio)\b", re.I)
_TIME_HINT = re.compile(
    r"(\b\d{1,2}\s*(?:am|pm|hrs?|h)\b|\b\d{1,2}:\d{2}\b|mediod[ií]a)", re.I)

_DOW_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MON_ES = ["", "ene", "feb", "mar", "abr", "may", "jun",
           "jul", "ago", "sep", "oct", "nov", "dic"]


def is_cal_create(t):
    """Intención de CREAR un evento (verbo de creación + nombre de evento u hora)."""
    return bool(_CREATE_VERB.search(t) and (_EVENT_NOUN.search(t) or _TIME_HINT.search(t)))


def is_mail_query(t):
    """Consulta sobre el correo (resumen/revisión de la bandeja de hoy)."""
    return bool(re.search(r"\b(correos?|e-?mails?|mails?|bandeja|inbox|buz[oó]n)\b",
                          t, re.I))


# ─── Búsqueda en el HISTORIAL de correo («¿qué me ha escrito Marco?») ─────────
_MAILSEARCH_RE = re.compile(
    r"(?:qu[eé] me han? (?:escrito|mandado|enviado)(?:\s+de(?:sde)?|\s+sobre)?\s+|"
    r"me (?:escribi[oó]|mand[oó]|envi[oó])\s+(?:algo\s+)?|"
    r"busca(?:me)?\s+(?:en el\s+)?correos?\s+(?:de|sobre|con)\s+|"
    r"correos?\s+(?:de|sobre)\s+|alg[uú]n\s+correo\s+de\s+)"
    r"([\w@.\-áéíóúñü ]{2,40})", re.I)

_DAY_TERMS = re.compile(r"^(hoy|ayer|ma[ñn]ana|esta\b|este\b|la semana|el (d[ií]a|mes)|"
                        r"mi[s]?\b|los [uú]ltimos)", re.I)


def mail_search_term(t):
    """Término a buscar en el historial de correo, o None si es consulta del día."""
    m = _MAILSEARCH_RE.search(t or "")
    if not m:
        return None
    term = m.group(1).strip(" ?¿!.,;:").strip()
    if not term or _DAY_TERMS.match(term):
        return None   # «correos de hoy» → resumen del día, no búsqueda
    return term


# ─── Búsqueda en la memoria del vault («¿qué sé de X?») ──────────────────────
_KNOW_RE = re.compile(
    r"(?:qu[eé] (?:sabes|s[eé]|sabemos|hay) (?:de|sobre)\s+|"
    r"qu[eé] tengo (?:apuntado|anotado|registrado|escrito) (?:de|sobre)\s+|"
    r"qu[eé] informaci[oó]n (?:tengo|hay|tenemos) (?:de|sobre)\s+|"
    r"busca en (?:mis|las) notas\s+(?:de|sobre)?\s*)"
    r"([\w\-áéíóúñü ]{2,60})", re.I)


def knowledge_term(t):
    m = _KNOW_RE.search(t or "")
    if not m:
        return None
    return m.group(1).strip(" ?¿!.,;:").strip() or None


# ─── Recordatorios de Mac (Reminders.app) ────────────────────────────────────
_REM_VERB = re.compile(r"\b(recu[eé]rda(?:me)?|recordatorio|recordar|"
                       r"an[oó]ta(?:me|lo|la)?|ap[uú]nta(?:me|lo|la)?)\b", re.I)
_REM_ADD = re.compile(r"\b(agr[eé]ga|a[ñn][aá]de|p[oó]ng?)\w{0,4}", re.I)
_REM_LIST_HINT = re.compile(r"\b(a (?:la |mi )?lista|en (?:la |mi )?lista|"
                            r"recordatorios?|a mis pendientes|al s[uú]per)\b", re.I)


def is_reminder_create(t):
    """Intención de CREAR un recordatorio: verbo de recordatorio, o verbo de
    'agregar' + señal de lista/recordatorio."""
    return bool(_REM_VERB.search(t) or (_REM_ADD.search(t) and _REM_LIST_HINT.search(t)))


def is_reminder_query(t):
    """Consulta sobre recordatorios/listas de Reminders.app."""
    return bool(re.search(r"\b(recordatorios?|reminders?|mis listas|"
                          r"qu[eé] tengo en (?:la |mi )?lista|"
                          r"qu[eé] hay en (?:la |mi )?lista|"
                          r"lista del? s[uú]per|compras del s[uú]per)\b", t, re.I))


def is_status_query(t):
    """Pedido de 'estatus/resumen del día': junta agenda + recordatorios + pendientes."""
    return bool(
        re.search(r"\b(est[aá]tus|status)\b", t, re.I)
        or re.search(r"res[uú]men.*(d[ií]a|hoy|jornada)", t, re.I)
        or re.search(r"c[oó]mo (va|viene|pinta|est[aá]).*(d[ií]a|todo|hoy|jornada)", t, re.I)
        or re.search(r"ponme al d[ií]a", t, re.I)
        or re.search(r"qu[eé] tengo (hoy|para hoy|en el d[ií]a)", t, re.I)
    )


# ─── Captura de notas de TEXTO al vault (segundo cerebro) ────────────────────
# Solo al INICIO del mensaje, para no chocar con recordatorios («apúntame X»
# sigue siendo recordatorio; «apunta esto: X» / «toma nota: X» es nota al vault).
_NOTE_PREFIX = re.compile(
    r"^\s*(?:nota[:,]\s*|toma(?:me)? nota(?:\s+de(?:\s+que)?)?[:,\s]+|"
    r"apunta esto[:,\s]*|guarda est[ao](?:\s+nota)?[:,\s]*)", re.I)


def is_note_capture(t):
    """Intención de archivar el mensaje como nota en Obsidian."""
    return bool(_NOTE_PREFIX.match(t or ""))


_REM_PREFIX = re.compile(
    r"^(?:por favor[,\s]*)?(?:recu[eé]rda(?:me)?|recordar|recordatorio(?:\s+de|\s+para)?|"
    r"an[oó]ta(?:me|lo|la)?|ap[uú]nta(?:me|lo|la)?|agr[eé]ga\w{0,4}|a[ñn][aá]de\w{0,4}|"
    r"p[oó]ng?\w{0,4})\s+(?:un recordatorio(?:\s+de|\s+para)?\s+|que\s+)?", re.I)
_DAY_WORD = re.compile(r"(pasado\s+ma[ñn]ana|ma[ñn]ana|hoy)", re.I)
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?|am|pm|hrs?|h)\b"
    r"|\b(\d{1,2}):(\d{2})\b", re.I)


def _strip_reminder_prefix(text):
    """Quita el verbo de recordatorio inicial para dejar el contenido como título."""
    return _REM_PREFIX.sub("", text.strip()).strip(" .,:;").strip()


def _parse_due(text):
    """Resuelve un vencimiento de forma determinista a partir de un día relativo
    (hoy/mañana/pasado mañana) + hora opcional. None si no hay día relativo."""
    dm = _DAY_WORD.search(text)
    if not dm:
        return None
    w = dm.group(1).lower()
    base = datetime.now().date()
    if "pasado" in w:
        d = base + timedelta(days=2)
    elif "hoy" in w:
        d = base
    else:
        d = base + timedelta(days=1)
    hh, mm = 9, 0
    tm = _TIME_RE.search(text)
    if tm and tm.group(1):
        hh, mm = int(tm.group(1)), int(tm.group(2) or 0)
        ap = (tm.group(3) or "").replace(".", "").replace(" ", "").lower()
        if ap.startswith("p") and hh < 12:
            hh += 12
        elif ap.startswith("a") and hh == 12:
            hh = 0
    elif tm:
        hh, mm = int(tm.group(4)), int(tm.group(5))
    return datetime(d.year, d.month, d.day, hh, mm)


def reminder_create_flow(text):
    """Crea un recordatorio en Reminders.app. El título es determinista (nunca
    falla aunque el LLM se equivoque); la fecha se resuelve por reglas + LLM."""
    today = datetime.now().date()
    ref = (f"hoy={today.isoformat()} ({_DOW_ES[today.weekday()]}), "
           f"mañana={(today + timedelta(days=1)).isoformat()}, "
           f"pasado mañana={(today + timedelta(days=2)).isoformat()}")
    sys_p = (
        "Extrae los datos para crear UN recordatorio en Reminders de Mac. "
        f"Referencia de fechas (zona America/Mexico_City): {ref}. "
        "IMPORTANTE: responde ÚNICAMENTE el JSON, empezando con '{' y terminando "
        "con '}', sin ningún texto antes ni después. Esquema exacto:\n"
        '{"title": "...", "list": "", "date": "YYYY-MM-DD" o null, '
        '"time": "HH:MM" o null}\n'
        "title = la tarea a recordar, breve, SIN el verbo (ej. «pagar la luz»). "
        "list SOLO si nombra una lista explícita; si no, \"\". date/time SOLO si hay "
        "vencimiento; si no, null. Hora 24h."
    )
    data = {}
    try:
        raw = _extract_complete([{"role": "system", "content": sys_p},
                              {"role": "user", "content": text}])
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            data = json.loads(m.group(0))
    except Exception:
        data = {}

    # Título: LLM si es razonable, si no, fallback determinista (nunca falla).
    title = (data.get("title") or "").strip()
    if not title or len(title) < 2:
        title = _strip_reminder_prefix(text)
    if not title:
        return "¿Qué quiere que le recuerde, Juanchi?"

    # Vencimiento: primero reglas deterministas; si no, lo que diga el LLM.
    due_dt = _parse_due(text)
    if due_dt is None and data.get("date"):
        try:
            due_dt = datetime.strptime(data["date"], "%Y-%m-%d")
            tm = data.get("time")
            if tm:
                hh, mm = (int(x) for x in str(tm).split(":")[:2])
                due_dt = due_dt.replace(hour=hh, minute=mm)
            else:
                due_dt = due_dt.replace(hour=9, minute=0)
        except Exception:
            due_dt = None

    # Lista: el LLM si coincide con una real; si no, busca una lista nombrada en
    # el texto (determinista); si no, "Actividades".
    known = serve_hud.reminder_lists()
    known_low = {n.lower(): n for n in known}
    llm_list = (data.get("list") or "").strip().lower()
    list_name = ""
    if llm_list and llm_list in known_low:
        list_name = known_low[llm_list]
    else:
        low = text.lower()
        for n in sorted(known, key=len, reverse=True):
            if n.lower() in low:
                list_name = n
                break
    if list_name:
        # quita la cola "a la lista del súper / al súper / a mi lista X" del título
        title = re.sub(
            r"\s*\b(?:a|en|al)\s+(?:la\s+|mi\s+)?(?:lista\b[\wáéíóúñ ]*|s[uú]per\b[\wáéíóúñ ]*)$",
            "", title, flags=re.I).strip(" .,:;") or title
    else:
        list_name = "Actividades"

    res = serve_hud.create_reminder(title, list_name=list_name, due_dt=due_dt)
    if not res.get("ok"):
        return (f"No pude crear el recordatorio «{title}», Juanchi. Reminders respondió: "
                f"{res.get('error', 'error desconocido')}.")

    when_txt = f"\n📅 vence {_fmt_when(due_dt)}" if due_dt else ""
    cm.log_event("accion", detalle=f"Recordatorio creado: «{title}» "
                 f"(lista {res.get('list', list_name)}"
                 + (f", vence {_fmt_when(due_dt)}" if due_dt else "") + ")")
    return (f"✅ Recordatorio creado, Juanchi:\n«{title}»\n"
            f"Lista: {res.get('list', list_name)}{when_txt}")


def _core_complete(msgs):
    """Una llamada simple al núcleo (chat) que devuelve el texto limpio."""
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False}).encode()
    req = urllib.request.Request(CORE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        j = json.load(r)
    txt = j["choices"][0]["message"]["content"]
    return re.sub(r"<think>[\s\S]*?</think>", "", txt).strip()


def _claude_complete(msgs, model=None, max_tokens=4096):
    """Responde con Claude API usando el mismo system+contexto+historial.
    Convierte el formato OpenAI (lista plana con 'system') al de Anthropic
    (system aparte, mensajes user/assistant). Lanza si falla — el llamador
    hace fail-open a Ollama."""
    client = _get_claude()
    system_parts, conv = [], []
    for m in msgs:
        role, content = m.get("role"), (m.get("content") or "")
        if role == "system":
            system_parts.append(content)
        elif role in ("user", "assistant"):
            conv.append({"role": role, "content": content})
    if not conv or conv[0]["role"] != "user":
        conv.insert(0, {"role": "user", "content": "(sin entrada)"})
    resp = client.messages.create(
        model=model or GOD_MODEL,
        max_tokens=max_tokens,
        system="\n\n".join(p for p in system_parts if p),
        messages=conv,
    )
    out = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return out.strip()


# --- Extracción estructurada (JSON) -------------------------------------------
# qwen local es POCO FIABLE para JSON (JARVIS.md §6.3): mete preámbulos, ignora
# fechas relativas. Para EXTRAER (recordatorio/evento/clasificar/destilar) se usa
# Claude Haiku (centavos por llamada) con fail-open al modelo local si no hay
# red/key. El chat general sigue 100% local ($0).
EXTRACT_MODEL = os.environ.get("JARVIS_EXTRACT_MODEL", "claude-haiku-4-5-20251001")


def _extract_complete(msgs):
    """LLM para extracción de datos: Haiku si está disponible; si no, el local.
    Los llamadores ya traen fallback determinista, así que esto puede fallar
    hacia qwen sin romper nada."""
    if _claude_available():
        try:
            return _claude_complete(msgs, model=EXTRACT_MODEL, max_tokens=1024)
        except Exception as e:
            print(f"[extract] Haiku falló ({str(e)[:80]}) — uso el local", flush=True)
    return _core_complete(msgs)


def _fmt_when(dt):
    """'hoy 10:00' / 'mañana 10:00' / 'vie 21 jun 10:00' — etiqueta legible en español."""
    delta = (dt.date() - datetime.now().date()).days
    if delta == 0:
        tag = "hoy"
    elif delta == 1:
        tag = "mañana"
    else:
        tag = f"{_DOW_ES[dt.weekday()][:3]} {dt.day} {_MON_ES[dt.month]}"
    return f"{tag} {dt.hour:02d}:{dt.minute:02d}"


def cal_create_flow(text):
    """Extrae los datos del evento (determinista, vía el LLM) y lo crea de verdad
    en el Calendario de Mac. Confirma solo con los datos REALES del evento creado.
    Si falta fecha u hora, pregunta en vez de inventar."""
    today = datetime.now().date()
    ref = (f"hoy={today.isoformat()} ({_DOW_ES[today.weekday()]}), "
           f"mañana={(today + timedelta(days=1)).isoformat()}, "
           f"pasado mañana={(today + timedelta(days=2)).isoformat()}")
    sys_p = (
        "Eres un extractor de datos para crear UN evento de calendario. "
        f"Referencia de fechas (zona America/Mexico_City): {ref}. "
        "Resuelve cualquier fecha relativa a fecha absoluta. Responde SOLO con JSON "
        "válido (sin texto extra, sin markdown, sin ```), con este esquema exacto:\n"
        '{"title": "...", "date": "YYYY-MM-DD" o null, "start": "HH:MM" o null, '
        '"end": "HH:MM" o null, "location": "", "notes": ""}\n'
        "Hora en formato 24h. Si falta el título, infiérelo breve. Si no hay fecha u "
        "hora de inicio, pon null en ese campo. Si no hay hora de fin, pon null."
    )
    try:
        raw = _extract_complete([{"role": "system", "content": sys_p},
                              {"role": "user", "content": text}])
    except Exception as e:
        return f"No pude contactar el núcleo para procesar la cita ({e}), Juanchi."

    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return ("No me quedaron claros los datos de la cita, Juanchi. "
                "¿Me lo dice completo? (ej. «agéndame junta con Pedro mañana 10:00»)")
    try:
        data = json.loads(m.group(0))
    except Exception:
        return ("No me quedaron claros los datos de la cita, Juanchi. "
                "¿Me lo dice completo? (ej. «agéndame junta con Pedro mañana 10:00»)")

    title = (data.get("title") or "").strip() or "Evento"
    d, st = data.get("date"), data.get("start")
    if not d or not st:
        falta = "la fecha" if not d else "la hora"
        return (f"Con gusto agendo «{title}», Juanchi, pero me falta {falta}. "
                "¿Me lo indica en un mensaje? (ej. «agéndame X mañana 10:00»)")
    try:
        sh, sm = (int(x) for x in str(st).split(":")[:2])
        start_dt = datetime.strptime(d, "%Y-%m-%d").replace(hour=sh, minute=sm)
    except Exception:
        return ("La fecha u hora no quedaron bien, Juanchi. "
                "¿Me las repite? (ej. «mañana 10:00»)")

    end_dt = None
    if data.get("end"):
        try:
            eh, em = (int(x) for x in str(data["end"]).split(":")[:2])
            cand = start_dt.replace(hour=eh, minute=em)
            end_dt = cand if cand > start_dt else None
        except Exception:
            end_dt = None

    res = serve_hud.create_calendar_event(
        title, start_dt, end_dt=end_dt,
        location=(data.get("location") or ""), notes=(data.get("notes") or ""))
    if not res.get("ok"):
        return (f"No pude agendar «{title}», Juanchi. Calendar.app respondió: "
                f"{res.get('error', 'error desconocido')}.")

    fin = f"–{end_dt.hour:02d}:{end_dt.minute:02d}" if end_dt else ""
    loc = res.get("calendar", "Calendario")
    cm.log_event("accion", detalle=f"Evento agendado: «{title}» "
                 f"{_fmt_when(start_dt)}{fin} ({loc})")
    return (f"✅ Agendado, Juanchi:\n«{title}»\n{_fmt_when(start_dt)}{fin}\n"
            f"{loc} (iCloud). Ya está en su Mac.")


PERSONAL_ROOT = os.environ.get("JARVIS_PERSONAL_ROOT", "Personal")

_NUM_HINT = ("\nMuestra los pendientes numerados EXACTAMENTE con estos números "
             "(no los reordenes ni renumeres). El usuario puede cerrarlos "
             "diciendo «cierra 2 y 5» o «ya hice la de …».")


def _work_group(rel_dir_parts):
    """Agrupa un pendiente de trabajo por cliente/proyecto: el segmento
    después de 'Clientes' si existe; si no, la primera subcarpeta."""
    parts = list(rel_dir_parts)
    if "Clientes" in parts:
        i = parts.index("Clientes")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[0] if parts else "(raíz)"


def _work_roots(data):
    """Raíces del vault que NO son personales y tienen pendientes abiertos."""
    return [p["name"] for p in data["projects"]
            if p["open"] and p["name"] != "(raíz)"
            and p["name"].lower() != PERSONAL_ROOT.lower()]


def _grouped_work(data):
    """{raíz: {grupo: [tareas]}} de todas las ramas de trabajo."""
    out = {}
    for root in _work_roots(data):
        d = serve_hud.project_tasks(root, limit=500)
        groups = {}
        for t in d["tasks"]:
            rel = Path(t["path"]).relative_to(serve_hud.VAULT / root)
            groups.setdefault(_work_group(rel.parts[:-1]), []).append(t)
        out[root] = groups
    return out


def _work_summary_lines(data, grouped):
    """Líneas de resumen por raíz de trabajo: total, vencidos y conteo por cliente."""
    lines = []
    for root, groups in grouped.items():
        total = sum(len(ts) for ts in groups.values())
        vencidas = sum(1 for ts in groups.values() for t in ts
                       if t["due"] and t["due"] < data["today"])
        gtxt = " · ".join(f"{g} {len(ts)}" for g, ts in
                          sorted(groups.items(), key=lambda x: -len(x[1]))[:6])
        lines.append(f"🏢 {root}: {total} abiertos"
                     + (f" ({vencidas} vencidos)" if vencidas else "")
                     + (f" — {gtxt}" if gtxt else ""))
    return lines


def _numbered(shown, with_due_flag=None):
    return "\n".join(
        f"{i}. {t['text']}"
        + (f" (📅 {t['due']}{' VENCIDA' if with_due_flag and t['due'] < with_due_flag else ''})"
           if t["due"] else "")
        for i, t in enumerate(shown, start=1))


def task_context(text, chat_id=None):
    """Datos reales de pendientes (determinista, sin alucinar).

    Default: PERSONALES en detalle numerado + trabajo (Nexia, etc.) en
    resumen por cliente. «pendientes de nexia/mapartel» → esa rama en
    detalle. «todos» → todo mezclado. La lista numerada se guarda en
    LAST_TASKS[chat_id] para que «cierra 2 y 5» sea determinista.
    """
    try:
        low = (text or "").lower()
        data = serve_hud.scan_tasks()

        # 1) raíz explícita («pendientes de nexia», «personales»)
        for p in data["projects"]:
            name = p["name"]
            if name != "(raíz)" and name.lower() in low:
                d = serve_hud.project_tasks(name, limit=500)
                shown = d["tasks"]
                if chat_id is not None:
                    LAST_TASKS[chat_id] = list(shown)
                return (f"DATOS REALES de Obsidian — pendientes ABIERTOS de {d['project']} "
                        f"({d['open']} total):\n" + _numbered(shown, data["today"])
                        + _NUM_HINT)

        grouped = _grouped_work(data)

        # 2) cliente/proyecto explícito («pendientes de mapartel»)
        for root, groups in grouped.items():
            for g, tasks in groups.items():
                if len(g) >= 4 and g.lower() in low:
                    if chat_id is not None:
                        LAST_TASKS[chat_id] = list(tasks)
                    return (f"DATOS REALES de Obsidian — pendientes ABIERTOS de {g} "
                            f"({root}, {len(tasks)} total):\n"
                            + _numbered(tasks, data["today"]) + _NUM_HINT)

        # 3) «todos» → vista completa mezclada
        if re.search(r"\btod[oa]s\b", low):
            shown, seen = [], set()
            for t in data["overdue"] + data["due_today"] + data["recent"]:
                key = (t["path"], t["line"])
                if key not in seen:
                    seen.add(key)
                    shown.append(t)
            if chat_id is not None:
                LAST_TASKS[chat_id] = list(shown)
            s = f"DATOS REALES de Obsidian (hoy {data['today']}):\n"
            s += f"Total pendientes: {data['total_open']}\n"
            s += "Pendientes (vencidas primero):\n" + "\n".join(
                f"{i}. [{t['project']}] {t['text']}"
                + (f" (📅 {t['due']})" if t["due"] else "")
                for i, t in enumerate(shown, start=1))
            return s + _NUM_HINT

        # 4) default: personal en detalle + trabajo resumido
        d = serve_hud.project_tasks(PERSONAL_ROOT, limit=500)
        shown = d["tasks"]
        if chat_id is not None:
            LAST_TASKS[chat_id] = list(shown)
        s = f"DATOS REALES de Obsidian (hoy {data['today']}):\n"
        s += f"PENDIENTES PERSONALES ({d['open']} abiertos):\n"
        s += _numbered(shown, data["today"]) or "(ninguno)"
        wl = _work_summary_lines(data, grouped)
        if wl:
            s += ("\n\nRESUMEN DE TRABAJO (menciónalo tal cual en 1-2 líneas al "
                  "final, SIN detallar tareas; si quiere el detalle que pida "
                  "«pendientes de Nexia» o del cliente):\n" + "\n".join(wl))
        return s + _NUM_HINT
    except Exception:
        return None


# --------- Cierre de pendientes por chat («cierra 2, 3 y 5») -------------

_CLOSE_VERB = re.compile(
    r"\b(cierra\w*|cierre\w*|cerrar|cerrad[oa]s?|palome\w+|complet[eé]|"
    r"completad[oa]s?|termin[eé]|terminad[oa]s?|ya\s+hice|ya\s+la[s]?\s+hice|"
    r"marca\w*\s+como\s+hech[oa]s?|ya\s+(?:está|están|quedó|quedaron)\s+"
    r"(?:hech[oa]s?|list[oa]s?))\b", re.I)
_TASK_WORD = re.compile(r"\b(tareas?|pendientes?|actividad(?:es)?|puntos?|notas?)\b", re.I)
_RANGE_RE = re.compile(r"\b(\d{1,3})\s*(?:al?|hasta)\s+(?:la\s+|el\s+)?(\d{1,3})\b", re.I)


def _strip_accents(s):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn").lower()


def _norm_title(s):
    """Normaliza para comparar: sin acentos/markdown, sin puntuación final."""
    s = _strip_accents(s or "").replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip().rstrip(".,;:!?")


_STOP_WORDS = {"la", "el", "los", "las", "de", "del", "en", "al", "a", "y", "o",
               "un", "una", "que", "para", "por", "con", "este", "esta", "ya",
               "mi", "su", "se", "le", "lo", "es", "the", "and", "to", "in"}


def _sig_words(s):
    return [w for w in re.findall(r"[a-z0-9]+", _norm_title(s))
            if w not in _STOP_WORDS]


def _word_match(a, b):
    """Palabras iguales, o que comparten prefijo de 4+ letras (llamar≈llamarle)."""
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a.startswith(b[:4]) and b.startswith(a[:4]))


def _fuzzy_title_hits(text, shown):
    """Índices (1-based) de tareas cuyo título coincide con el mensaje por
    palabras significativas (≥60% del título presente, mínimo según largo)."""
    words = _sig_words(text)
    out = []
    for i, t in enumerate(shown or [], start=1):
        if not t:
            continue
        title = _sig_words(t["text"])
        if not title:
            continue
        hits = sum(1 for w in title if any(_word_match(w, m) for m in words))
        need = 1 if len(title) == 1 else max(2, int(len(title) * 0.6))
        if hits >= need:
            out.append(i)
    return out


def is_close_tasks(text, chat_id=None):
    """Intención de cerrar pendientes: verbo de cierre + (números, palabra de
    tarea, o el título de una tarea de la última lista mostrada)."""
    if not _CLOSE_VERB.search(text or ""):
        return False
    if re.search(r"\b\d{1,3}\b", text) or _TASK_WORD.search(text):
        return True
    return bool(_fuzzy_title_hits(text, LAST_TASKS.get(chat_id)))


def close_tasks_flow(chat_id, text):
    shown = LAST_TASKS.get(chat_id)
    if not shown:
        return ("Uy Juanchi, no tengo fresca la lista. Pídame primero los "
                "pendientes («¿qué tengo pendiente?») y ahí sí me dice cuáles "
                "cierro por número.")
    # 1) números explícitos (con rangos «2 al 5»)
    nums = set()
    for a, b in _RANGE_RE.findall(text):
        nums.update(range(int(a), int(b) + 1))
    nums.update(int(n) for n in re.findall(r"\b(\d{1,3})\b", text))
    picks = sorted(n for n in nums if 1 <= n <= len(shown))
    # 2) «todas»
    if not picks and re.search(r"\btod[oa]s\b", text, re.I):
        picks = [i for i, t in enumerate(shown, start=1) if t]
    # 3) por texto («ya hice la de pagar la renta»)
    if not picks:
        picks = _fuzzy_title_hits(text, shown)
        if len(picks) > 1:
            opciones = "\n".join(f"{n}. {shown[n-1]['text']}" for n in picks)
            return ("Me suenan varias, Juanchi — ¿cuál(es) cierro? Dígame por "
                    f"número:\n{opciones}")
    if not picks:
        return ("No identifiqué cuáles cerrar, Juanchi. Dígame los números de la "
                "lista (ej. «cierra 2, 3 y 5») o «todas».")

    items, already, out_sel = [], [], []
    for n in picks:
        t = shown[n - 1]
        if t is None:
            already.append(n)
        else:
            items.append((n, t))
    res = serve_hud.close_tasks_bulk(
        [{"path": t["path"], "line": t["line"], "text": t["text"]} for _, t in items]
    ) if items else {"closed": 0, "failed": []}
    failed_keys = {(f.get("path"), f.get("line")) for f in res.get("failed", [])}
    ok_lines, fail_lines = [], []
    for n, t in items:
        if (t["path"], t["line"]) in failed_keys:
            err = next((f.get("error", "") for f in res.get("failed", [])
                        if (f.get("path"), f.get("line")) == (t["path"], t["line"])), "")
            fail_lines.append(f"✗ {n}. {t['text']} ({err})")
        else:
            ok_lines.append(f"✓ {n}. {t['text']}")
            shown[n - 1] = None   # numeración estable para cierres siguientes
    parts = []
    if ok_lines:
        parts.append("Listo Juanchi, cerradas de una:\n" + "\n".join(ok_lines))
        cm.log_event("accion", detalle=f"Pendientes cerrados via bot: "
                     + "; ".join(l[2:] for l in ok_lines))
    if already:
        parts.append("Ya estaban cerradas: " + ", ".join(str(n) for n in already))
    if fail_lines:
        parts.append("Estas no pude (la nota cambió):\n" + "\n".join(fail_lines))
    quedan = sum(1 for t in shown if t)
    parts.append(f"Le quedan {quedan} de esa lista." if quedan
                 else "¡Esa lista quedó limpia, parce! 🎉")
    return "\n\n".join(parts)


def ask_core(chat_id, text):
    """Responde y deja registro persistente del intercambio (memoria de Jarvis)."""
    reply = _ask_core_inner(chat_id, text)
    cm.log_exchange(chat_id, text, reply,
                    mode="dios" if GOD_MODE.get(chat_id) else "local")
    return reply


def _ask_core_inner(chat_id, text):
    # Acciones reales (escritura). El recordatorio se evalúa ANTES que el evento:
    # un "ponme un recordatorio a las 5" no debe acabar como evento de calendario.
    if is_reminder_create(text):
        return reminder_create_flow(text)
    if is_cal_create(text):
        return cal_create_flow(text)
    if is_close_tasks(text, chat_id):
        return close_tasks_flow(chat_id, text)
    # historial: solo turnos user/assistant (el contexto inyectado es por-llamada)
    hist = histories.setdefault(chat_id, [])
    # Un "estatus del día" jala TODO lo conectado (agenda + recordatorios + pendientes).
    status = is_status_query(text)
    msgs = [{"role": "system", "content": SYSTEM}]
    if status:
        msgs.append({"role": "system", "content":
            "El usuario pide un ESTATUS DEL DÍA. Con los DATOS REALES que siguen "
            "(agenda, recordatorios y pendientes), arma un resumen breve y organizado "
            "en secciones (📅 Agenda · ✅ Pendientes · 🔔 Recordatorios). Usa solo los "
            "datos provistos; si una sección viene vacía, dilo en una línea. Si viene "
            "un PERFIL, ordena por lo que más acerca a esas metas."})
        try:
            prof = ob.profile_context()
            if prof:
                msgs.append({"role": "system", "content": prof})
        except Exception:
            pass
    if is_task_query(text) or status:
        ctx = task_context(text, chat_id)
        if ctx:
            msgs.append({"role": "system", "content": ctx})
    try:
        ent = serve_hud.entity_context(text)   # MOC + notas recientes del proyecto/cliente
        if ent:
            msgs.append({"role": "system", "content": ent})
    except Exception:
        pass
    if is_cal_query(text) or status:
        try:
            cal = serve_hud.calendar_context(days=14)   # agenda del Calendario de Mac
            if cal:
                msgs.append({"role": "system", "content": cal})
        except Exception:
            pass
    mterm = mail_search_term(text)
    if mterm:
        try:
            ms = serve_hud.mail_search_context(mterm)   # historial + cuerpos
            if ms:
                msgs.append({"role": "system", "content": ms})
        except Exception:
            pass
    elif is_mail_query(text):
        try:
            mail = serve_hud.mail_context()   # correos de hoy en Mail.app
            if mail:
                msgs.append({"role": "system", "content": mail})
        except Exception:
            pass
    kterm = knowledge_term(text)
    if kterm:
        try:
            kc = serve_hud.knowledge_context(kterm)   # FTS sobre memory.db
            if kc:
                msgs.append({"role": "system", "content": kc})
        except Exception:
            pass
    if is_reminder_query(text) or status:
        try:
            rem = serve_hud.reminders_context()   # recordatorios de Reminders.app
            if rem:
                msgs.append({"role": "system", "content": rem})
        except Exception:
            pass
    msgs += hist[-8:]
    msgs.append({"role": "user", "content": text})
    try:
        if GOD_MODE.get(chat_id):
            # Modo dios: Claude. Si falla (red/cuota), fail-open al núcleo local.
            try:
                reply = _claude_complete(msgs)
            except Exception as e:
                reply = (f"🔻 (Modo dios no respondió: {str(e)[:80]} — uso el local)\n\n"
                         + _core_complete(msgs))
        else:
            reply = _core_complete(msgs)
        reply = reply or "(sin respuesta)"
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": reply})
        del hist[:-8]
        return reply
    except Exception as e:
        return (f"No pude contactar el núcleo local ({e}). "
                "¿La Mac está encendida con Jarvis corriendo?")


# ─── Handlers ───────────────────────────────────────────────────────────────
async def gate(update: Update) -> bool:
    cid = str(update.effective_chat.id)
    if cid in allowed_ids():
        return True
    name = update.effective_user.first_name if update.effective_user else "?"
    await update.message.reply_text(
        f"🔒 Acceso no autorizado.\nTu chat ID es: `{cid}`\n"
        f"Pídele a quien configura Jarvis que agregue este ID.",
        parse_mode="Markdown")
    print(f"[ACCESO DENEGADO] {name} — chat_id={cid}", flush=True)
    return False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    extra = ("" if ob.profile_exists() else
             "\n\n🧠 Aún no tengo su perfil: corra /onboarding (6 preguntas) "
             "para armar el núcleo de su segundo cerebro.")
    await update.message.reply_text(
        "👋 ¿Qué más, Juanchi? Aquí Jarvis, a la orden.\n"
        "Escríbame lo que necesite. Pregúnteme por sus pendientes "
        "(ej. «¿qué tengo en Nexia?») o cualquier cosa de sus notas.\n"
        "Use /voz para activar o silenciar mis respuestas habladas." + extra)


async def cmd_voz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Activa/desactiva las respuestas habladas. Acepta /voz on | off."""
    if not await gate(update):
        return
    chat_id = update.effective_chat.id
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("on", "si", "sí", "1"):
        VOICE_REPLY[chat_id] = True
    elif arg in ("off", "no", "0"):
        VOICE_REPLY[chat_id] = False
    else:
        VOICE_REPLY[chat_id] = not _tts_voice_enabled(chat_id)
    estado = "activada 🔊" if _tts_voice_enabled(chat_id) else "silenciada 🔇"
    await update.message.reply_text(f"Voz {estado}, Juanchi.")


async def cmd_dios(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Activa el 'modo dios' (Claude) en este chat. Acepta /dios on | off."""
    if not await gate(update):
        return
    chat_id = update.effective_chat.id
    if not _claude_available():
        await update.message.reply_text(
            "No tengo configurado Claude, Juanchi: falta ANTHROPIC_API_KEY "
            "(o el SDK 'anthropic') en el entorno del bot.")
        return
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("off", "no", "0", "normal"):
        GOD_MODE[chat_id] = False
    elif arg in ("on", "si", "sí", "1"):
        GOD_MODE[chat_id] = True
    else:
        GOD_MODE[chat_id] = not GOD_MODE.get(chat_id)
    if GOD_MODE[chat_id]:
        await update.message.reply_text(
            f"🧠 Modo dios ACTIVADO, Juanchi — razono con {GOD_MODEL}. "
            "Use /normal para volver al modelo local.")
    else:
        await update.message.reply_text(
            "🔌 Modo dios desactivado. Vuelvo al modelo local (Ollama), Juanchi.")


async def cmd_normal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Vuelve al modelo local (Ollama) en este chat."""
    if not await gate(update):
        return
    GOD_MODE[update.effective_chat.id] = False
    await update.message.reply_text(
        "🔌 Modo local (Ollama) activo, Juanchi. Use /dios para el modo poderoso.")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Captura excepciones para que un fallo de red o de un handler NO tumbe el bot.
    Los errores transitorios de red se ignoran (PTB reintenta el polling solo)."""
    err = ctx.error
    if isinstance(err, (NetworkError, TimedOut)):
        print(f"[net] error de red transitorio (ignorado): {err}", flush=True)
        return
    print(f"[error] excepción no manejada: {err!r}", flush=True)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    chat_id = update.effective_chat.id

    # ¿entrevista de onboarding en curso? → la respuesta alimenta el perfil
    if chat_id in ONBOARD:
        await _ob_advance(chat_id, ctx.bot, update.message.text.strip())
        return

    # ¿estábamos esperando el nombre de la persona para una nota de voz?
    if chat_id in PENDING_NAME:
        token = PENDING_NAME.pop(chat_id)
        state = PENDING.get(token)
        if state:
            state["person"] = update.message.text.strip()
            await _do_file_and_ask_delete(ctx.bot, state, token)
            return

    # captura explícita de nota de texto («toma nota: …», «apunta esto: …»)
    if is_note_capture(update.message.text):
        await capture_note_flow(update, ctx,
                                _NOTE_PREFIX.sub("", update.message.text).strip())
        return

    await ctx.bot.send_chat_action(chat_id, "typing")
    reply = await asyncio.to_thread(ask_core, update.effective_chat.id,
                                    update.message.text)
    await reply_with_voice(update, chat_id, reply)


# ============================ NOTAS DE VOZ ====================================

_TRANSCRIBE_CLI = str(Path(__file__).resolve().parent / "transcribe_cli.py")


async def transcribe_audio(path):
    """Transcribe en un SUBPROCESO (mlx en su propio hilo principal — evita el
    cuelgue de Metal dentro de asyncio.to_thread). Devuelve el texto o lanza."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, _TRANSCRIBE_CLI, str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    raw = out.decode("utf-8", "replace")
    i = raw.find("@@RESULT@@")
    if i < 0:
        tail = (err.decode("utf-8", "replace") or raw)[-400:]
        raise RuntimeError(f"transcripción falló (rc={proc.returncode}): {tail}")
    data = json.loads(raw[i + len("@@RESULT@@"):])
    if data.get("error"):
        raise RuntimeError(data["error"])
    return (data.get("text") or "").strip()

def _preview(txt, n=600):
    t = txt.strip().replace("\n", " ")
    return t if len(t) <= n else t[:n] + "…"


def _card_markup(token, state):
    """Tarjeta inicial: confirmar la propuesta, cambiar área, u otra persona."""
    area_label = vn.AREAS[state["area"]]["label"]
    who = state["person"] or ("Sesión" if vn.AREAS[state["area"]].get("sesiones")
                              else "Inbox")
    rows = [[InlineKeyboardButton(f"✅ {area_label} · {who}", callback_data=f"vn|ok|{token}")]]
    rows.append([InlineKeyboardButton(vn.AREAS[k]["label"], callback_data=f"vn|area|{k}|{token}")
                 for k in ("mapartel", "nexia")])
    rows.append([InlineKeyboardButton(vn.AREAS[k]["label"], callback_data=f"vn|area|{k}|{token}")
                 for k in ("personal", "villacatania")])
    rows.append([InlineKeyboardButton("👤 Otra persona", callback_data=f"vn|name|{token}")])
    return InlineKeyboardMarkup(rows)


def _people_markup(token, state):
    """Tras elegir área: candidatos de esa área + Inbox + otra persona."""
    rows = []
    cands = state.get("candidates", [])
    for i in range(0, len(cands), 2):
        rows.append([InlineKeyboardButton(c, callback_data=f"vn|pick|{j}|{token}")
                     for j, c in enumerate(cands[i:i+2], start=i)])
    inbox_label = ("🎙️ Sesión (Crudas)" if vn.AREAS[state["area"]].get("sesiones")
                   else "📥 Inbox del área")
    rows.append([InlineKeyboardButton(inbox_label, callback_data=f"vn|inbox|{token}"),
                 InlineKeyboardButton("👤 Otra persona", callback_data=f"vn|name|{token}")])
    return InlineKeyboardMarkup(rows)


def _del_markup(token):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑️ Borrar audio", callback_data=f"vn|del|yes|{token}"),
        InlineKeyboardButton("💾 Conservar", callback_data=f"vn|del|no|{token}")]])


def _card_text(state):
    area_label = vn.AREAS[state["area"]]["label"]
    who = state["person"] or ("Sesión (Crudas)"
                              if vn.AREAS[state["area"]].get("sesiones")
                              else "Inbox del área")
    conf = {"high": "alta", "medium": "media", "low": "baja"}.get(state.get("confidence"), "—")
    head = f"🎙️ *Nota transcrita* · confianza {conf}\n\n{_preview(state['text'])}\n\n"
    return head + f"📂 Propuesta: *{area_label} · {who}*\n¿Dónde la archivo?"


async def _send_card(bot, chat_id, state):
    token = uuid.uuid4().hex[:10]
    PENDING[token] = state
    await bot.send_message(chat_id, _card_text(state),
                           reply_markup=_card_markup(token, state),
                           parse_mode="Markdown")
    return token


async def _do_file_and_ask_delete(query_or_bot, state, token):
    """Archiva el texto en el MD y pregunta si borrar el audio (si lo hay)."""
    res = await asyncio.to_thread(
        vn.file_note, state["area"], state["person"], state["text"],
        datetime.now(), state.get("source", "voz"), state.get("resumen", ""),
        state.get("icon", "🎙️"))
    if not res.get("ok"):
        msg = f"⚠️ No pude archivar: {res.get('error')}"
        await _edit_or_send(query_or_bot, state, msg, None)
        return
    state["filed"] = res
    cm.log_event("nota", chat_id=state.get("chat_id"),
                 area=res["area_label"], title=res["title"],
                 source=state.get("source", ""), resumen=state.get("resumen", ""))
    accion = "creé" if res["nuevo"] else "actualicé"
    if not state.get("audio"):
        # nota de texto: no hay audio que borrar → confirmación final directa
        txt = (f"✅ Listo, Juanchi. {accion.capitalize()} *{res['title']}* "
               f"en _{res['area_label']}_.\n`{res['rel']}`")
        await _edit_or_send(query_or_bot, state, txt, None)
        PENDING.pop(token, None)
        return
    txt = (f"✅ Listo, Juanchi. {accion.capitalize()} *{res['title']}* "
           f"en _{res['area_label']}_.\n`{res['rel']}`\n\n¿Borro la nota de voz?")
    await _edit_or_send(query_or_bot, state, txt, _del_markup(token))


async def _edit_or_send(query_or_bot, state, text, markup):
    """Edita el mensaje de la tarjeta si vino de un botón; si no, manda nuevo."""
    is_query = hasattr(query_or_bot, "data")  # CallbackQuery tiene .data; Bot no
    if is_query:
        try:
            await query_or_bot.edit_message_text(text, reply_markup=markup,
                                                 parse_mode="Markdown")
            return
        except Exception:
            pass
    bot = query_or_bot if not is_query else query_or_bot.get_bot()
    await bot.send_message(state["chat_id"], text, reply_markup=markup,
                           parse_mode="Markdown")


# ============================ ONBOARDING =====================================
ONBOARD = {}   # chat_id -> {"i": paso actual, "data": {clave: respuesta}}

TOUR = (
    "🧭 *Su segundo cerebro quedó armado, Juanchi. Así se usa:*\n\n"
    "*Capturar*\n"
    "· Nota de voz (Telegram o Memos de Apple) → la transcribo y archivo\n"
    "· «toma nota: …» o /nota → nota de texto al vault\n"
    "· «recuérdame …» → Recordatorios · «agéndame …» → Calendario\n\n"
    "*Consultar*\n"
    "· «¿qué tengo hoy?» / «estatus del día»\n"
    "· «¿qué sabes de X?» → busco en sus notas\n"
    "· «¿qué me ha escrito X?» → busco en su correo\n"
    "· «¿qué tengo en Nexia?» → pendientes por proyecto\n\n"
    "*Automático*\n"
    "· ☀️ 7:00 brief de la mañana · 🌙 21:00 cierre + diario en Obsidian\n"
    "· Todo lo que hablamos queda en su diario (`/diario` lo genera ya)\n"
    "· /brief a demanda · /dios modo Claude · /voz respuestas habladas\n\n"
    "Su perfil vive en `Personal/Segundo Cerebro/` y guía mis prioridades. "
    "Re-corra /onboarding cuando cambien sus metas."
)


def _ob_markup(final=False):
    if final:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Saltar pregunta", callback_data="ob|skip"),
        InlineKeyboardButton("✖️ Cancelar", callback_data="ob|cancel")]])


async def cmd_onboarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Arranca (o reinicia) la entrevista del segundo cerebro."""
    if not await gate(update):
        return
    chat_id = update.effective_chat.id
    ONBOARD[chat_id] = {"i": 0, "data": {}}
    ya = ("\n_(Ya tiene un perfil — sus respuestas nuevas lo actualizan y el "
          "anterior queda en el historial.)_" if ob.profile_exists() else "")
    await update.message.reply_text(
        "🧠 *Onboarding del segundo cerebro*\n"
        "Le haré 6 preguntas, Juanchi. Con sus respuestas construyo su perfil en "
        "Obsidian y lo uso para priorizar briefs, pendientes y correo. "
        "Conteste con texto libre; puede saltar cualquiera." + ya,
        parse_mode="Markdown")
    await update.message.reply_text(ob.STEPS[0]["q"], reply_markup=_ob_markup())


async def _ob_advance(chat_id, bot, answer):
    """Registra la respuesta del paso actual y manda el siguiente (o cierra)."""
    st = ONBOARD.get(chat_id)
    if not st:
        return
    step = ob.STEPS[st["i"]]
    if answer is not None:
        st["data"][step["key"]] = answer
    st["i"] += 1
    if st["i"] < len(ob.STEPS):
        await bot.send_message(chat_id, ob.STEPS[st["i"]]["q"],
                               reply_markup=_ob_markup())
        return
    # terminó: escribir perfil + tour
    ONBOARD.pop(chat_id, None)
    res = await asyncio.to_thread(ob.write_profile, st["data"])
    if not res.get("ok"):
        await bot.send_message(
            chat_id, f"⚠️ No pude escribir el perfil: {res.get('error')}")
        return
    cm.log_event("accion", chat_id=chat_id,
                 detalle=f"Onboarding completado → {res['rel']}")
    contestadas = sum(1 for v in st["data"].values() if (v or "").strip())
    await bot.send_message(
        chat_id,
        f"✅ Perfil guardado ({contestadas}/6 respondidas):\n`{res['rel']}`\n"
        "Desde ahora priorizo sus briefs y el estatus con ese lente.",
        parse_mode="Markdown")
    await bot.send_message(chat_id, TOUR, parse_mode="Markdown")


async def on_ob_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = update.effective_chat.id
    verb = q.data.split("|")[1]
    if chat_id not in ONBOARD:
        await q.edit_message_reply_markup(None)
        return
    if verb == "cancel":
        ONBOARD.pop(chat_id, None)
        await q.edit_message_text("Onboarding cancelado, Juanchi. "
                                  "Retómelo cuando guste con /onboarding.")
        return
    if verb == "skip":
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        await _ob_advance(chat_id, ctx.bot, None)


async def capture_note_flow(update, ctx, content):
    """Archiva una nota de TEXTO en el vault con la misma tarjeta de botones
    que las notas de voz (clasificación área/persona, sin paso de audio)."""
    chat_id = update.effective_chat.id
    if not content or len(content) < 3:
        await update.message.reply_text(
            "📝 ¿Qué anoto, Juanchi? Mándeme «toma nota: …» con el contenido.")
        return
    await ctx.bot.send_chat_action(chat_id, "typing")
    cls = await asyncio.to_thread(vn.classify, content, _extract_complete)
    state = {"text": content, "area": cls["area"], "person": cls["person"],
             "resumen": cls["resumen"], "confidence": cls["confidence"],
             "candidates": cls["candidates"], "audio": None,
             "tg_voice_msg_id": None, "source": "Telegram texto",
             "icon": "📝", "chat_id": chat_id}
    await _send_card(ctx.bot, chat_id, state)


async def cmd_nota(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/nota <texto> — archiva el texto como nota en Obsidian."""
    if not await gate(update):
        return
    await capture_note_flow(update, ctx, " ".join(ctx.args).strip() if ctx.args else "")


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, "typing")
    msg = await update.message.reply_text("🎙️ Ya te escucho, Juanchi…")

    VOICE_TMP.mkdir(parents=True, exist_ok=True)
    voice = update.message.voice or update.message.audio
    ogg = VOICE_TMP / f"{update.message.message_id}.ogg"
    try:
        f = await ctx.bot.get_file(voice.file_id)
        await f.download_to_drive(str(ogg))
        text = await transcribe_audio(ogg)
    except Exception as e:
        await msg.edit_text(f"⚠️ No pude transcribir el audio: {e}")
        return

    if not text:
        await msg.edit_text("🤔 No alcancé a entender nada del audio, Juanchi.")
        return

    # «toma nota…» dictado → flujo de notas de siempre (archiva audio + nota)
    if is_note_capture(text):
        note = _NOTE_PREFIX.sub("", text).strip() or text
        cls = await asyncio.to_thread(vn.classify, note, _extract_complete)
        state = {"text": note, "area": cls["area"], "person": cls["person"],
                 "resumen": cls["resumen"], "confidence": cls["confidence"],
                 "candidates": cls["candidates"], "audio": str(ogg),
                 "tg_voice_msg_id": update.message.message_id,
                 "source": "Telegram", "chat_id": chat_id}
        await msg.delete()
        await _send_card(ctx.bot, chat_id, state)
        return

    # Conversación: el audio es un mensaje más para Jarvis
    # (mismo pipeline que texto: recordatorios, estatus, correo, ask_core…)
    await msg.edit_text(f"🗣️ «{text}»")
    await ctx.bot.send_chat_action(chat_id, "typing")
    reply = await asyncio.to_thread(ask_core, chat_id, text)
    await reply_with_voice(update, chat_id, reply)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    verb = parts[1]
    token = parts[-1]
    state = PENDING.get(token)
    if not state:
        await q.edit_message_text("⌛ Esa nota ya expiró, Juanchi. Reenvíela, por favor.")
        return

    if verb == "ok":
        await _do_file_and_ask_delete(q, state, token)

    elif verb == "area":
        key = parts[2]
        state["area"] = key
        reg = await asyncio.to_thread(vn.people_registry)
        state["candidates"] = sorted(e["name"] for e in reg.values() if e["area"] == key)[:6]
        # si la persona actual no pertenece al área, resetear
        if state["person"] and _slug(state["person"]) not in {_slug(c) for c in state["candidates"]}:
            state["person"] = None
        await q.edit_message_text(
            f"📂 *{vn.AREAS[key]['label']}* — ¿de quién es esta nota?",
            reply_markup=_people_markup(token, state), parse_mode="Markdown")

    elif verb == "pick":
        idx = int(parts[2])
        cands = state.get("candidates", [])
        if 0 <= idx < len(cands):
            state["person"] = cands[idx]
        await _do_file_and_ask_delete(q, state, token)

    elif verb == "inbox":
        state["person"] = None
        await _do_file_and_ask_delete(q, state, token)

    elif verb == "name":
        PENDING_NAME[state["chat_id"]] = token
        await q.edit_message_text(
            f"👤 Escríbame el nombre de la persona para esta nota "
            f"(_{vn.AREAS[state['area']]['label']}_):", parse_mode="Markdown")

    elif verb == "del":
        yes = parts[2] == "yes"
        if yes:
            # borra el mensaje de voz de Telegram (si aplica) y archiva el audio
            mid = state.get("tg_voice_msg_id")
            if mid:
                try:
                    await ctx.bot.delete_message(state["chat_id"], mid)
                except Exception:
                    pass
            res = await asyncio.to_thread(vn.archive_audio, state["audio"])
            tail = "🗑️ Audio archivado." if res.get("ok") else f"({res.get('error')})"
        else:
            tail = "💾 Audio conservado."
        filed = state.get("filed", {})
        await q.edit_message_text(
            f"✅ Guardado en _{filed.get('area_label','')}_ · *{filed.get('title','')}*\n{tail}",
            parse_mode="Markdown")
        PENDING.pop(token, None)


def _slug(s):
    return vn._strip(s)


# -------- Vigilante de Memos de voz de Apple (carpeta vigilada) ----------------
VM_DIR = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
VM_STATE = Path.home() / ".openjarvis" / "voz_watch_state.json"


def _owner_chat():
    ids = allowed_ids()
    env = os.environ.get("JARVIS_OWNER_CHAT")
    return env or (next(iter(ids)) if ids else None)


async def watch_voice_memos(app):
    """Procesa SOLO grabaciones creadas DESPUÉS de activar el vigilante.

    Línea base por marca de tiempo (`start_ts`): nunca toca el histórico, ni
    siquiera si el permiso de Disco Completo se concede más tarde (el backlog
    tiene mtime anterior a start_ts). `seen` evita reprocesar el mismo archivo.
    """
    st = {}
    if VM_STATE.exists():
        try:
            st = json.loads(VM_STATE.read_text())
        except Exception:
            st = {}
    if not isinstance(st, dict) or "start_ts" not in st:
        st = {"start_ts": time.time(), "seen": []}
        VM_STATE.write_text(json.dumps(st))
        print(f"[memos] línea base por fecha fijada — solo grabaciones nuevas", flush=True)
    start_ts = st["start_ts"]
    seen = set(st.get("seen", []))

    # diagnóstico de Acceso a Disco Completo: cuántos .m4a alcanza a ver
    try:
        visibles = len(list(VM_DIR.glob("*.m4a"))) if VM_DIR.is_dir() else -1
    except Exception:
        visibles = -1
    if visibles <= 0:
        print(f"[memos] ⚠ visibles={visibles} — sin Acceso a Disco Completo "
              f"(otorgar a python3.13). El vigilante quedará inactivo hasta entonces.",
              flush=True)
    else:
        print(f"[memos] ✓ Acceso OK — {visibles} grabaciones visibles "
              f"(solo procesaré las nuevas desde ahora).", flush=True)

    def _save():
        VM_STATE.write_text(json.dumps({"start_ts": start_ts, "seen": sorted(seen)}))

    sizes = {}  # debounce: esperar a que el archivo deje de crecer
    while True:
        try:
            chat = _owner_chat()
            if chat and VM_DIR.is_dir():
                for p in sorted(VM_DIR.glob("*.m4a")):
                    if p.name in seen:
                        continue
                    stt = p.stat()
                    if stt.st_mtime <= start_ts:   # del histórico → ignorar
                        continue
                    sz = stt.st_size
                    if sizes.get(p.name) != sz:    # aún copiándose/grabándose
                        sizes[p.name] = sz
                        continue
                    # estable y nuevo → procesar
                    seen.add(p.name)
                    _save()
                    try:
                        text = await transcribe_audio(p)
                        if not text:
                            continue
                        cls = await asyncio.to_thread(vn.classify, text, _extract_complete)
                        state = {"text": text, "area": cls["area"], "person": cls["person"],
                                 "resumen": cls["resumen"], "confidence": cls["confidence"],
                                 "candidates": cls["candidates"], "audio": str(p),
                                 "tg_voice_msg_id": None, "source": "Memos",
                                 "chat_id": int(chat)}
                        await app.bot.send_message(
                            int(chat), "📒 Nueva *nota de voz de Apple* detectada:",
                            parse_mode="Markdown")
                        await _send_card(app.bot, int(chat), state)
                    except Exception as e:
                        print(f"[memos] error con {p.name}: {e}", flush=True)
        except Exception as e:
            print(f"[memos] watcher: {e}", flush=True)
        await asyncio.sleep(30)


# -------- Briefs proactivos (mañana y noche) ------------------------------------
BRIEF_STATE = Path.home() / ".openjarvis" / "briefs_state.json"
BRIEF_MORNING = os.environ.get("JARVIS_BRIEF_MORNING", "07:00")
BRIEF_EVENING = os.environ.get("JARVIS_BRIEF_EVENING", "21:00")
BRIEFS_ON = os.environ.get("JARVIS_BRIEFS", "on").lower() not in ("off", "0", "no")
_TG_MAX = 3900  # margen bajo el límite de 4096 de Telegram


def _brief_llm(msgs, fallback_parts):
    """Redacta el brief con el LLM local; si falla, manda los datos crudos
    (mejor un brief feo que ningún brief)."""
    try:
        out = _core_complete(msgs)
        if out:
            return out[:_TG_MAX]
    except Exception as e:
        print(f"[brief] LLM falló ({e}) — mando datos crudos", flush=True)
    raw = "\n\n".join(p for p in fallback_parts if p)
    return raw[:_TG_MAX] if raw else "Sin datos disponibles para el brief, Juanchi."


def morning_brief_text():
    """Brief de la mañana: agenda de hoy + pendientes + recordatorios + correo."""
    parts = []
    for getter in (lambda: task_context(""),
                   lambda: serve_hud.calendar_context(days=2),
                   lambda: serve_hud.reminders_context(),
                   lambda: serve_hud.mail_context()):
        try:
            ctx = getter()
            if ctx:
                parts.append(ctx)
        except Exception:
            pass
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "system", "content":
             "Redacta el BRIEF DE LA MAÑANA para Juan con los DATOS REALES que "
             "siguen. Formato: saludo de una línea y secciones 📅 Hoy · "
             "⚠️ Vencidas/urgente · ✅ Pendientes clave · 🔔 Recordatorios · "
             "📧 Correo (solo lo importante). Breve y accionable; enfócate en HOY. "
             "Usa solo los datos provistos; sección sin datos = una línea. Si viene "
             "un PERFIL, prioriza lo que más acerca a esas metas."}]
    try:
        prof = ob.profile_context()
        if prof:
            msgs.append({"role": "system", "content": prof})
    except Exception:
        pass
    msgs += [{"role": "system", "content": p} for p in parts]
    msgs.append({"role": "user", "content": "Buenos días, dame mi brief del día."})
    return "☀️ " + _brief_llm(msgs, parts)


def evening_brief_text():
    """Cierre del día: destila el diario, reporta qué quedó abierto y qué viene."""
    diary = cm.distill_day(complete=_extract_complete)
    parts = []
    try:
        data = serve_hud.scan_tasks()
        manana = (datetime.now().date() + timedelta(days=1)).isoformat()
        lines = []
        if data["overdue"]:
            lines.append("VENCIDAS: " + " · ".join(
                f"{t['text']} [{t['project']}, {t['due']}]" for t in data["overdue"]))
        if data["due_today"]:
            lines.append("QUEDARON ABIERTAS HOY: " + " · ".join(
                f"{t['text']} [{t['project']}]" for t in data["due_today"]))
        vence_manana = [t for t in data["upcoming"] if t["due"] == manana]
        if vence_manana:
            lines.append("VENCEN MAÑANA: " + " · ".join(
                f"{t['text']} [{t['project']}]" for t in vence_manana))
        if lines:
            parts.append("DATOS REALES de pendientes (Obsidian):\n" + "\n".join(lines))
    except Exception:
        pass
    try:
        cal = serve_hud.calendar_context(days=2)
        if cal:
            parts.append(cal)
    except Exception:
        pass
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "system", "content":
             "Redacta el CIERRE DEL DÍA para Juan con los DATOS REALES que siguen. "
             "Formato: 2-3 líneas de balance, luego secciones ⏳ Quedó abierto · "
             "📅 Mañana (agenda y vencimientos). Breve, en tu tono paisa, y cierra "
             "con un «chaíto». Usa solo los datos "
             "provistos; si no hay nada en una sección, dilo en una línea. Si viene "
             "un PERFIL, señala qué de mañana acerca (o aleja) de esas metas."}]
    try:
        prof = ob.profile_context()
        if prof:
            msgs.append({"role": "system", "content": prof})
    except Exception:
        pass
    msgs += [{"role": "system", "content": p} for p in parts]
    msgs.append({"role": "user", "content": "Dame el cierre del día."})
    txt = "🌙 " + _brief_llm(msgs, parts)
    if diary.get("ok"):
        txt += f"\n\n📓 Diario del día guardado en Obsidian:\n`{diary['rel']}`"
    return txt


def _run_vault_sync():
    """Sync incremental del índice del vault (memory.db) — mantiene fresca la
    búsqueda «¿qué sé de X?». Corre tras el cierre nocturno. Best-effort."""
    import subprocess
    env = dict(os.environ)
    env["PATH"] = (str(Path.home() / ".local/bin") + os.pathsep
                   + env.get("PATH", ""))   # sync_vault llama a `uv` internamente
    script = Path(__file__).resolve().parent / "sync_vault.py"
    try:
        r = subprocess.run([sys.executable, str(script)], env=env,
                           capture_output=True, text=True, timeout=1800)
        out = (r.stdout or r.stderr or "").strip()
        print(f"[sync] vault → memoria: {out[-200:] or 'sin salida'}", flush=True)
    except Exception as e:
        print(f"[sync] omitido (no crítico): {e}", flush=True)


def _brief_state():
    try:
        return json.loads(BRIEF_STATE.read_text())
    except Exception:
        return {}


def _brief_mark(kind, day):
    st = _brief_state()
    st[kind] = day
    try:
        BRIEF_STATE.write_text(json.dumps(st))
    except Exception:
        pass


async def _send_brief(bot, kind):
    """Genera y envía un brief al dueño. kind: 'morning' | 'evening'."""
    chat = _owner_chat()
    if not chat:
        return False
    builder = morning_brief_text if kind == "morning" else evening_brief_text
    try:
        text = await asyncio.to_thread(builder)
    except Exception as e:
        print(f"[brief] no pude armar el brief {kind}: {e}", flush=True)
        return False
    await bot.send_message(int(chat), text)
    cm.log_event("brief", chat_id=int(chat),
                 tipo="mañana" if kind == "morning" else "noche")
    return True


async def daily_briefs(app):
    """Tarea de fondo: brief a las BRIEF_MORNING y cierre a las BRIEF_EVENING.
    Estado en disco para no duplicar tras reinicios (KeepAlive respawnea seguido).
    Si la Mac dormía a la hora exacta, se envía al despertar (con ventana)."""
    print(f"[brief] briefs proactivos {'activos' if BRIEFS_ON else 'APAGADOS'} "
          f"(mañana {BRIEF_MORNING}, noche {BRIEF_EVENING})", flush=True)
    while True:
        try:
            if BRIEFS_ON:
                now = datetime.now()
                today = now.date().isoformat()
                hm = now.strftime("%H:%M")
                st = _brief_state()
                # mañana: desde BRIEF_MORNING, con ventana hasta las 14:00
                if (st.get("morning") != today and BRIEF_MORNING <= hm < "14:00"):
                    if await _send_brief(app.bot, "morning"):
                        _brief_mark("morning", today)
                # noche: desde BRIEF_EVENING hasta el fin del día
                if st.get("evening") != today and hm >= BRIEF_EVENING:
                    if await _send_brief(app.bot, "evening"):
                        _brief_mark("evening", today)
                        # con el día cerrado, refresca el índice del vault
                        await asyncio.to_thread(_run_vault_sync)
        except Exception as e:
            print(f"[brief] loop: {e}", flush=True)
        await asyncio.sleep(60)


async def cmd_brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/brief — manda el brief de la mañana ahora mismo (a demanda)."""
    if not await gate(update):
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    text = await asyncio.to_thread(morning_brief_text)
    await update.message.reply_text(text)


async def cmd_diario(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/diario — destila el día a la nota de Obsidian ahora mismo."""
    if not await gate(update):
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
    res = await asyncio.to_thread(cm.distill_day, None, _extract_complete)
    if res.get("ok"):
        await update.message.reply_text(
            f"📓 Diario del día actualizado, Juanchi ({res['chats']} turnos):\n"
            f"`{res['rel']}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            f"📓 No generé el diario: {res.get('reason', 'error desconocido')}.")


async def _post_init(app):
    app.create_task(watch_voice_memos(app))
    print("[memos] vigilante de Memos de voz activo (cada 30s)", flush=True)
    app.create_task(daily_briefs(app))


def wait_for_network(host="api.telegram.org", timeout=120):
    """Al arrancar con el equipo, la red/DNS puede no estar lista todavía.
    Espera (hasta `timeout`s) a que el host resuelva antes de iniciar el polling,
    para no morir con httpx.ConnectError y depender solo de KeepAlive."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            socket.gethostbyname(host)
            return True
        except OSError:
            print(f"… red/DNS aún no lista para {host}, reintentando en 3s",
                  flush=True)
            time.sleep(3)
    print(f"⚠ red/DNS no disponible tras {timeout}s — intento arrancar igual",
          flush=True)
    return False


def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("voz", cmd_voz))
    app.add_handler(CommandHandler("dios", cmd_dios))
    app.add_handler(CommandHandler("normal", cmd_normal))
    app.add_handler(CommandHandler("nota", cmd_nota))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("diario", cmd_diario))
    app.add_handler(CommandHandler("onboarding", cmd_onboarding))
    app.add_handler(CallbackQueryHandler(on_ob_button, pattern=r"^ob\|"))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^vn\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_error_handler(on_error)
    return app


def main():
    ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ALLOW_FILE.exists():
        ALLOW_FILE.write_text("")  # vacío = lockdown hasta autorizar un ID
    print(f"JARVIS Telegram → núcleo {CORE} · allowlist: {ALLOW_FILE}", flush=True)
    print(f"IDs autorizados: {allowed_ids() or '(ninguno — manda un mensaje al bot)'}",
          flush=True)
    print(f"Modo dios disponible: {'sí (' + GOD_MODEL + ')' if _claude_available() else 'no (sin ANTHROPIC_API_KEY)'}",
          flush=True)
    # Loop supervisor: si el polling cae por un fallo de red que escala,
    # esperamos a que vuelva el DNS y reintentamos en vez de morir. launchd
    # (KeepAlive) sigue como último respaldo si el proceso muere del todo.
    while True:
        wait_for_network()
        try:
            build_app().run_polling(drop_pending_updates=True)
            break  # salida limpia (SIGTERM/SIGINT) → no reiniciar
        except (NetworkError, TimedOut, OSError) as e:
            print(f"[supervisor] polling cayó por red ({e}); reintento en 5s", flush=True)
            time.sleep(5)
        except Exception as e:
            print(f"[supervisor] polling cayó ({e!r}); reintento en 10s", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
