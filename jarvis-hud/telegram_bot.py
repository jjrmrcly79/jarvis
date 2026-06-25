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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

TOKEN = os.environ.get("JARVIS_TG_TOKEN")
if not TOKEN:
    sys.exit("Falta JARVIS_TG_TOKEN en el entorno "
             "(configúralo en el plist de launchd o expórtalo antes de correr).")
CORE = os.environ.get("JARVIS_CORE", "http://127.0.0.1:8000")
MODEL = os.environ.get("JARVIS_MODEL", "qwen3.5:27b")
ALLOW_FILE = Path.home() / ".openjarvis" / "telegram_allowed.txt"

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
        raw = _core_complete([{"role": "system", "content": sys_p},
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
        raw = _core_complete([{"role": "system", "content": sys_p},
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
    # Acciones reales (escritura). El recordatorio se evalúa ANTES que el evento:
    # un "ponme un recordatorio a las 5" no debe acabar como evento de calendario.
    if is_reminder_create(text):
        return reminder_create_flow(text)
    if is_cal_create(text):
        return cal_create_flow(text)
    # historial: solo turnos user/assistant (el contexto inyectado es por-llamada)
    hist = histories.setdefault(chat_id, [])
    msgs = [{"role": "system", "content": SYSTEM}]
    if is_task_query(text):
        ctx = task_context(text)
        if ctx:
            msgs.append({"role": "system", "content": ctx})
    try:
        ent = serve_hud.entity_context(text)   # MOC + notas recientes del proyecto/cliente
        if ent:
            msgs.append({"role": "system", "content": ent})
    except Exception:
        pass
    if is_cal_query(text):
        try:
            cal = serve_hud.calendar_context(days=14)   # agenda del Calendario de Mac
            if cal:
                msgs.append({"role": "system", "content": cal})
        except Exception:
            pass
    if is_mail_query(text):
        try:
            mail = serve_hud.mail_context()   # correos de hoy en Mail.app
            if mail:
                msgs.append({"role": "system", "content": mail})
        except Exception:
            pass
    if is_reminder_query(text):
        try:
            rem = serve_hud.reminders_context()   # recordatorios de Reminders.app
            if rem:
                msgs.append({"role": "system", "content": rem})
        except Exception:
            pass
    msgs += hist[-8:]
    msgs.append({"role": "user", "content": text})
    body = json.dumps({"model": MODEL, "messages": msgs, "stream": False}).encode()
    req = urllib.request.Request(CORE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            j = json.load(r)
        reply = j["choices"][0]["message"]["content"]
        import re
        reply = re.sub(r"<think>[\s\S]*?</think>", "", reply).strip() or "(sin respuesta)"
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
    """Archiva el texto en el MD y pregunta si borrar el audio."""
    res = await asyncio.to_thread(
        vn.file_note, state["area"], state["person"], state["text"],
        datetime.now(), state.get("source", "voz"), state.get("resumen", ""))
    if not res.get("ok"):
        msg = f"⚠️ No pude archivar: {res.get('error')}"
        await _edit_or_send(query_or_bot, state, msg, None)
        return
    state["filed"] = res
    accion = "creé" if res["nuevo"] else "actualicé"
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

    cls = await asyncio.to_thread(vn.classify, text, _core_complete)
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
                        cls = await asyncio.to_thread(vn.classify, text, _core_complete)
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


async def _post_init(app):
    app.create_task(watch_voice_memos(app))
    print("[memos] vigilante de Memos de voz activo (cada 30s)", flush=True)


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


def main():
    wait_for_network()
    ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ALLOW_FILE.exists():
        ALLOW_FILE.write_text("")  # vacío = lockdown hasta autorizar un ID
    print(f"JARVIS Telegram → núcleo {CORE} · allowlist: {ALLOW_FILE}", flush=True)
    print(f"IDs autorizados: {allowed_ids() or '(ninguno — manda un mensaje al bot)'}",
          flush=True)
    app = ApplicationBuilder().token(TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("voz", cmd_voz))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^vn\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
