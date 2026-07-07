"""Tests for the AppleRemindersSink."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from openjarvis.channels.task_extraction import ExtractedItem
from openjarvis.channels.task_sinks import AppleRemindersSink


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
