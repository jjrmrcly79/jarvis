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

SYSTEM = ("Eres J.A.R.V.I.S, el asistente personal de tu jefe, por Telegram. "
          "Respondes en español, sereno y elegante, conciso (1-4 frases salvo que "
          "pidan detalle). Llamas al usuario 'señor' o 'jefe' a veces. Nunca inventas "
          "datos: si te dan DATOS REALES de notas/pendientes, úsalos tal cual.")

histories = {}   # chat_id -> [mensajes]

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
        return "¿Qué quiere que le recuerde, señor?"

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
        return (f"No pude crear el recordatorio «{title}», señor. Reminders respondió: "
                f"{res.get('error', 'error desconocido')}.")

    when_txt = f"\n📅 vence {_fmt_when(due_dt)}" if due_dt else ""
    cm.log_event("accion", detalle=f"Recordatorio creado: «{title}» "
                 f"(lista {res.get('list', list_name)}"
                 + (f", vence {_fmt_when(due_dt)}" if due_dt else "") + ")")
    return (f"✅ Recordatorio creado, señor:\n«{title}»\n"
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
        return f"No pude contactar el núcleo para procesar la cita ({e}), señor."

    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return ("No me quedaron claros los datos de la cita, señor. "
                "¿Me lo dice completo? (ej. «agéndame junta con Pedro mañana 10:00»)")
    try:
        data = json.loads(m.group(0))
    except Exception:
        return ("No me quedaron claros los datos de la cita, señor. "
                "¿Me lo dice completo? (ej. «agéndame junta con Pedro mañana 10:00»)")

    title = (data.get("title") or "").strip() or "Evento"
    d, st = data.get("date"), data.get("start")
    if not d or not st:
        falta = "la fecha" if not d else "la hora"
        return (f"Con gusto agendo «{title}», señor, pero me falta {falta}. "
                "¿Me lo indica en un mensaje? (ej. «agéndame X mañana 10:00»)")
    try:
        sh, sm = (int(x) for x in str(st).split(":")[:2])
        start_dt = datetime.strptime(d, "%Y-%m-%d").replace(hour=sh, minute=sm)
    except Exception:
        return ("La fecha u hora no quedaron bien, señor. "
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
        return (f"No pude agendar «{title}», señor. Calendar.app respondió: "
                f"{res.get('error', 'error desconocido')}.")

    fin = f"–{end_dt.hour:02d}:{end_dt.minute:02d}" if end_dt else ""
    loc = res.get("calendar", "Calendario")
    cm.log_event("accion", detalle=f"Evento agendado: «{title}» "
                 f"{_fmt_when(start_dt)}{fin} ({loc})")
    return (f"✅ Agendado, señor:\n«{title}»\n{_fmt_when(start_dt)}{fin}\n"
            f"{loc} (iCloud). Ya está en su Mac.")


def task_context(text):
    """Datos reales de pendientes (determinista, sin alucinar)."""
    try:
        data = serve_hud.scan_tasks()
        projs = {p["name"].lower(): p["name"] for p in data["projects"]}
        for low, name in projs.items():
            if low in text.lower():
                d = serve_hud.project_tasks(name)
                lines = "\n".join("- " + t["text"] + (f" (📅 {t['due']})" if t["due"] else "")
                                  for t in d["tasks"])
                return (f"DATOS REALES de Obsidian — pendientes ABIERTOS de {d['project']} "
                        f"({d['open']} total):\n{lines}")
        s = f"DATOS REALES de Obsidian (hoy {data['today']}):\n"
        s += f"Total pendientes: {data['total_open']}\n"
        s += "Por proyecto: " + ", ".join(f"{p['name']}={p['open']}" for p in data["projects"]) + "\n"
        if data["overdue"]:
            s += "VENCIDAS: " + " · ".join(f"{t['text']} [{t['project']}, {t['due']}]"
                                           for t in data["overdue"]) + "\n"
        s += "Pendientes recientes:\n" + "\n".join(
            f"- [{t['project']}] {t['text']}" for t in data["recent"])
        return s
    except Exception:
        return None


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
            "datos provistos; si una sección viene vacía, dilo en una línea."})
    if is_task_query(text) or status:
        ctx = task_context(text)
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
    await update.message.reply_text(
        "👋 J.A.R.V.I.S a su disposición, señor.\n"
        "Escríbame lo que necesite. Pregúnteme por sus pendientes "
        "(ej. «¿qué tengo en Nexia?») o cualquier cosa de sus notas.\n"
        "Use /voz para activar o silenciar mis respuestas habladas.")


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
    await update.message.reply_text(f"Voz {estado}, señor.")


async def cmd_dios(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Activa el 'modo dios' (Claude) en este chat. Acepta /dios on | off."""
    if not await gate(update):
        return
    chat_id = update.effective_chat.id
    if not _claude_available():
        await update.message.reply_text(
            "No tengo configurado Claude, señor: falta ANTHROPIC_API_KEY "
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
            f"🧠 Modo dios ACTIVADO, señor — razono con {GOD_MODEL}. "
            "Use /normal para volver al modelo local.")
    else:
        await update.message.reply_text(
            "🔌 Modo dios desactivado. Vuelvo al modelo local (Ollama), señor.")


async def cmd_normal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Vuelve al modelo local (Ollama) en este chat."""
    if not await gate(update):
        return
    GOD_MODE[update.effective_chat.id] = False
    await update.message.reply_text(
        "🔌 Modo local (Ollama) activo, señor. Use /dios para el modo poderoso.")


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
    who = state["person"] or "Inbox"
    rows = [[InlineKeyboardButton(f"✅ {area_label} · {who}", callback_data=f"vn|ok|{token}")]]
    rows.append([InlineKeyboardButton(vn.AREAS[k]["label"], callback_data=f"vn|area|{k}|{token}")
                 for k in ("personal", "nexia", "villacatania")])
    rows.append([InlineKeyboardButton("👤 Otra persona", callback_data=f"vn|name|{token}")])
    return InlineKeyboardMarkup(rows)


def _people_markup(token, state):
    """Tras elegir área: candidatos de esa área + Inbox + otra persona."""
    rows = []
    cands = state.get("candidates", [])
    for i in range(0, len(cands), 2):
        rows.append([InlineKeyboardButton(c, callback_data=f"vn|pick|{j}|{token}")
                     for j, c in enumerate(cands[i:i+2], start=i)])
    rows.append([InlineKeyboardButton("📥 Inbox del área", callback_data=f"vn|inbox|{token}"),
                 InlineKeyboardButton("👤 Otra persona", callback_data=f"vn|name|{token}")])
    return InlineKeyboardMarkup(rows)


def _del_markup(token):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑️ Borrar audio", callback_data=f"vn|del|yes|{token}"),
        InlineKeyboardButton("💾 Conservar", callback_data=f"vn|del|no|{token}")]])


def _card_text(state):
    area_label = vn.AREAS[state["area"]]["label"]
    who = state["person"] or "Inbox del área"
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
        txt = (f"✅ Listo, señor. {accion.capitalize()} *{res['title']}* "
               f"en _{res['area_label']}_.\n`{res['rel']}`")
        await _edit_or_send(query_or_bot, state, txt, None)
        PENDING.pop(token, None)
        return
    txt = (f"✅ Listo, señor. {accion.capitalize()} *{res['title']}* "
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


async def capture_note_flow(update, ctx, content):
    """Archiva una nota de TEXTO en el vault con la misma tarjeta de botones
    que las notas de voz (clasificación área/persona, sin paso de audio)."""
    chat_id = update.effective_chat.id
    if not content or len(content) < 3:
        await update.message.reply_text(
            "📝 ¿Qué anoto, señor? Mándeme «toma nota: …» con el contenido.")
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
    msg = await update.message.reply_text("🎙️ Transcribiendo su nota, señor…")

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
        await msg.edit_text("🤔 No alcancé a entender nada del audio, señor.")
        return

    cls = await asyncio.to_thread(vn.classify, text, _extract_complete)
    state = {"text": text, "area": cls["area"], "person": cls["person"],
             "resumen": cls["resumen"], "confidence": cls["confidence"],
             "candidates": cls["candidates"], "audio": str(ogg),
             "tg_voice_msg_id": update.message.message_id,
             "source": "Telegram", "chat_id": chat_id}
    await msg.delete()
    await _send_card(ctx.bot, chat_id, state)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    verb = parts[1]
    token = parts[-1]
    state = PENDING.get(token)
    if not state:
        await q.edit_message_text("⌛ Esa nota ya expiró, señor. Reenvíela, por favor.")
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
    return raw[:_TG_MAX] if raw else "Sin datos disponibles para el brief, señor."


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
             "Usa solo los datos provistos; sección sin datos = una línea."}]
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
             "📅 Mañana (agenda y vencimientos). Breve, sereno. Usa solo los datos "
             "provistos; si no hay nada en una sección, dilo en una línea."}]
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
            f"📓 Diario del día actualizado, señor ({res['chats']} turnos):\n"
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
