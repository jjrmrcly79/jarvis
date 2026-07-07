"""Extract actionable tasks and meetings from incoming channel messages.

Most of a user's real to-dos arrive as chat messages ("recuérdame llamar al
banco mañana", "junta con Ana el viernes 10am").  :class:`MessageTaskExtractor`
runs an incoming :class:`~openjarvis.channels._stubs.ChannelMessage` through the
configured LLM engine and turns it into zero or more structured
:class:`ExtractedItem` records, which are appended to a JSONL store so other
surfaces (the HUD, a CLI, an Apple Reminders sink) can pick them up.

The extractor is deliberately platform-neutral: it only depends on the engine
abstraction and a JSONL file.  Downstream delivery (e.g. creating an Apple
Reminder) is handled by an optional ``sink`` callback so the core stays portable
and easy to test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from openjarvis.channels._stubs import ChannelMessage
from openjarvis.core.types import Message, Role
from openjarvis.engine._stubs import InferenceEngine, ResponseFormat

logger = logging.getLogger(__name__)

# Where extracted items are persisted by default.
_DEFAULT_STORE = Path.home() / ".openjarvis" / "extracted_tasks.jsonl"

# Valid item kinds.
_KINDS = ("task", "meeting")

# JSON schema advertised to engines that support structured output.
EXTRACTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "title": {"type": "string"},
                    "due": {"type": ["string", "null"]},
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["kind", "title"],
            },
        }
    },
    "required": ["items"],
}


@dataclass(slots=True)
class ExtractedItem:
    """A single task or meeting extracted from a message."""

    kind: str  # "task" | "meeting"
    title: str
    due: Optional[str] = None  # ISO-8601 date or datetime, or None
    participants: List[str] = field(default_factory=list)
    notes: str = ""
    source_channel: str = ""
    source_sender: str = ""
    source_message_id: str = ""
    source_text: str = ""
    created_at: str = ""

    def to_json(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict for persistence."""
        return asdict(self)


# A sink receives each newly extracted item for downstream delivery (e.g.
# creating an Apple Reminder).  It must not raise; the extractor guards it.
Sink = Callable[[ExtractedItem], None]


def default_store_path() -> Path:
    """Return the default JSONL store path for extracted items."""
    return _DEFAULT_STORE


def load_items(store_path: Optional[Path] = None) -> List[ExtractedItem]:
    """Read all extracted items from *store_path* (newest last).

    Malformed lines are skipped.  Returns an empty list if the file is absent.
    """
    path = store_path or _DEFAULT_STORE
    if not path.is_file():
        return []
    items: List[ExtractedItem] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read extracted-items store: %s", path)
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            items.append(ExtractedItem(**data))
        except TypeError:
            # Unknown/extra keys — keep only the known fields.
            known = {
                k: data[k]
                for k in (
                    "kind",
                    "title",
                    "due",
                    "participants",
                    "notes",
                    "source_channel",
                    "source_sender",
                    "source_message_id",
                    "source_text",
                    "created_at",
                )
                if k in data
            }
            try:
                items.append(ExtractedItem(**known))
            except TypeError:
                continue
    return items


_SYSTEM_PROMPT = """\
You extract actionable TASKS and MEETINGS from a single chat message.

Rules:
- Return ONLY genuinely actionable items the message asks for or implies. If the
  message is small talk, an acknowledgement, or has nothing to do, return an
  empty list.
- "meeting" = an appointment/call with a time or a clear intent to meet.
  Everything else actionable is a "task".
- "title" must be a short imperative phrase in the SAME language as the message.
- "due": resolve relative dates ("mañana", "el viernes", "next week") against the
  provided current date. Use ISO-8601: "YYYY-MM-DD" for a date, or
  "YYYY-MM-DDTHH:MM" when a time is given. Use null if there is no due date.
- "participants": people named as involved (may be empty).
- "notes": short extra context, or null.

Respond with a JSON object of exactly this shape and nothing else:
{"items": [{"kind": "task"|"meeting", "title": str, "due": str|null,
            "participants": [str], "notes": str|null}]}
"""


class MessageTaskExtractor:
    """Turn incoming messages into structured tasks/meetings via the LLM.

    Parameters
    ----------
    engine:
        A concrete :class:`~openjarvis.engine._stubs.InferenceEngine`.
    model:
        Model identifier to run on *engine*.
    store_path:
        JSONL file to append extracted items to.  Defaults to
        ``~/.openjarvis/extracted_tasks.jsonl``.
    sink:
        Optional callback invoked once per newly extracted item (e.g. to create
        an Apple Reminder).  Exceptions raised by the sink are logged, not
        propagated.
    temperature / max_tokens:
        Generation parameters passed to the engine.
    now_fn:
        Callable returning the current :class:`datetime` (injectable for tests).
    """

    def __init__(
        self,
        engine: InferenceEngine,
        *,
        model: str,
        store_path: Optional[Path] = None,
        sink: Optional[Sink] = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._engine = engine
        self._model = model
        self._store_path = store_path or _DEFAULT_STORE
        self._sink = sink
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._now_fn = now_fn or (lambda: datetime.now(tz=timezone.utc))

    # -- public API ------------------------------------------------------------

    def extract(
        self,
        text: str,
        *,
        channel: str = "",
        sender: str = "",
        message_id: str = "",
    ) -> List[ExtractedItem]:
        """Extract tasks/meetings from *text* without persisting them."""
        text = (text or "").strip()
        if not text:
            return []

        now = self._now_fn()
        messages: Sequence[Message] = [
            Message(role=Role.SYSTEM, content=_SYSTEM_PROMPT),
            Message(
                role=Role.USER,
                content=(
                    f"Current date/time: {now.isoformat()}\n"
                    f"Message from {sender or 'unknown'} on {channel or 'chat'}:\n"
                    f"{text}"
                ),
            ),
        ]

        try:
            result = self._engine.generate(
                messages,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format=ResponseFormat(type="json_object"),
            )
        except Exception:
            logger.exception("Task extraction generate() failed")
            return []

        raw = result.get("content", "") if isinstance(result, dict) else ""
        parsed = _parse_items(raw)

        created = now.isoformat()
        items: List[ExtractedItem] = []
        for entry in parsed:
            item = _to_item(
                entry,
                channel=channel,
                sender=sender,
                message_id=message_id,
                source_text=text,
                created_at=created,
            )
            if item is not None:
                items.append(item)
        return items

    def process(self, message: ChannelMessage) -> List[ExtractedItem]:
        """Extract from *message*, persist, dispatch to the sink, and return."""
        items = self.extract(
            message.content,
            channel=message.channel,
            sender=message.sender,
            message_id=message.message_id,
        )
        if items:
            self._persist(items)
            if self._sink is not None:
                for item in items:
                    try:
                        self._sink(item)
                    except Exception:
                        logger.exception("Extracted-item sink failed")
        return items

    # -- internals -------------------------------------------------------------

    def _persist(self, items: Sequence[ExtractedItem]) -> None:
        """Append *items* to the JSONL store (best-effort)."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._store_path, "a", encoding="utf-8") as fh:
                for item in items:
                    fh.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("Could not persist extracted items to %s", self._store_path)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_items(raw: str) -> List[Dict[str, Any]]:
    """Parse the model output into a list of raw item dicts.

    Tolerates code fences and stray prose around the JSON payload, and accepts
    either ``{"items": [...]}`` or a bare ``[...]`` list.
    """
    text = (raw or "").strip()
    if not text:
        return []

    # Strip Markdown code fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        # Drop an optional leading language tag (e.g. "json\n{...}").
        newline = text.find("\n")
        if newline != -1 and " " not in text[:newline]:
            text = text[newline + 1 :]

    data = _loads_lenient(text)
    if data is None:
        return []

    if isinstance(data, dict):
        items = data.get("items", [])
    elif isinstance(data, list):
        items = data
    else:
        return []

    return [it for it in items if isinstance(it, dict)]


def _loads_lenient(text: str) -> Any:
    """Best-effort JSON parse: try whole string, then the first {...}/[...]."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced-looking JSON object/array substring.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _to_item(
    entry: Dict[str, Any],
    *,
    channel: str,
    sender: str,
    message_id: str,
    source_text: str,
    created_at: str,
) -> Optional[ExtractedItem]:
    """Validate and normalise a single raw item dict into an ExtractedItem."""
    title = str(entry.get("title", "")).strip()
    if not title:
        return None

    kind = str(entry.get("kind", "task")).strip().lower()
    if kind not in _KINDS:
        kind = "task"

    due_raw = entry.get("due")
    due = str(due_raw).strip() if due_raw else None
    if due in ("", "null", "none"):
        due = None

    participants_raw = entry.get("participants") or []
    if isinstance(participants_raw, str):
        participants = [participants_raw.strip()] if participants_raw.strip() else []
    elif isinstance(participants_raw, list):
        participants = [str(p).strip() for p in participants_raw if str(p).strip()]
    else:
        participants = []

    notes_raw = entry.get("notes")
    notes = str(notes_raw).strip() if notes_raw else ""

    return ExtractedItem(
        kind=kind,
        title=title,
        due=due,
        participants=participants,
        notes=notes,
        source_channel=channel,
        source_sender=sender,
        source_message_id=message_id,
        source_text=source_text,
        created_at=created_at,
    )


__all__ = [
    "EXTRACTION_SCHEMA",
    "ExtractedItem",
    "MessageTaskExtractor",
    "Sink",
    "default_store_path",
    "load_items",
]
