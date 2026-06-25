"""Mail read tool — read messages from the native macOS Mail.app (read-only).

Reads Mail's local **Envelope Index** SQLite database directly (no IMAP, no
credentials, no network, no Apple Events). This is instant even on inboxes with
tens of thousands of messages, unlike scripting Mail's unified inbox.

Returns sender, subject, date and read/unread status; can optionally include the
short body snippet Mail caches. Never sends, deletes, or modifies anything —
the database is opened read-only/immutable.

Note: the index lives under ~/Library/Mail, which is protected by macOS. The
OpenJarvis process needs Full Disk Access (System Settings → Privacy & Security
→ Full Disk Access) to read it; otherwise opening the file fails.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec


def _find_envelope_index() -> Optional[Path]:
    """Locate the newest Mail Envelope Index DB under ~/Library/Mail."""
    base = Path.home() / "Library" / "Mail"
    candidates = sorted(
        base.glob("V*/MailData/Envelope Index"),
        key=lambda p: p.parent.parent.name,  # V10 > V9 ...
        reverse=True,
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


@ToolRegistry.register("mail_read")
class MailReadTool(BaseTool):
    """Read recent messages from the native macOS Mail.app (read-only)."""

    tool_id = "mail_read"
    is_local = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="mail_read",
            description=(
                "Read recent emails from the native macOS Mail app (Apple Mail), "
                "read-only. Lists sender, subject, date and read/unread status, "
                "with an optional body snippet. Supports filtering to unread only "
                "and searching by sender or subject. Use for 'read/check my "
                "email', 'any new mail?', 'emails from X', 'unread emails'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max messages to return (1-50, default 10).",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only return unread messages. Default false.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional text to match in sender or subject "
                            "(case-insensitive)."
                        ),
                    },
                    "include_body": {
                        "type": "boolean",
                        "description": (
                            "Include the cached body snippet (~400 chars). "
                            "Default false."
                        ),
                    },
                    "mailbox": {
                        "type": "string",
                        "description": (
                            "'inbox' (default, all accounts' inboxes unified) or "
                            "'all' to include every mailbox."
                        ),
                    },
                },
                "required": [],
            },
            category="productivity",
            timeout_seconds=20.0,
            latency_estimate=0.5,
        )

    def _build_query(
        self,
        *,
        count: int,
        unread_only: bool,
        query: str,
        include_body: bool,
        mailbox: str,
    ) -> Tuple[str, list]:
        body_select = ", su.summary" if include_body else ", NULL"
        body_join = (
            " LEFT JOIN summaries su ON su.ROWID = m.summary" if include_body else ""
        )
        sql = (
            "SELECT m.read, m.date_received, m.subject_prefix, s.subject, "
            "COALESCE(NULLIF(a.comment, ''), a.address)" + body_select + " "
            "FROM messages m "
            "LEFT JOIN addresses a ON a.ROWID = m.sender "
            "LEFT JOIN subjects s ON s.ROWID = m.subject "
            "LEFT JOIN mailboxes mb ON mb.ROWID = m.mailbox" + body_join + " "
            "WHERE m.deleted = 0"
        )
        args: list = []
        if mailbox.strip().lower() != "all":
            sql += " AND mb.url LIKE '%/INBOX'"
        if unread_only:
            sql += " AND m.read = 0"
        if query:
            sql += " AND (s.subject LIKE ? OR a.address LIKE ? OR a.comment LIKE ?)"
            like = f"%{query}%"
            args += [like, like, like]
        sql += " ORDER BY m.date_received DESC LIMIT ?"
        args.append(count)
        return sql, args

    def execute(self, **params: Any) -> ToolResult:
        try:
            count = int(params.get("count") or 10)
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(count, 50))
        unread_only = bool(params.get("unread_only", False))
        query = (params.get("query") or "").strip()
        include_body = bool(params.get("include_body", False))
        mailbox = (params.get("mailbox") or "inbox").strip()

        db = _find_envelope_index()
        if db is None:
            return ToolResult(
                tool_name="mail_read",
                content=(
                    "Could not find the Apple Mail database under ~/Library/Mail. "
                    "Is Apple Mail set up on this Mac?"
                ),
                success=False,
            )

        sql, args = self._build_query(
            count=count,
            unread_only=unread_only,
            query=query,
            include_body=include_body,
            mailbox=mailbox,
        )

        try:
            conn = sqlite3.connect(
                f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5.0
            )
            try:
                rows = conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            hint = ""
            if "unable to open" in str(exc).lower() or "authoriz" in str(exc).lower():
                hint = (
                    " — grant Full Disk Access to the OpenJarvis process under "
                    "System Settings → Privacy & Security → Full Disk Access."
                )
            return ToolResult(
                tool_name="mail_read",
                content=f"Could not read Mail database: {exc}{hint}",
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="mail_read",
                content=f"Mail read error: {exc}",
                success=False,
            )

        if not rows:
            scope = "unread " if unread_only else ""
            extra = f" matching '{query}'" if query else ""
            where = "inbox" if mailbox.lower() != "all" else "Mail"
            return ToolResult(
                tool_name="mail_read",
                content=f"No {scope}messages found in {where}{extra}.",
                success=True,
            )

        lines: List[str] = []
        for n, row in enumerate(rows, start=1):
            read_flag, date_epoch, prefix, subject, sender, snippet = row
            marker = "○" if read_flag else "● NUEVO"
            try:
                when = datetime.fromtimestamp(int(date_epoch)).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except (TypeError, ValueError, OSError):
                when = "?"
            subj = ((prefix or "") + (subject or "")).strip() or "(sin asunto)"
            sender = (sender or "(desconocido)").strip()
            lines.append(f"{n}. {marker} | {when}\n   De: {sender}\n   Asunto: {subj}")
            if include_body and snippet:
                body = " ".join(str(snippet).split())[:400]
                if body:
                    lines.append(f"   {body}")

        scope = "no leídos" if unread_only else "recientes"
        where = "inbox" if mailbox.lower() != "all" else "todos los buzones"
        header_q = f" (filtro: '{query}')" if query else ""
        header = f"{len(rows)} correo(s) {scope} en {where}{header_q}:"
        return ToolResult(
            tool_name="mail_read",
            content=header + "\n\n" + "\n".join(lines),
            success=True,
        )


__all__ = ["MailReadTool"]
