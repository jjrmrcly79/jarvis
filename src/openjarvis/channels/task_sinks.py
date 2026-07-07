"""Downstream sinks for extracted tasks/meetings.

A *sink* takes an :class:`~openjarvis.channels.task_extraction.ExtractedItem`
and delivers it somewhere the user actually looks.  The Apple Reminders sink
below targets macOS ``Reminders.app`` via ``osascript`` — the same surface the
OpenJarvis HUD reads for its agenda/reminders panels — so items extracted from
WhatsApp show up there automatically.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from datetime import datetime
from typing import Optional

from openjarvis.channels.task_extraction import ExtractedItem

logger = logging.getLogger(__name__)


# AppleScript: create a reminder, optionally with a due date, in a named list
# (falling back to the default list when it does not exist).
_REM_CREATE_SCRIPT = """on run argv
  set listName to item 1 of argv
  set remName to item 2 of argv
  set remBody to item 3 of argv
  set hasDue to item 4 of argv
  tell application "Reminders"
    if (exists list listName) then
      set tgt to list listName
    else
      set tgt to default list
    end if
    set newR to make new reminder at end of tgt with properties {name:remName}
    if remBody is not "" then set body of newR to remBody
    if hasDue is "1" then
      set dd to current date
      set day of dd to 1
      set year of dd to ((item 5 of argv) as integer)
      set month of dd to ((item 6 of argv) as integer)
      set day of dd to ((item 7 of argv) as integer)
      set hours of dd to ((item 8 of argv) as integer)
      set minutes of dd to ((item 9 of argv) as integer)
      set seconds of dd to 0
      set due date of newR to dd
    end if
    return (name of tgt)
  end tell
end run"""

# Accepted due formats: date, or date+time.
_DUE_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_due(due: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-ish due string into a naive local datetime, or None."""
    if not due:
        return None
    for fmt in _DUE_FORMATS:
        try:
            return datetime.strptime(due, fmt)
        except ValueError:
            continue
    return None


class AppleRemindersSink:
    """Create macOS Reminders for extracted items.

    Parameters
    ----------
    list_name:
        Reminders list to add to.  Falls back to the system default list when it
        does not exist.
    default_due_hour:
        Hour of day to use when an item has a due *date* but no time.

    On non-macOS systems the sink is a no-op (logs a debug message).
    """

    def __init__(
        self, list_name: str = "WhatsApp", *, default_due_hour: int = 9
    ) -> None:
        self._list = list_name
        self._default_due_hour = default_due_hour

    @property
    def available(self) -> bool:
        """True on macOS, where ``osascript``/Reminders.app exist."""
        return platform.system() == "Darwin"

    def __call__(self, item: ExtractedItem) -> None:
        """Create a reminder for *item* (no-op off macOS)."""
        if not self.available:
            logger.debug("AppleRemindersSink skipped: not macOS")
            return

        due_dt = _parse_due(item.due)
        has_due = "1" if due_dt is not None else "0"
        if due_dt is not None and due_dt.hour == 0 and due_dt.minute == 0:
            due_dt = due_dt.replace(hour=self._default_due_hour)

        # Prefix meetings so they read clearly in the list.
        name = item.title
        if item.kind == "meeting" and not name.lower().startswith(
            ("junta", "reunión", "reunion", "meeting", "call", "llamada")
        ):
            name = f"Reunión: {name}"

        body_parts = []
        if item.participants:
            body_parts.append("Con: " + ", ".join(item.participants))
        if item.notes:
            body_parts.append(item.notes)
        if item.source_sender:
            body_parts.append(f"(via {item.source_channel} — {item.source_sender})")
        body = "\n".join(body_parts)

        comps = (
            [
                str(due_dt.year),
                str(due_dt.month),
                str(due_dt.day),
                str(due_dt.hour),
                str(due_dt.minute),
            ]
            if due_dt is not None
            else ["0", "0", "0", "0", "0"]
        )
        args = [self._list, name, body, has_due, *comps]

        try:
            proc = subprocess.run(
                ["osascript", "-e", _REM_CREATE_SCRIPT, *args],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except Exception:
            logger.exception("AppleRemindersSink: osascript failed")
            return
        if proc.returncode != 0:
            logger.warning(
                "AppleRemindersSink: Reminders.app error: %s",
                (proc.stderr or "").strip(),
            )


__all__ = ["AppleRemindersSink"]
