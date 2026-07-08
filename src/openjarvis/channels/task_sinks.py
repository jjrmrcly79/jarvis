"""Downstream sinks for extracted tasks/meetings.

A *sink* takes an :class:`~openjarvis.channels.task_extraction.ExtractedItem`
and delivers it somewhere the user actually looks:

- :class:`AppleRemindersSink` targets macOS ``Reminders.app`` via ``osascript``
  — the surface the HUD reads for its agenda/reminders panels.
- :class:`ObsidianTasksSink` appends ``- [ ] …`` checkboxes to a note in an
  Obsidian vault — the surface the HUD's pending-task scanner reads.

Both share the same call signature so they can be combined with
:func:`combine_sinks`.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from openjarvis.channels.task_extraction import ExtractedItem, Sink

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
    kinds:
        Restrict to these item kinds (e.g. ``("meeting",)``).  ``None`` accepts
        both tasks and meetings.

    On non-macOS systems the sink is a no-op (logs a debug message).
    """

    def __init__(
        self,
        list_name: str = "WhatsApp",
        *,
        default_due_hour: int = 9,
        kinds: Optional[Sequence[str]] = None,
    ) -> None:
        self._list = list_name
        self._default_due_hour = default_due_hour
        self._kinds = tuple(kinds) if kinds else None

    @property
    def available(self) -> bool:
        """True on macOS, where ``osascript``/Reminders.app exist."""
        return platform.system() == "Darwin"

    def __call__(self, item: ExtractedItem) -> None:
        """Create a reminder for *item* (no-op off macOS)."""
        if self._kinds is not None and item.kind not in self._kinds:
            return
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


# ---------------------------------------------------------------------------
# Obsidian
# ---------------------------------------------------------------------------

# Standard iCloud location for an Obsidian vault (``~`` expands per user).
_DEFAULT_VAULT = (
    Path.home() / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents"
)

# Matches an existing open checkbox line: ``- [ ] some text``.
_OPEN_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[ \]\s*(.+?)\s*$")
# Trailing due-date marker the HUD understands: ``📅 2026-07-08``.
_DUE_MARKER_RE = re.compile(r"\s*(?:📅|due:?|vence:?)\s*\d{4}-\d{2}-\d{2}\s*$", re.I)


def _resolve_vault(vault_path: Optional[str]) -> Path:
    """Resolve the vault directory: explicit arg > ``VAULT`` env > iCloud default."""
    if vault_path:
        return Path(vault_path).expanduser()
    env = os.environ.get("VAULT", "")
    if env:
        return Path(env).expanduser()
    return _DEFAULT_VAULT


class ObsidianTasksSink:
    """Append extracted *tasks* as ``- [ ]`` checkboxes to an Obsidian note.

    Meetings are ignored (route those to a calendar/reminders sink).  The note
    is created if missing.  The line format — ``- [ ] Title 📅 YYYY-MM-DD`` — is
    exactly what the HUD's pending-task scanner reads, so tasks land in the
    "pendientes" panel.

    Parameters
    ----------
    vault_path:
        Vault directory.  Falls back to the ``VAULT`` env var, then the standard
        iCloud Obsidian path.
    note:
        Note (relative to the vault) to append to.  Defaults to
        ``"Bandeja de WhatsApp.md"``.
    dedupe:
        Skip appending when an open checkbox with the same title already exists
        in the note.
    """

    def __init__(
        self,
        vault_path: Optional[str] = None,
        *,
        note: str = "Bandeja de WhatsApp.md",
        dedupe: bool = True,
    ) -> None:
        self._vault = _resolve_vault(vault_path)
        self._note = note
        self._dedupe = dedupe

    @property
    def available(self) -> bool:
        """True when the vault directory exists."""
        return self._vault.is_dir()

    def _target(self) -> Optional[Path]:
        """Resolve the note path, ensuring it stays inside the vault."""
        target = (self._vault / self._note).resolve()
        try:
            target.relative_to(self._vault.resolve())
        except ValueError:
            logger.warning("ObsidianTasksSink: note escapes the vault: %s", self._note)
            return None
        return target

    def __call__(self, item: ExtractedItem) -> None:
        """Append *item* as a checkbox (tasks only; no-op if vault missing)."""
        if item.kind != "task":
            return
        if not self.available:
            logger.debug(
                "ObsidianTasksSink skipped: vault not found at %s", self._vault
            )
            return

        target = self._target()
        if target is None:
            return

        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
        except OSError:
            logger.warning("ObsidianTasksSink: could not read %s", target)
            existing = ""

        if self._dedupe and self._already_present(existing, item.title):
            return

        line = self._format(item)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(line + "\n")
        except OSError:
            logger.warning("ObsidianTasksSink: could not write %s", target)

    @staticmethod
    def _format(item: ExtractedItem) -> str:
        """Render an item as an Obsidian task line."""
        line = f"- [ ] {item.title}"
        date = _date_only(item.due)
        if date:
            line += f" 📅 {date}"
        return line

    @staticmethod
    def _already_present(existing: str, title: str) -> bool:
        """True when an open checkbox with the same title already exists."""
        want = title.strip().lower()
        for raw in existing.splitlines():
            m = _OPEN_CHECKBOX_RE.match(raw)
            if not m:
                continue
            text = _DUE_MARKER_RE.sub("", m.group(1)).strip().lower()
            if text == want:
                return True
        return False


def _date_only(due: Optional[str]) -> Optional[str]:
    """Return the ``YYYY-MM-DD`` portion of a due string, or None."""
    dt = _parse_due(due)
    return dt.strftime("%Y-%m-%d") if dt is not None else None


# ---------------------------------------------------------------------------
# Apple Calendar
# ---------------------------------------------------------------------------

# AppleScript: create a timed event in a named calendar, falling back to the
# first calendar when the named one does not exist (calendar names are
# localized, e.g. "Calendar" vs "Calendario").
_CAL_CREATE_SCRIPT = """on run argv
  set calName to item 1 of argv
  set evSummary to item 2 of argv
  set yr to (item 3 of argv) as integer
  set mo to (item 4 of argv) as integer
  set dy to (item 5 of argv) as integer
  set hr to (item 6 of argv) as integer
  set mi to (item 7 of argv) as integer
  set durMin to (item 8 of argv) as integer
  set evNotes to item 9 of argv
  set startDate to current date
  set day of startDate to 1
  set year of startDate to yr
  set month of startDate to mo
  set day of startDate to dy
  set hours of startDate to hr
  set minutes of startDate to mi
  set seconds of startDate to 0
  set endDate to startDate + (durMin * minutes)
  tell application "Calendar"
    if (exists calendar calName) then
      set tgt to calendar calName
    else
      set tgt to item 1 of calendars
    end if
    tell tgt
      set newEv to make new event with properties {summary:evSummary}
      set start date of newEv to startDate
      set end date of newEv to endDate
      if evNotes is not "" then set description of newEv to evNotes
    end tell
  end tell
end run"""


class AppleCalendarSink:
    """Create macOS Calendar events for extracted *meetings*.

    Tasks are ignored (route those to Reminders/Obsidian).  A meeting with no
    parseable date is skipped (a calendar event needs a date).

    Parameters
    ----------
    calendar_name:
        Target calendar.  Falls back to the first calendar when it does not
        exist (names are localized).
    default_hour:
        Hour to use when a meeting has a date but no time.
    duration_min:
        Event length in minutes.

    No-op on non-macOS systems.
    """

    def __init__(
        self,
        calendar_name: str = "Calendario",
        *,
        default_hour: int = 9,
        duration_min: int = 60,
    ) -> None:
        self._calendar = calendar_name
        self._default_hour = default_hour
        self._duration_min = duration_min

    @property
    def available(self) -> bool:
        """True on macOS, where ``osascript``/Calendar.app exist."""
        return platform.system() == "Darwin"

    def __call__(self, item: ExtractedItem) -> None:
        """Create a calendar event for a meeting (no-op otherwise/off macOS)."""
        if item.kind != "meeting":
            return
        if not self.available:
            logger.debug("AppleCalendarSink skipped: not macOS")
            return

        start = _parse_due(item.due)
        if start is None:
            logger.debug("AppleCalendarSink: meeting without a date, skipping")
            return
        if start.hour == 0 and start.minute == 0:
            start = start.replace(hour=self._default_hour)

        notes_parts = []
        if item.participants:
            notes_parts.append("Con: " + ", ".join(item.participants))
        if item.notes:
            notes_parts.append(item.notes)
        if item.source_sender:
            notes_parts.append(f"(via {item.source_channel} — {item.source_sender})")
        notes = "\n".join(notes_parts)

        args = [
            self._calendar,
            item.title,
            str(start.year),
            str(start.month),
            str(start.day),
            str(start.hour),
            str(start.minute),
            str(self._duration_min),
            notes,
        ]
        try:
            proc = subprocess.run(
                ["osascript", "-e", _CAL_CREATE_SCRIPT, *args],
                capture_output=True,
                text=True,
                timeout=40,
            )
        except Exception:
            logger.exception("AppleCalendarSink: osascript failed")
            return
        if proc.returncode != 0:
            logger.warning(
                "AppleCalendarSink: Calendar.app error: %s",
                (proc.stderr or "").strip(),
            )


def combine_sinks(*sinks: Optional[Sink]) -> Optional[Sink]:
    """Combine several sinks into one that fans out to each.

    ``None`` entries are dropped.  Each sink is guarded independently so one
    failing sink does not prevent the others from running.  Returns ``None``
    when no sinks remain, or the single sink when only one is given.
    """
    active = [s for s in sinks if s is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def _fan_out(item: ExtractedItem) -> None:
        for sink in active:
            try:
                sink(item)
            except Exception:
                logger.exception("combine_sinks: a sink failed")

    return _fan_out


__all__ = [
    "AppleCalendarSink",
    "AppleRemindersSink",
    "ObsidianTasksSink",
    "combine_sinks",
]
