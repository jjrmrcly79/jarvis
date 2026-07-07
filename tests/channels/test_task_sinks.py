"""Tests for the AppleRemindersSink."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from openjarvis.channels.task_extraction import ExtractedItem
from openjarvis.channels.task_sinks import (
    AppleRemindersSink,
    ObsidianTasksSink,
    combine_sinks,
)


def _item(**kw):
    base = dict(kind="task", title="Llamar al banco")
    base.update(kw)
    return ExtractedItem(**base)


class TestAvailability:
    def test_available_true_on_darwin(self):
        sink = AppleRemindersSink()
        with patch("platform.system", return_value="Darwin"):
            assert sink.available is True

    def test_available_false_off_darwin(self):
        sink = AppleRemindersSink()
        with patch("platform.system", return_value="Linux"):
            assert sink.available is False


class TestCall:
    def test_noop_off_macos(self):
        sink = AppleRemindersSink()
        with (
            patch("platform.system", return_value="Linux"),
            patch("subprocess.run") as run,
        ):
            sink(_item())
        run.assert_not_called()

    def test_creates_reminder_with_due(self):
        sink = AppleRemindersSink(list_name="WhatsApp")
        proc = MagicMock(returncode=0, stdout="WhatsApp", stderr="")
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", return_value=proc) as run,
        ):
            sink(_item(due="2026-07-08T15:30", participants=["Ana"]))

        run.assert_called_once()
        args = run.call_args[0][0]
        assert args[0] == "osascript"
        # Positional script args: list, name, body, has_due, y, m, d, h, min
        assert args[3] == "WhatsApp"  # list name
        assert "Llamar al banco" in args[4]  # reminder name
        assert args[6] == "1"  # has_due flag
        assert args[7:] == ["2026", "7", "8", "15", "30"]

    def test_date_only_due_uses_default_hour(self):
        sink = AppleRemindersSink(default_due_hour=8)
        proc = MagicMock(returncode=0, stdout="WhatsApp", stderr="")
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", return_value=proc) as run,
        ):
            sink(_item(due="2026-07-08"))
        args = run.call_args[0][0]
        # hour defaulted to 8, minute 0
        assert args[7:] == ["2026", "7", "8", "8", "0"]

    def test_meeting_title_prefixed(self):
        sink = AppleRemindersSink()
        proc = MagicMock(returncode=0, stdout="WhatsApp", stderr="")
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", return_value=proc) as run,
        ):
            sink(_item(kind="meeting", title="con el cliente"))
        name = run.call_args[0][0][4]
        assert name.startswith("Reunión:")

    def test_no_due_sets_flag_zero(self):
        sink = AppleRemindersSink()
        proc = MagicMock(returncode=0, stdout="WhatsApp", stderr="")
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", return_value=proc) as run,
        ):
            sink(_item(due=None))
        assert run.call_args[0][0][6] == "0"  # has_due

    def test_osascript_failure_does_not_raise(self):
        sink = AppleRemindersSink()
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", side_effect=OSError("boom")),
        ):
            # Should swallow the error.
            sink(_item())

    def test_kinds_filter_skips_other_kinds(self):
        sink = AppleRemindersSink(kinds=("meeting",))
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run") as run,
        ):
            sink(_item(kind="task"))  # filtered out
        run.assert_not_called()

    def test_kinds_filter_allows_matching_kind(self):
        sink = AppleRemindersSink(kinds=("meeting",))
        proc = MagicMock(returncode=0, stdout="WhatsApp", stderr="")
        with (
            patch("platform.system", return_value="Darwin"),
            patch("subprocess.run", return_value=proc) as run,
        ):
            sink(_item(kind="meeting", title="Junta"))
        run.assert_called_once()


class TestObsidianTasksSink:
    def test_available_reflects_vault_dir(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path))
        assert sink.available is True
        missing = ObsidianTasksSink(str(tmp_path / "nope"))
        assert missing.available is False

    def test_appends_task_checkbox(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Pagar renta", due="2026-07-08"))
        content = (tmp_path / "inbox.md").read_text(encoding="utf-8")
        assert content == "- [ ] Pagar renta 📅 2026-07-08\n"

    def test_datetime_due_uses_date_only(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Enviar reporte", due="2026-07-08T15:30"))
        content = (tmp_path / "inbox.md").read_text(encoding="utf-8")
        assert "📅 2026-07-08" in content
        assert "15:30" not in content

    def test_no_due_omits_marker(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Sin fecha", due=None))
        content = (tmp_path / "inbox.md").read_text(encoding="utf-8")
        assert content == "- [ ] Sin fecha\n"

    def test_meetings_are_ignored(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(kind="meeting", title="Junta"))
        assert not (tmp_path / "inbox.md").exists()

    def test_noop_when_vault_missing(self, tmp_path):
        sink = ObsidianTasksSink(str(tmp_path / "missing"), note="inbox.md")
        sink(_item(title="X"))
        assert not (tmp_path / "missing").exists()

    def test_appends_to_existing_note(self, tmp_path):
        note = tmp_path / "inbox.md"
        note.write_text("# Bandeja\n\n- [ ] Vieja\n", encoding="utf-8")
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Nueva"))
        content = note.read_text(encoding="utf-8")
        assert content == "# Bandeja\n\n- [ ] Vieja\n- [ ] Nueva\n"

    def test_adds_newline_when_file_lacks_trailing(self, tmp_path):
        note = tmp_path / "inbox.md"
        note.write_text("- [ ] Vieja", encoding="utf-8")  # no trailing newline
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Nueva"))
        content = note.read_text(encoding="utf-8")
        assert content == "- [ ] Vieja\n- [ ] Nueva\n"

    def test_dedupe_skips_existing_title(self, tmp_path):
        note = tmp_path / "inbox.md"
        note.write_text("- [ ] Pagar renta 📅 2026-07-08\n", encoding="utf-8")
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md")
        sink(_item(title="Pagar renta", due="2026-07-09"))  # same title, new date
        content = note.read_text(encoding="utf-8")
        assert content.count("Pagar renta") == 1

    def test_dedupe_can_be_disabled(self, tmp_path):
        note = tmp_path / "inbox.md"
        note.write_text("- [ ] Pagar renta\n", encoding="utf-8")
        sink = ObsidianTasksSink(str(tmp_path), note="inbox.md", dedupe=False)
        sink(_item(title="Pagar renta"))
        content = note.read_text(encoding="utf-8")
        assert content.count("Pagar renta") == 2

    def test_note_escaping_vault_is_rejected(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        outside = tmp_path / "outside.md"
        sink = ObsidianTasksSink(str(vault), note="../outside.md")
        sink(_item(title="X"))
        assert not outside.exists()

    def test_uses_vault_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VAULT", str(tmp_path))
        sink = ObsidianTasksSink(note="inbox.md")
        sink(_item(title="Desde env"))
        assert (tmp_path / "inbox.md").exists()


class TestCombineSinks:
    def test_none_when_empty(self):
        assert combine_sinks() is None
        assert combine_sinks(None, None) is None

    def test_single_sink_passthrough(self):
        def sink(_item):
            pass

        assert combine_sinks(sink) is sink
        assert combine_sinks(None, sink, None) is sink

    def test_fans_out_to_all(self):
        a, b = [], []
        combined = combine_sinks(a.append, b.append)
        combined(_item(title="T"))
        assert a and b

    def test_one_failing_sink_does_not_block_others(self):
        received = []

        def bad(_item):
            raise ValueError("boom")

        combined = combine_sinks(bad, received.append)
        combined(_item(title="T"))  # must not raise
        assert len(received) == 1
