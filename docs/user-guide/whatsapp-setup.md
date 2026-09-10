# WhatsApp assistant — setup & migration guide

This is a practical, copy‑paste checklist for standing up the always‑on WhatsApp
assistant (pair your personal WhatsApp → extract tasks/meetings → Obsidian +
Reminders) on a **new machine**, plus the gotchas we hit so you don't hit them
again.

The end state: a background service that pairs WhatsApp once (QR), listens for
incoming messages, runs each through a local LLM (Ollama), and files **tasks**
into an Obsidian note and **meetings** into Reminders.app — starting at login,
with no terminal open.

---

## Prerequisites

- **Node.js 18+** on `PATH` (the WhatsApp bridge is Node). Check: `node --version`.
- **Ollama** installed and running, with at least one model pulled
  (`ollama list`). A small model like `qwen3:8b` is a good speed/quality balance
  for extraction.
- **uv** (Python package manager) and the OpenJarvis repo cloned.
- macOS for the background service and the Reminders sink (the extractor and
  Obsidian sink are cross‑platform).

---

## Fresh setup / migration steps

```bash
# 1. Clone and enter the repo
git clone <your-fork-url> OpenJarvis
cd OpenJarvis

# 2. Install with the extras you actually use (see "uv sync gotcha" below)
uv sync --extra server --extra inference-cloud --extra speech --extra dev

# 3. Build the Rust extension
uv run maturin develop -m rust/crates/openjarvis-python/Cargo.toml

# 4. Point at your Obsidian vault (adjust the path)
export VAULT="$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents"

# 5. Pair WhatsApp once — scan the QR in WhatsApp ▸ Settings ▸
#    Linked Devices ▸ Link a Device
uv run jarvis channel connect --channel-type whatsapp_baileys

# 6. Once you see "Connected", Ctrl+C, then install the always-on service
uv run jarvis channel service install --to-reminders --to-obsidian --model qwen3:8b
```

Pairing state lives in `~/.openjarvis/whatsapp_baileys_bridge/auth/`. **Copy that
folder to the new machine** to migrate without re‑scanning the QR (optional — a
fresh QR scan also works).

Verify:

```bash
uv run jarvis channel service status
tail -n 20 ~/.openjarvis/whatsapp-service.log
uv run jarvis channel inbox
```

You want to see `Task extraction on (model: …)` and
`✓ Connected. Listening for messages…` in the log.

---

## Files & locations

| What | Where |
|---|---|
| Bridge runtime (npm install + compiled JS) | `~/.openjarvis/whatsapp_baileys_bridge/` |
| WhatsApp pairing/auth state | `~/.openjarvis/whatsapp_baileys_bridge/auth/` |
| Extracted items (JSONL) | `~/.openjarvis/extracted_tasks.jsonl` |
| Service wrapper script | `~/.openjarvis/whatsapp-service.sh` |
| Service definition (launchd) | `~/Library/LaunchAgents/com.openjarvis.whatsapp.plist` |
| Service log | `~/.openjarvis/whatsapp-service.log` |
| Tasks note (Obsidian) | `<vault>/Bandeja de WhatsApp.md` |

Service management: `jarvis channel service {install,status,uninstall}`.
To change options (model, note, etc.), `uninstall` then `install` again.

---

## Learnings & gotchas (things that bit us)

1. **Use Baileys 6.7.23, not 7.x‑rc.** The 7.0.0 release candidates fail QR
   pairing with **code 428 (Connection Terminated)** — the socket closes before
   a QR is issued. The stable `6.7.23` pairs fine on the same machine/network.
   Pinned in `whatsapp_baileys_bridge/package.json`.

2. **The bridge builds itself on first connect** (`npm install` + `tsc`). It can
   take 1–2 min the first time and prints progress. If dependencies change, it
   reinstalls automatically (manifest newer than `node_modules`).

3. **launchd has a minimal PATH.** A background service started by launchd does
   *not* get your interactive PATH, and `~/.zshrc` often bails out early for
   non‑interactive shells (`[[ $- != *i* ]] && return`), so `node`/`uv` go
   missing → "node not found on PATH". `jarvis channel service install` fixes
   this by detecting `node`/`npm`/`uv` at install time and baking their dirs
   into the wrapper's PATH. **Re‑run `service install` after changing your Node
   install** (e.g. new nvm version).

4. **`uv sync` (bare) strips optional extras.** Running plain `uv sync` uninstalls
   anything not in the base deps — the HUD server (`fastapi`/`uvicorn`), cloud
   engines (`anthropic`), voice (`piper-tts`), and the built Rust extension.
   Always sync **with your extras** and rebuild Rust:
   ```bash
   uv sync --extra server --extra inference-cloud --extra speech --extra dev
   uv run maturin develop -m rust/crates/openjarvis-python/Cargo.toml
   ```

5. **zsh does not treat `#` as a comment interactively** (unless
   `setopt interactive_comments`). Pasting commands with trailing `# comments`
   throws `parse error near ')'` or passes junk as flags. Paste commands
   *without* inline comments.

6. **Ollama must be running** for extraction. No API key needed (local). For
   always‑on, keep Ollama running at login (the Ollama.app does this; with
   Homebrew use `brew services start ollama`). Pick the extraction model with
   `--model` (e.g. `qwen3:8b` for speed) independent of the rest of Jarvis.

7. **Messages you send are ignored by default.** The bridge skips `fromMe`
   messages so it doesn't process your outgoing chatter. Self‑notes (the
   "Message Yourself" chat) are handled separately — see the channel docs.

8. **The Mac must be on and logged in** for the LaunchAgent to run. It restarts
   on failure and reconnects without a new QR (auth is saved).

---

## Troubleshooting quick reference

| Symptom | Fix |
|---|---|
| `node not found on PATH` in the log | Re‑run `jarvis channel service install` (re‑bakes PATH); ensure `node --version` works in your shell |
| Endless `disconnected` / code 428, no QR | Confirm Baileys is 6.7.23 (`package.json`); pull latest and reinstall the service |
| `no inference engine…` in the log | Start Ollama (`ollama ps`), then reinstall the service |
| HUD / `jarvis serve` broken after `uv sync` | Re‑sync with extras + rebuild Rust (gotcha #4) |
| Inbox empty but messages arrive | Expected for non‑actionable chat; test with a clear task from another contact |
| `parse error near ')'` when pasting | Remove inline `#` comments (gotcha #5) |
