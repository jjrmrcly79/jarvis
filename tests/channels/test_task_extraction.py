"""Tests for MessageTaskExtractor and the extracted-items store."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from openjarvis.channels._stubs import ChannelMessage
from openjarvis.channels.task_extraction import (
    ExtractedItem,
    MessageTaskExtractor,
    load_items,
)


class FakeEngine:
    """Minimal engine stub returning a canned ``content`` string."""

    def __init__(self, content="", *, raises=False):
        self._content = content
        self._raises = raises
        self.calls = []

    def generate(self, messages, *, model, **kwargs):
        self.calls.append({"messages": messages, "model": model, "kwargs": kwargs})
        if self._raises:
            raise RuntimeError("engine boom")
        return {"content": self._content}


_FIXED_NOW = lambda: datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)  # noqa: E731


def _extractor(content, *, raises=False, store_path=None, sink=None):
    return MessageTaskExtractor(
        FakeEngine(content, raises=raises),
        model="test-model",
        store_path=store_path,
        sink=sink,
        now_fn=_FIXED_NOW,
    )


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------


class TestExtract:
    def test_parses_items(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "kind": "task",
                        "title": "Llamar al banco",
                        "due": "2026-07-08",
                        "participants": [],
                        "notes": "sobre la tarjeta",
                    }
                ]
            }
        )
        items = _extractor(content).extract(
            "Recuérdame llamar al banco mañana", channel="whatsapp", sender="Ana"
        )
        assert len(items) == 1
        it = items[0]
        assert it.kind == "task"
        assert it.title == "Llamar al banco"
        assert it.due == "2026-07-08"
        assert it.notes == "sobre la tarjeta"
        assert it.source_channel == "whatsapp"
        assert it.source_sender == "Ana"
        assert it.created_at == _FIXED_NOW().isoformat()

    def test_meeting_with_participants(self):
        content = json.dumps(
            {
                "items": [
                    {
                        "kind": "meeting",
                        "title": "Junta de proyecto",
                        "due": "2026-07-10T10:00",
                        "participants": ["Ana", "Luis"],
                    }
                ]
            }
        )
        items = _extractor(content).extract("Junta con Ana y Luis el viernes 10am")
        assert items[0].kind == "meeting"
        assert items[0].participants == ["Ana", "Luis"]
        assert items[0].due == "2026-07-10T10:00"

    def test_empty_message_returns_empty(self):
        eng = _extractor("whatever")
        assert eng.extract("   ") == []

    def test_no_items(self):
        assert _extractor(json.dumps({"items": []})).extract("hola") == []

    def test_code_fence_wrapped(self):
        content = '```json\n{"items": [{"kind": "task", "title": "Comprar pan"}]}\n```'
        items = _extractor(content).extract("compra pan")
        assert len(items) == 1
        assert items[0].title == "Comprar pan"

    def test_bare_list(self):
        content = json.dumps([{"kind": "task", "title": "Enviar correo"}])
        items = _extractor(content).extract("manda el correo")
        assert len(items) == 1
        assert items[0].title == "Enviar correo"

    def test_prose_around_json(self):
        content = 'Sure! Here you go:\n{"items": [{"kind":"task","title":"X"}]} done'
        items = _extractor(content).extract("hazlo")
        assert len(items) == 1
        assert items[0].title == "X"

    def test_malformed_json_returns_empty(self):
        assert _extractor("not json at all").extract("hola") == []

    def test_generate_exception_returns_empty(self):
        assert _extractor("", raises=True).extract("hola") == []

    def test_invalid_kind_defaults_to_task(self):
        content = json.dumps({"items": [{"kind": "nonsense", "title": "T"}]})
        items = _extractor(content).extract("x")
        assert items[0].kind == "task"

    def test_participants_string_is_coerced(self):
        content = json.dumps(
            {"items": [{"kind": "task", "title": "T", "participants": "Ana"}]}
        )
        assert _extractor(content).extract("x")[0].participants == ["Ana"]

    def test_item_without_title_skipped(self):
        content = json.dumps(
            {"items": [{"kind": "task", "title": ""}, {"kind": "task", "title": "Ok"}]}
        )
        items = _extractor(content).extract("x")
        assert len(items) == 1
        assert items[0].title == "Ok"

    def test_null_due_normalised(self):
        content = json.dumps({"items": [{"kind": "task", "title": "T", "due": "null"}]})
        assert _extractor(content).extract("x")[0].due is None

    def test_response_format_requested(self):
        eng = FakeEngine(json.dumps({"items": []}))
        MessageTaskExtractor(eng, model="m", now_fn=_FIXED_NOW).extract("hola")
        assert "response_format" in eng.calls[0]["kwargs"]


# ---------------------------------------------------------------------------
# process() — persistence + sink
# ---------------------------------------------------------------------------


class TestProcess:
    def test_persists_and_returns(self, tmp_path):
        store = tmp_path / "items.jsonl"
        content = json.dumps({"items": [{"kind": "task", "title": "Pagar renta"}]})
        ext = _extractor(content, store_path=store)
        msg = ChannelMessage(
            channel="whatsapp_baileys",
            sender="Casero",
            content="paga la renta",
            message_id="m1",
        )
        items = ext.process(msg)
        assert len(items) == 1

        loaded = load_items(store)
        assert len(loaded) == 1
        assert loaded[0].title == "Pagar renta"
        assert loaded[0].source_message_id == "m1"

    def test_sink_invoked_per_item(self, tmp_path):
        store = tmp_path / "items.jsonl"
        received = []
        content = json.dumps(
            {
                "items": [
                    {"kind": "task", "title": "A"},
                    {"kind": "meeting", "title": "B"},
                ]
            }
        )
        ext = _extractor(content, store_path=store, sink=received.append)
        ext.process(ChannelMessage(channel="c", sender="s", content="do A and B"))
        assert [i.title for i in received] == ["A", "B"]

    def test_sink_exception_does_not_crash(self, tmp_path):
        store = tmp_path / "items.jsonl"

        def bad_sink(_item):
            raise ValueError("boom")

        content = json.dumps({"items": [{"kind": "task", "title": "A"}]})
        ext = _extractor(content, store_path=store, sink=bad_sink)
        # Must not raise even though the sink does.
        items = ext.process(ChannelMessage(channel="c", sender="s", content="x"))
        assert len(items) == 1

    def test_no_items_no_file(self, tmp_path):
        store = tmp_path / "items.jsonl"
        ext = _extractor(json.dumps({"items": []}), store_path=store)
        ext.process(ChannelMessage(channel="c", sender="s", content="hola"))
        assert not store.exists()


# ---------------------------------------------------------------------------
# load_items()
# ---------------------------------------------------------------------------


class TestLoadItems:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_items(tmp_path / "nope.jsonl") == []

    def test_skips_malformed_lines(self, tmp_path):
        store = tmp_path / "items.jsonl"
        good = ExtractedItem(kind="task", title="Real")
        store.write_text(
            "not json\n" + json.dumps(good.to_json()) + "\n" + "\n",  # blank line
            encoding="utf-8",
        )
        loaded = load_items(store)
        assert len(loaded) == 1
        assert loaded[0].title == "Real"

    def test_tolerates_extra_keys(self, tmp_path):
        store = tmp_path / "items.jsonl"
        payload = ExtractedItem(kind="task", title="T").to_json()
        payload["unexpected"] = "field"
        store.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        loaded = load_items(store)
        assert len(loaded) == 1
        assert loaded[0].title == "T"
