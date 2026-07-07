# J.A.R.V.I.S — Capacidades y Aprendizajes

> Documento maestro del asistente personal de Juan (OpenJarvis + jarvis-hud).
> Última actualización: 2026-06-20.
> Mantener al día cuando se agregue o cambie una capacidad.

---

## 1. Qué es

J.A.R.V.I.S es un asistente personal **local** que corre en la Mac y se controla
desde el **celular por Telegram**. Usa un modelo local (Ollama) con memoria de
Obsidian y acceso real a apps de la Mac (Calendario, Mail, Recordatorios).

**Principio rector:** *no alucinar*. Cuando hay datos reales (notas, agenda,
correos, recordatorios) se inyectan tal cual o se ejecutan acciones reales —
nunca se inventa una confirmación.

---

## 2. Arquitectura

```
   Celular (Telegram)
        │  long-polling (sin IP pública)
        ▼
   telegram_bot.py  ──HTTP──►  Núcleo :8000  ──►  Ollama (qwen3.5:27b)
   (jarvis-hud)                /v1/chat/completions
        │
        │ inyecta CONTEXTO REAL y ejecuta ACCIONES vía serve_hud.py
        ▼
   Mac: Obsidian · Calendar.app · Mail.app · Reminders.app  (osascript)
```

### Servicios (launchd, en `~/Library/LaunchAgents/`)

| Servicio | Lanza | Función |
|----------|-------|---------|
| `com.openjarvis.core` | `.venv/bin/jarvis serve --host 127.0.0.1 --port 8000` | Núcleo API (chat + memoria/RAG) |
| `com.openjarvis.telegram` | `.venv/bin/python jarvis-hud/telegram_bot.py` | **El bot de Telegram (lo que usas)** |
| `com.openjarvis.hud` | `.venv/bin/python jarvis-hud/serve_hud.py` | HUD web `:8090` (interfaz de navegador) |

> Los tres arrancan solos al encender el equipo (`RunAtLoad` + `KeepAlive`).
> **El bot NO depende del HUD web**: importa `serve_hud` como módulo en proceso;
> el `:8090` es solo la interfaz de navegador.
> **Blindaje de red:** `telegram_bot.py` espera a que el DNS resuelva
> `api.telegram.org` antes de arrancar el polling (`wait_for_network`), para no
> morir con `httpx.ConnectError` cuando el equipo enciende antes de tener red.
> El plist viejo `com.openjarvis.gateway` (apuntaba a `~/openjarvis`, ruta borrada,
> salía con error 78) quedó **archivado** en `~/Library/LaunchAgents/_disabled/`.

- **HUD web:** `serve_hud.py` sirve la interfaz en `http://127.0.0.1:8090/` y
  hace de proxy del núcleo (`/v1`, `/health`, `/dashboard`, …). Lanzador: `launch.sh`.
- **Núcleo:** `http://127.0.0.1:8000` — API estilo OpenAI (`/v1/chat/completions`).
- **Modelo:** `qwen3.5:27b` (Ollama local). Variable `JARVIS_MODEL`.
- **Memoria:** `~/.openjarvis/memory.db` (SQLite + FTS). Indexa Obsidian
  (`rebuild.sh` = reindex limpio; `jarvis-sync` = incremental).
- **Vault Obsidian:** `~/Library/Mobile Documents/iCloud~md~obsidian/Documents`.

> ⚠️ Hay dos checkouts: `~/dev/OpenJarvis` (el que usa launchd) y
> `~/openjarvis` / `~/dev/openjarvis` (copias). El archivo `jarvis_telegram.py`
> en la raíz del repo es una **copia vieja que NO se usa**.

---

## 3. El bot de Telegram (`jarvis-hud/telegram_bot.py`)

- **Token:** `JARVIS_TG_TOKEN` (env). **Núcleo:** `JARVIS_CORE` (default :8000).
- **Candado (allowlist):** `~/.openjarvis/telegram_allowed.txt`, un chat ID por
  línea. Vacío = bloqueado. Solo los IDs listados pueden usar el bot.
- **Cómo responde:** `ask_core(chat_id, text)` arma los mensajes para el LLM:
  1. `SYSTEM` (personalidad: sereno, conciso, "señor/jefe", no inventa).
  2. **Acciones reales** (escritura) — devuelven respuesta directa sin pasar por
     el chat libre: crear recordatorio / crear evento.
  3. **Contexto real** (lectura) — se inyecta como `system` según la intención:
     pendientes de Obsidian, entidades/MOC, agenda, correos, recordatorios.
  4. Historial corto (últimos 8 turnos por chat) + el mensaje del usuario.
- **Voz:** aún no (falta transcripción local).
- **Importante:** el bot **no usa** el sistema de tools/agente de OpenJarvis
  (`src/openjarvis/...`). Es chat plano + inyección de contexto + acciones por
  `osascript`. Toda capacidad nueva se construye en `serve_hud.py` (función
  determinista) + `telegram_bot.py` (rama de intención).

---

## 4. Capacidades actuales

| Capacidad | Tipo | Disparador (ejemplos) | Implementación |
|-----------|------|------------------------|----------------|
| **Pendientes Obsidian** | lectura | "¿qué tengo en Nexia?", "pendientes vencidos" | `serve_hud.scan_tasks` / `task_context` |
| **Entidades / notas (MOC)** | lectura | menciona un proyecto/cliente | `serve_hud.entity_context` |
| **Agenda (Calendario Mac)** | lectura | "¿qué tengo mañana?", "mi agenda" | `serve_hud.calendar_context` |
| **Crear evento** | escritura | "agéndame junta con Pedro mañana 10am" | `cal_create_flow` → `create_calendar_event` |
| **Resumen de correo (hoy)** | lectura | "resúmeme mis correos de hoy", "mi bandeja" | `serve_hud.mail_context` |
| **Recordatorios (ver)** | lectura | "¿qué recordatorios tengo?", "lista del súper" | `serve_hud.reminders_context` |
| **Crear recordatorio** | escritura | "recuérdame pagar la luz mañana 5pm" | `reminder_create_flow` → `create_reminder` |
| **Notas de voz → MD por persona** | escritura | mandas/grabas una nota de voz | `voice_notes.py` (transcribe + clasifica + archiva) |
| **Notas de TEXTO → MD por persona** | escritura | `/nota …`, "toma nota: …", "apunta esto: …" | `capture_note_flow` (misma tarjeta de botones, sin audio) |
| **Memoria persistente** | escritura | automático (todo intercambio) | `chat_memory.py` → `~/.openjarvis/chat_log.jsonl` |
| **Diario del día en Obsidian** | escritura | automático 21:00 · `/diario` | `chat_memory.distill_day` → `Personal/Diario Jarvis/AAAA-MM-DD.md` |
| **Brief proactivo** | lectura | automático 7:00 y 21:00 · `/brief` | `daily_briefs` (tarea de fondo del bot) |
| **Buscar en historial de correo** | lectura | "¿qué me ha escrito Marco?", "busca correos de CFE" | `serve_hud.mail_search_context` (Envelope Index + cuerpo) |
| **Buscar en la memoria del vault** | lectura | "¿qué sabes de X?", "¿qué tengo apuntado sobre Y?" | `serve_hud.knowledge_context` (FTS sobre memory.db) |

### 4.1 Pendientes de Obsidian (lectura)
Escáner determinista de checkboxes `- [ ]` en el vault, por proyecto, con fechas
de vencimiento (`📅`, `due:`, `vence:`). Distingue abiertos/vencidos/recientes.

### 4.2 Entidades / MOC (lectura)
Detecta proyectos/clientes mencionados e inyecta su nota MOC + notas recientes.

### 4.3 Calendario de Mac — Calendar.app / iCloud
- **Leer:** `calendar_context(days=14)` lee eventos próximos de **todos** los
  calendarios vía `osascript` (`_CAL_SCRIPT`), ordenados por fecha.
- **Crear:** `create_calendar_event(summary, start_dt, end_dt, calendar="Calendario", …)`
  (`_CAL_CREATE_SCRIPT`). Construye la fecha por **componentes** (robusto, zona
  local), default calendario **"Calendario"** (iCloud). Confirma con los datos
  reales del evento creado.
- **Flujo del bot:** `cal_create_flow` extrae título/fecha/hora con el LLM (JSON),
  pregunta si falta algo, crea y confirma. Comportamiento: **crear directo y
  reportar**.
- Calendarios en la Mac: `Calendario` (iCloud, default), `Family`, `Festivos en
  México`, `villacatania.celaya@gmail.com` (Google), Birthdays, etc.

### 4.4 Correo — Mail.app (Envelope Index, sin AppleScript)
- **Leer/resumir hoy:** `mail_context()` lee la bandeja directo del **Envelope
  Index** (SQLite de Mail, `~/Library/Mail/V*/MailData/`) — sub-segundo aun con
  50k correos, no necesita Mail.app abierta. Requiere Full Disk Access.
- **Buscar en el HISTORIAL (2026-07-06):** `mail_search_context(término, days=90)`
  busca por remitente/asunto e incluye el **inicio del cuerpo** (tabla
  `summaries`). Disparadores en el bot: «¿qué me ha escrito Marco?», «busca
  correos de CFE», «algún correo de X». OJO: «correos de hoy/ayer/esta semana»
  NO es búsqueda — cae al resumen del día (`_DAY_TERMS` lo filtra).
- Los scripts `mail_sync.py` / `connect_zoho.py` → `memory.db` son manuales y
  quedaron viejos; ya no hacen falta para leer/buscar.
- Cuentas en Mail.app: iCloud, Google, `villacatania.celaya@gmail.com`,
  `naturalezamisticaaa@gmail.com`.

### 4.5 Recordatorios — Reminders.app
- **Leer:** `reminders_context()` lista pendientes no completados, agrupados por
  lista, con vencimiento si lo tienen.
- **Crear:** `create_reminder(name, list_name="Actividades", due_dt=None)`. Si la
  lista no existe, cae al default del sistema. `reminder_lists()` devuelve los
  nombres reales (cacheado).
- **Flujo del bot:** `reminder_create_flow`. **Default list = "Actividades"**;
  si nombras una lista ("a la lista del súper") la usa. El título nunca falla
  (fallback determinista), la fecha se resuelve por reglas, la lista por match
  determinista contra las listas reales.
- Listas del usuario: Actividades, Súper, Compras del súper, Aplicaciones,
  NexIA Soluciones, Actividades Chapitas, + días de la semana.

---

### 4.6 Notas de voz → MD por persona (`voice_notes.py`)
Transcripción local + clasificación + archivado en la bóveda. **Dos fuentes:**
1. **Telegram:** mandas una nota de voz al bot → `handle_voice` la descarga,
   transcribe, clasifica y presenta una **tarjeta con botones**.
2. **Memos de voz de Apple:** `watch_voice_memos` (tarea de fondo, cada 30 s)
   detecta `.m4a` **nuevos** (línea base al arrancar — no toca el histórico) y
   empuja la misma tarjeta a tu Telegram.

**Motor (`voice_notes.py`):**
- `transcribe(path)` — **mlx-whisper large-v3** local (Apple Silicon, español).
  Repo: `mlx-community/whisper-large-v3-mlx` (variable `JARVIS_WHISPER_REPO`).
- `classify(texto, complete)` — área + persona + confianza. Se apoya en un
  **registro real** de personas de la bóveda (no alucina): si mencionas a alguien
  conocido → confianza alta. Fallback determinista por palabras clave de área.
- `file_note(area, persona, texto)` — agrega sección fechada `## fecha · 🎙️ fuente`
  al MD de la persona (lo crea con frontmatter si no existe). Sin persona → `Inbox de voz.md`.
- `archive_audio(path)` — **mueve** el audio a `~/.openjarvis/voz_archivo/AÑO/` (reversible).

**Áreas y carpeta de personas (un MD por persona, se va actualizando):**
| Área | Carpeta de personas |
|------|---------------------|
| Personal | `Personal/05-Relaciones/personas/` |
| Nexia | `Nexia/Personas/` |
| Villa Catania | `VillaCataniaVault/Comunidad/01_Directorio/` |

**Flujo de botones:** ✅ confirma la propuesta · `[Personal][Nexia][Villa Catania]`
cambia el área (muestra candidatos de esa área) · 👤 *Otra persona* pide el nombre
por texto. Tras archivar → **[🗑️ Borrar audio] [💾 Conservar]** (borrar elimina el
mensaje de voz en Telegram y archiva el `.ogg`; para Memos mueve el `.m4a`).

**Limpia del backlog (Fase 0):**
- `voice_inventory.py` (0a) — inventario sin transcribir → `~/.openjarvis/voice_inventory.json`
  + `Personal/Notas de voz/Inventario backlog.md`. **733 memos · 20.6 GB · 590 h de
  audio** (media 48 min, máx 20.6 h, sin dups de archivo).
- `voice_backlog.py` (0b/0c) — barrido nocturno **reanudable**. Transcribe del más
  corto al más largo hasta `--max-seconds` (30 min), clasifica, detecta si ya está
  en la bóveda (match literal), guarda cada transcripción en
  `Personal/Notas de voz/transcripciones/AÑO/`. Checkpoint en
  `~/.openjarvis/voice_backlog_results.jsonl`. Los **>30 min (263, ~528 GB·h)**
  NO se transcriben → van a la lista de revisión manual. Velocidad medida: **4.4×
  tiempo real**. NO borra ni mueve audio (eso se aprueba después).
  - Correr: `nohup caffeinate -i -s .venv/bin/python jarvis-hud/voice_backlog.py &`
  - Avance/dashboard: `… voice_backlog.py report` → `Personal/Notas de voz/Triage backlog.md`
  - Reanudar: volver a correr (salta los ya hechos por el JSONL).
- **Dashboard de limpia (en el HUD):** `http://127.0.0.1:8090/voz` — página dedicada
  en `serve_hud.py` (`VOICE_PAGE` + endpoints `GET /voice/data` y `POST /voice/archive`).
  Muestra las cubetas en vivo (con contenido · ya en bóveda · vacíos · largos · errores)
  con GB recuperables, y permite **archivar** (mover a `~/.openjarvis/voz_archivo/`,
  reversible) por selección. El HUD corre con el python con FDA → puede mover los audios.
  Archivados se registran en `~/.openjarvis/voice_archived.json` y se excluyen del conteo.

---

### 4.7 Memoria persistente + diario + briefs (2026-07-06)

**Memoria (`chat_memory.py`):** cada intercambio texto (pregunta + respuesta),
cada acción real (recordatorio/evento creado) y cada nota archivada quedan en
`~/.openjarvis/chat_log.jsonl` (append-only, fail-safe: el log jamás tumba al
bot). `log_exchange` lo hace el wrapper `ask_core` → nada se escapa.

**Notas de texto:** `/nota <texto>` o mensajes que EMPIEZAN con «toma nota:»,
«Nota:», «apunta esto:», «guarda esta nota». Reusa la tarjeta de botones de las
notas de voz (área/persona/Inbox), archiva con icono 📝 vía `vn.file_note`.
OJO: «apúntame/anótame X» sigue siendo RECORDATORIO (el detector de nota solo
casa al inicio del mensaje con la palabra "nota"/"esto").

**Diario (`distill_day`):** regenera `Personal/Diario Jarvis/AAAA-MM-DD.md`
desde el log (idempotente): Resumen + Compromisos (LLM, con fallback
determinista si falla) + Acciones ejecutadas + transcripción en callout
plegable. Se corre solo a las 21:00 (dentro del cierre) o con `/diario`.

**Briefs (`daily_briefs`, tarea de fondo):**
- ☀️ 7:00 — agenda + vencidas + pendientes clave + recordatorios + correo.
- 🌙 21:00 — destila el diario + qué quedó abierto + qué vence mañana + agenda.
- Estado en `~/.openjarvis/briefs_state.json` (no duplica tras reinicios); si
  la Mac dormía, el de la mañana se manda al despertar (ventana hasta 14:00).
- Config por env en el plist: `JARVIS_BRIEFS=off` apaga,
  `JARVIS_BRIEF_MORNING`/`JARVIS_BRIEF_EVENING` cambian horas.
- Tras el cierre nocturno corre `_run_vault_sync()` (= `sync_vault.py`,
  incremental) para que la búsqueda «¿qué sé de X?» (memory.db FTS) esté
  fresca cada día. Antepone `~/.local/bin` al PATH porque `sync_vault`
  llama a `uv` y el PATH de launchd no lo trae.
- Si el LLM falla, el brief sale con los datos crudos (nunca se queda callado).
- `/brief` lo dispara a demanda.

---

## 5. Ruteo de intención (orden en `ask_core`)

El orden importa para que las acciones no se pisen:

```
1. is_reminder_create   → reminder_create_flow   (ANTES que evento:
                                                   "recordatorio a las 5pm" ≠ cita)
2. is_cal_create        → cal_create_flow
3. (chat normal con inyección de contexto:)
   - is_task_query      → pendientes Obsidian
   - entity_context     → siempre que detecte entidad
   - is_cal_query       → agenda
   - is_mail_query      → correos de hoy
   - is_reminder_query  → recordatorios
```

Reglas clave:
- **Recordatorio vs evento:** recordatorio requiere verbo explícito
  ("recuérdame/anota/apunta") o "agrega/añade/pon + lista/recordatorio/súper".
  Evento requiere "agéndame/agenda/pon… + nombre de evento u hora". Por eso
  "ponme una **reunión** 10am" → evento, y "ponme un **recordatorio** 5pm" →
  recordatorio.
- Los verbos toleran clíticos ("agrégale", "añádelo", "ponme", "anótalo").

---

## 6. Aprendizajes técnicos (los caros de descubrir)

1. **El bot real es `jarvis-hud/telegram_bot.py`**, lanzado por launchd
   (`com.openjarvis.telegram`), y habla por HTTP `/v1/chat/completions`.
   `jarvis_telegram.py` (raíz) es basura vieja. El sistema de
   conectores/ToolRegistry de OpenJarvis **no** está en este camino.

2. **Sin acción real, el LLM alucina confirmaciones.** "Sí, te agendé la reunión"
   sin ejecutar nada. La cura: rama de acción determinista + confirmar solo con
   datos reales leídos de vuelta.

3. **El modelo local (qwen3.5:27b) es POCO FIABLE para extracción JSON.** A veces
   mete preámbulo en otro idioma, ignora "mañana", o no devuelve JSON limpio.
   → Para acciones, **no depender del LLM**: usar fallbacks deterministas
   (título por regex, fecha por reglas hoy/mañana/pasado-mañana + hora, lista por
   match contra las listas reales). El LLM solo se usa cuando acierta.
   **Mitigado (2026-07-06):** la EXTRACCIÓN (recordatorio/evento, clasificación
   de notas, destilado del diario) ahora usa **Claude Haiku** vía
   `_extract_complete()` (env `JARVIS_EXTRACT_MODEL`, default
   `claude-haiku-4-5-20251001`, centavos por llamada) con **fail-open al local**
   si no hay red/key. El chat general sigue 100% local ($0). Los fallbacks
   deterministas se conservan como última red.

4. **osascript a apps de Mac = la vía correcta** (Calendar/Mail/Reminders).
   Construir fechas por **componentes** (`set year/month/day…`), no por strings
   (dependen del idioma del sistema). Setear `day` a 1 antes de cambiar mes/año
   evita desbordes (31 → mes corto).

5. **Permisos de Automatización** (Ajustes → Privacidad) ya concedidos para
   Calendar, Mail y Reminders. Mail.app debe estar abierta para leer en vivo.

   **Acceso a Disco Completo** (notas de voz / Memos): el vigilante lee la carpeta
   protegida de Memos de Apple → requiere FDA para `python3.13`
   (`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`). 
   ⚠️ Tras conceder FDA, `kickstart -k` NO basta para que macOS reevalúe TCC →
   hacer **recarga completa**: `launchctl bootout … && launchctl bootstrap …`.
   El log lo confirma: `[memos] ✓ Acceso OK — N grabaciones visibles`.

7. **ffmpeg bajo launchd:** mlx-whisper llama a `ffmpeg` por subprocess. El PATH de
   launchd es mínimo y NO incluye `/opt/homebrew/bin` → error `[Errno 2] ... 'ffmpeg'`.
   Solución (doble): `PATH` con `/opt/homebrew/bin` en el plist del bot **y**
   `voice_notes.py` lo antepone a `os.environ` al importar.

8. **mlx/Metal se CUELGA en un hilo de fondo:** correr `mlx_whisper.transcribe`
   dentro de `asyncio.to_thread` deja el proceso vivo pero a 0% CPU (deadlock de
   Metal fuera del hilo principal). Solución: transcribir en un **subproceso**
   (`transcribe_cli.py` vía `asyncio.create_subprocess_exec`). Probado standalone
   funciona; en thread no. Bonus: libera la RAM del modelo (~3 GB) al terminar.

6. **Lectura en vivo vs índice:** el correo y la agenda se leen en vivo (frescos),
   porque no hay sync automático confiable. Acepta ~10–15 s de latencia extra.

7. **Patrón de validación:** crear artefacto de prueba marcado → verificar →
   **borrarlo**. Ojo: si el LLM "limpia" el marcador del título, el borrado por
   nombre no lo encuentra → barrer también por el título real.

---

## 7. Cómo agregar una capacidad nueva (patrón)

1. **`serve_hud.py`** — función determinista:
   - *Lectura:* `xxx_context()` → devuelve un bloque de texto real (o `None`).
   - *Escritura:* `create_xxx(...)` → ejecuta `osascript`/API y devuelve
     `{ok, …}` / `{ok: False, error}`.
2. **`telegram_bot.py`**:
   - `is_xxx_query()` / `is_xxx_create()` (regex de intención).
   - Para escritura: `xxx_create_flow()` (extracción + fallback determinista +
     ejecución + confirmación real).
   - Enganchar en `ask_core` (acciones arriba; lecturas en la zona de contexto).
3. **Validar** aislado y end-to-end; **limpiar** artefactos de prueba.
4. **Reiniciar** el servicio (ver §8) y actualizar este MD.

---

## 8. Operación

```bash
# Reiniciar el bot tras editar el código:
launchctl kickstart -k gui/$(id -u)/com.openjarvis.telegram

# Reiniciar el HUD web tras editar serve_hud.py:
launchctl kickstart -k gui/$(id -u)/com.openjarvis.hud

# Ver estado de los servicios (deben aparecer los tres):
launchctl list | grep openjarvis

# Log del bot:
tail -f ~/.openjarvis/telegram-bot.log

# Probar una función aislada (sin Telegram):
/Users/juangarces/dev/OpenJarvis/.venv/bin/python -c "import serve_hud; print(serve_hud.calendar_context(days=7))"

# Autorizar un chat ID nuevo:
echo "CHAT_ID" >> ~/.openjarvis/telegram_allowed.txt   # y reiniciar el bot

# Notas de voz — reiniciar la línea base del vigilante de Memos
# (procesará como "nuevos" los .m4a que aparezcan tras esto):
rm ~/.openjarvis/voz_watch_state.json && launchctl kickstart -k gui/$(id -u)/com.openjarvis.telegram

# Audios archivados al borrar (reversible):
ls ~/.openjarvis/voz_archivo/
```

**Archivos clave** (`jarvis-hud/`): `telegram_bot.py` (bot + intención),
`serve_hud.py` (HUD + funciones reales), `launch.sh` (lanzador), `rebuild.sh`
(reindex Obsidian), `mail_sync.py` / `connect_zoho.py` (indexadores de correo,
manuales).

---

## 9. Limitaciones y pendientes

- [ ] **Multi-turno:** si falta un dato al crear, el bot pide repetir todo en un
      mensaje (aún no encadena la respuesta de seguimiento).
- [x] **Memoria persistente + diario + briefs 7:00/21:00:** ✅ (2026-07-06). Ver §4.7.
- [x] **Notas de voz:** ✅ implementadas (Telegram + vigilante de Memos, mlx-whisper
      large-v3, clasificación área/persona, archivado en MD). Ver §4.6.
- [ ] **Correo:** solo cabeceras de hoy. Pendiente: leer el **cuerpo** de un
      correo específico, filtrar solo **no leídos**, o rango de fechas.
- [ ] **Recordatorios:** pendiente **completar/cerrar** desde el bot
      ("ya compré el queso"). Hay dos listas de compras parecidas (Súper /
      Compras del súper) — "del súper" cae en *Súper*.
- [ ] **Calendario:** sin invitados ni selección de calendario distinto a
      "Calendario" todavía.
- [ ] **Modelo:** la extracción depende de un modelo local flojo; considerar uno
      mejor para JSON, o más reglas deterministas.
