/**
 * OpenJarvis WhatsApp Baileys Bridge
 *
 * JSON-line protocol on stdio:
 *
 * Input commands (stdin):
 *   {"type":"send","jid":"<jid>","text":"<message>"}
 *   {"type":"disconnect"}
 *
 * Output events (stdout):
 *   {"type":"message","jid":"<jid>","sender":"<sender>","text":"<text>","message_id":"<id>"}
 *   {"type":"status","status":"connected"|"disconnected"}
 *   {"type":"qr","data":"<qr-string>"}
 *   {"type":"error","message":"<description>"}
 */

import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  WASocket,
} from "@whiskeysockets/baileys";
import * as readline from "readline";
import * as qrcodeTerminal from "qrcode-terminal";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function emit(event: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify(event) + "\n");
}

// Normalise a JID to its bare identifier (drop the device suffix ":12" and the
// server part "@…"), so "5215551234567:12@s.whatsapp.net" -> "5215551234567".
function bareJid(jid: string | undefined | null): string {
  if (!jid) return "";
  return jid.split("@")[0].split(":")[0];
}

// Minimal pino-compatible no-op logger.  Keeps Baileys' internal chatter off
// stderr so the only thing written there is the scannable QR code, which the
// Python side forwards to the user's terminal during pairing.
const silentLogger: any = {
  level: "silent",
  child: () => silentLogger,
  trace: () => {},
  debug: () => {},
  info: () => {},
  warn: () => {},
  error: () => {},
  fatal: () => {},
};

function parseArgs(): { authDir: string } {
  const args = process.argv.slice(2);
  let authDir = "./auth";
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--auth-dir" && i + 1 < args.length) {
      authDir = args[i + 1];
      break;
    }
  }
  return { authDir };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const { authDir } = parseArgs();

  const { state, saveCreds } = await useMultiFileAuthState(authDir);

  // Use the current WhatsApp Web protocol version.  A stale version makes
  // WhatsApp close the socket (code 405) *before* issuing a QR, which looks
  // like an endless "disconnected" loop with no pairing code.
  let waVersion: [number, number, number] | undefined;
  try {
    const fetched = await fetchLatestBaileysVersion();
    waVersion = fetched.version;
    emit({ type: "info", message: `WhatsApp Web version ${waVersion.join(".")}` });
  } catch (err: any) {
    emit({
      type: "info",
      message: `Could not fetch WhatsApp Web version (${err?.message}); using bundled default`,
    });
  }

  let sock: WASocket | null = null;
  // Count consecutive closes with no successful open/QR so a persistent
  // failure (e.g. blocked network) surfaces an error instead of looping.
  let failStreak = 0;
  const MAX_ATTEMPTS = 6;

  // True when *jid* is our own "Message Yourself" chat, i.e. the message was
  // sent by us to ourselves. Used to let self-notes through the fromMe filter.
  // Matches on both the phone-number JID and the privacy LID, since WhatsApp
  // may address the self-chat by either.
  function isSelfChat(jid: string): boolean {
    const target = bareJid(jid);
    if (!target) return false;
    const me = bareJid(sock?.user?.id);
    const meLid = bareJid((sock?.user as any)?.lid);
    return (me !== "" && target === me) || (meLid !== "" && target === meLid);
  }

  function startSocket(attempt: number): void {
    // Alternate between the fetched WA Web version and Baileys' bundled
    // default so a version mismatch on either side can't permanently block
    // pairing.
    const useFetchedVersion = attempt % 2 === 0;
    sock = makeWASocket({
      version: useFetchedVersion ? waVersion : undefined,
      auth: state,
      logger: silentLogger,
      browser: Browsers.macOS("Desktop"),
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        failStreak = 0;
        // Show QR in stderr for local debugging and emit structured event.
        qrcodeTerminal.generate(qr, { small: true }, (code: string) => {
          process.stderr.write(code + "\n");
        });
        emit({ type: "qr", data: qr });
      }

      if (connection === "close") {
        const statusCode = (lastDisconnect?.error as any)?.output?.statusCode;
        const reason = (lastDisconnect?.error as any)?.message || "";

        if (statusCode === DisconnectReason.loggedOut) {
          emit({ type: "status", status: "disconnected" });
          emit({ type: "error", message: "Logged out from WhatsApp" });
          return;
        }

        emit({ type: "status", status: "disconnected", code: statusCode, reason });

        failStreak += 1;
        if (failStreak >= MAX_ATTEMPTS) {
          emit({
            type: "error",
            message:
              `WhatsApp closed the connection ${failStreak} times without ` +
              `pairing (last code ${statusCode ?? "?"}: ${reason}). This is ` +
              `almost always the network blocking WhatsApp Web — try another ` +
              `network (e.g. a phone hotspot), disable any VPN/proxy, then retry.`,
          });
          return;
        }
        // Reconnect with exponential backoff (2s, 4s, 8s… capped at 30s) so we
        // don't hammer WhatsApp and risk rate-limiting.
        const delay = Math.min(2000 * 2 ** (failStreak - 1), 30000);
        setTimeout(() => startSocket(attempt + 1), delay);
      } else if (connection === "open") {
        failStreak = 0;
        emit({ type: "status", status: "connected" });
      }
    });

    sock.ev.on("messages.upsert", (m) => {
      for (const msg of m.messages) {
        if (!msg.message) continue;

        const remoteJid = msg.key.remoteJid || "";
        if (msg.key.fromMe) {
          // Skip our own outgoing chatter to others, but DO process notes we
          // send to ourselves (the "Message Yourself" chat), so WhatsApp can be
          // used as a personal task inbox.
          if (!isSelfChat(remoteJid)) continue;
        }

        const text =
          msg.message.conversation ||
          msg.message.extendedTextMessage?.text ||
          "";
        if (!text) continue;

        emit({
          type: "message",
          jid: remoteJid,
          sender: msg.key.participant || remoteJid,
          text,
          message_id: msg.key.id || "",
        });
      }
    });
  }

  startSocket(0);

  // -----------------------------------------------------------------------
  // Stdin command processing
  // -----------------------------------------------------------------------

  const rl = readline.createInterface({ input: process.stdin });

  rl.on("line", async (line: string) => {
    let cmd: Record<string, unknown>;
    try {
      cmd = JSON.parse(line);
    } catch {
      emit({ type: "error", message: "Invalid JSON on stdin" });
      return;
    }

    if (cmd.type === "send" && sock) {
      try {
        await sock.sendMessage(cmd.jid as string, { text: cmd.text as string });
      } catch (err: any) {
        emit({ type: "error", message: `Send failed: ${err.message}` });
      }
    } else if (cmd.type === "disconnect") {
      if (sock) {
        sock.end(undefined);
      }
      emit({ type: "status", status: "disconnected" });
      process.exit(0);
    }
  });

  rl.on("close", () => {
    if (sock) {
      sock.end(undefined);
    }
    process.exit(0);
  });
}

main().catch((err) => {
  emit({ type: "error", message: `Fatal: ${err.message}` });
  process.exit(1);
});
