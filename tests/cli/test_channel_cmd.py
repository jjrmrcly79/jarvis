"""Tests for the ``jarvis channel`` CLI commands."""

from __future__ import annotations

from unittest import mock

from click.testing import CliRunner

from openjarvis.channels._stubs import ChannelStatus
from openjarvis.cli import cli


def _patch_channel(
    list_channels=None,
    send_return=True,
    status_return=ChannelStatus.DISCONNECTED,
):
    """Return patches for load_config and _get_channel."""
    cfg = mock.MagicMock()
    cfg.channel.default_channel = ""

    bridge_instance = mock.MagicMock()
    bridge_instance.list_channels.return_value = list_channels or []
    bridge_instance.send.return_value = send_return
    bridge_instance.status.return_value = status_return

    config_patch = mock.patch(
        "openjarvis.core.config.load_config",
        return_value=cfg,
    )
    get_channel_patch = mock.patch(
        "openjarvis.cli.channel_cmd._get_channel",
        return_value=bridge_instance,
    )
    return config_patch, get_channel_patch, bridge_instance


class TestChannelHelp:
    def test_subcommands_in_help(self) -> None:
        result = CliRunner().invoke(cli, ["channel", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "send" in result.output
        assert "status" in result.output
        assert "connect" in result.output


class TestChannelList:
    def test_list_with_channels(self) -> None:
        config_p, getch_p, _ = _patch_channel(
            list_channels=["slack", "discord"],
        )
        with config_p, getch_p:
            result = CliRunner().invoke(cli, ["channel", "list"])
        assert result.exit_code == 0
        assert "slack" in result.output
        assert "discord" in result.output

    def test_list_no_channels(self) -> None:
        config_p, getch_p, _ = _patch_channel(list_channels=[])
        with config_p, getch_p:
            result = CliRunner().invoke(cli, ["channel", "list"])
        assert result.exit_code == 0
        assert "No channels available" in result.output

    def test_list_connection_error(self) -> None:
        config_p, getch_p, inst = _patch_channel()
        inst.list_channels.side_effect = ConnectionError("refused")
        with config_p, getch_p:
            result = CliRunner().invoke(cli, ["channel", "list"])
        assert result.exit_code == 0
        assert "Failed" in result.output or "refused" in result.output


class TestChannelSend:
    def test_send_success(self) -> None:
        config_p, getch_p, _ = _patch_channel(send_return=True)
        with config_p, getch_p:
            result = CliRunner().invoke(
                cli,
                ["channel", "send", "slack", "Hello!"],
            )
        assert result.exit_code == 0
        assert "Message sent" in result.output

    def test_send_failure(self) -> None:
        config_p, getch_p, _ = _patch_channel(send_return=False)
        with config_p, getch_p:
            result = CliRunner().invoke(
                cli,
                ["channel", "send", "slack", "Hello!"],
            )
        assert result.exit_code == 0
        assert "Failed to send" in result.output


class TestChannelConnect:
    def test_connect_streams_and_disconnects(self) -> None:
        config_p, getch_p, inst = _patch_channel(
            status_return=ChannelStatus.CONNECTED,
        )
        # Stop the listen loop on the first sleep so the test terminates.
        sleep_p = mock.patch("time.sleep", side_effect=KeyboardInterrupt)
        with config_p, getch_p, sleep_p:
            result = CliRunner().invoke(
                cli,
                ["channel", "connect", "--channel-type", "whatsapp_baileys"],
            )

        assert result.exit_code == 0
        assert "Connected" in result.output
        assert "disconnected" in result.output.lower()
        inst.connect.assert_called_once()
        inst.on_message.assert_called_once()
        inst.disconnect.assert_called_once()

    def test_connect_registers_qr_surface(self) -> None:
        config_p, getch_p, inst = _patch_channel(
            status_return=ChannelStatus.CONNECTED,
        )
        sleep_p = mock.patch("time.sleep", side_effect=KeyboardInterrupt)
        with config_p, getch_p, sleep_p:
            CliRunner().invoke(
                cli,
                ["channel", "connect", "--channel-type", "whatsapp_baileys"],
            )
        # QR pairing surfaces registered for bridge-style channels.
        inst.set_stderr_handler.assert_called_once()
        inst.on_qr.assert_called_once()

    def test_connect_unsupported_channel(self) -> None:
        cfg = mock.MagicMock()
        cfg.channel.default_channel = ""
        # A channel object that lacks on_message is not a live channel.
        limited = mock.MagicMock(spec=["connect", "status", "disconnect"])
        config_p = mock.patch("openjarvis.core.config.load_config", return_value=cfg)
        getch_p = mock.patch(
            "openjarvis.cli.channel_cmd._get_channel", return_value=limited
        )
        with config_p, getch_p:
            result = CliRunner().invoke(
                cli, ["channel", "connect", "--channel-type", "whatsapp"]
            )
        assert result.exit_code == 0
        assert "does not support live connections" in result.output
        limited.connect.assert_not_called()


class TestChannelConnectExtraction:
    def test_connect_flags_in_help(self) -> None:
        result = CliRunner().invoke(cli, ["channel", "connect", "--help"])
        assert result.exit_code == 0
        assert "--extract-tasks" in result.output
        assert "--to-reminders" in result.output
        assert "--to-obsidian" in result.output
        assert "--vault" in result.output

    def test_extract_without_engine_warns(self) -> None:
        config_p, getch_p, _ = _patch_channel(
            status_return=ChannelStatus.CONNECTED,
        )
        resolve_p = mock.patch(
            "openjarvis.cli.channel_cmd._resolve_engine_model",
            return_value=None,
        )
        sleep_p = mock.patch("time.sleep", side_effect=KeyboardInterrupt)
        with config_p, getch_p, resolve_p, sleep_p:
            result = CliRunner().invoke(
                cli,
                [
                    "channel",
                    "connect",
                    "--channel-type",
                    "whatsapp_baileys",
                    "--extract-tasks",
                ],
            )
        assert result.exit_code == 0
        assert "no inference engine" in result.output.lower()

    def test_extract_with_engine_announces(self) -> None:
        config_p, getch_p, _ = _patch_channel(
            status_return=ChannelStatus.CONNECTED,
        )
        resolve_p = mock.patch(
            "openjarvis.cli.channel_cmd._resolve_engine_model",
            return_value=(mock.MagicMock(), "test-model"),
        )
        sleep_p = mock.patch("time.sleep", side_effect=KeyboardInterrupt)
        with config_p, getch_p, resolve_p, sleep_p:
            result = CliRunner().invoke(
                cli,
                [
                    "channel",
                    "connect",
                    "--channel-type",
                    "whatsapp_baileys",
                    "--extract-tasks",
                ],
            )
        assert result.exit_code == 0
        assert "Task extraction on" in result.output
        assert "test-model" in result.output

    def test_model_override_announced(self) -> None:
        config_p, getch_p, _ = _patch_channel(
            status_return=ChannelStatus.CONNECTED,
        )
        resolve_p = mock.patch(
            "openjarvis.cli.channel_cmd._resolve_engine_model",
            return_value=(mock.MagicMock(), "default-model"),
        )
        sleep_p = mock.patch("time.sleep", side_effect=KeyboardInterrupt)
        with config_p, getch_p, resolve_p, sleep_p:
            result = CliRunner().invoke(
                cli,
                [
                    "channel",
                    "connect",
                    "--channel-type",
                    "whatsapp_baileys",
                    "--extract-tasks",
                    "--model",
                    "qwen3:8b",
                ],
            )
        assert result.exit_code == 0
        assert "qwen3:8b" in result.output
        assert "default-model" not in result.output


class TestChannelInbox:
    def test_inbox_empty(self) -> None:
        with mock.patch(
            "openjarvis.channels.task_extraction.load_items", return_value=[]
        ):
            result = CliRunner().invoke(cli, ["channel", "inbox"])
        assert result.exit_code == 0
        assert "No extracted items" in result.output

    def test_inbox_lists_items(self) -> None:
        from openjarvis.channels.task_extraction import ExtractedItem

        items = [
            ExtractedItem(
                kind="task",
                title="Pagar renta",
                due="2026-07-08",
                source_sender="Casero",
                created_at="2026-07-07T12:00:00",
            ),
            ExtractedItem(
                kind="meeting",
                title="Junta con Ana",
                due="2026-07-10T10:00",
                source_sender="Ana",
                created_at="2026-07-07T12:05:00",
            ),
        ]
        with mock.patch(
            "openjarvis.channels.task_extraction.load_items", return_value=items
        ):
            result = CliRunner().invoke(cli, ["channel", "inbox"])
        assert result.exit_code == 0
        assert "Pagar renta" in result.output
        assert "Junta con Ana" in result.output

    def test_inbox_filter_kind(self) -> None:
        from openjarvis.channels.task_extraction import ExtractedItem

        items = [
            ExtractedItem(kind="task", title="Tarea A"),
            ExtractedItem(kind="meeting", title="Reunion B"),
        ]
        with mock.patch(
            "openjarvis.channels.task_extraction.load_items", return_value=items
        ):
            result = CliRunner().invoke(cli, ["channel", "inbox", "--kind", "meeting"])
        assert result.exit_code == 0
        assert "Reunion B" in result.output
        assert "Tarea A" not in result.output


class TestServiceHelpers:
    def test_connect_args_defaults(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
        )
        assert args == [
            "channel",
            "connect",
            "--channel-type",
            "whatsapp_baileys",
            "--to-reminders",
            "--to-obsidian",
        ]

    def test_connect_args_extract_only(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=False,
            to_obsidian=False,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
        )
        assert "--extract-tasks" in args
        assert "--to-reminders" not in args

    def test_connect_args_with_model(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
            model="qwen3:8b",
        )
        assert "--model" in args and "qwen3:8b" in args

    def test_connect_args_with_calendar(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
            to_calendar=True,
            calendar="Trabajo",
        )
        assert "--to-calendar" in args
        assert "--calendar" in args and "Trabajo" in args

    def test_connect_args_no_calendar_by_default(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
        )
        assert "--to-calendar" not in args
        assert "--calendar" not in args

    def test_connect_args_no_model_by_default(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="WhatsApp",
            obsidian_note="Bandeja de WhatsApp.md",
        )
        assert "--model" not in args

    def test_connect_args_custom_note_and_list(self) -> None:
        from openjarvis.cli.channel_cmd import _service_connect_args

        args = _service_connect_args(
            "whatsapp_baileys",
            extract_tasks=True,
            to_reminders=True,
            to_obsidian=True,
            reminders_list="Trabajo",
            obsidian_note="Inbox WA.md",
        )
        assert "--reminders-list" in args and "Trabajo" in args
        assert "--obsidian-note" in args and "Inbox WA.md" in args

    def test_wrapper_script_quotes_and_sources(self) -> None:
        from openjarvis.cli.channel_cmd import _service_wrapper_script

        body = _service_wrapper_script(
            "/Users/juan/dev/Open Jarvis",  # space -> must be quoted
            "/Users/juan/vault",
            ["channel", "connect", "--channel-type", "whatsapp_baileys"],
        )
        assert body.startswith("#!/bin/zsh\n")
        assert 'source "$HOME/.zprofile"' in body
        assert "export VAULT=/Users/juan/vault" in body
        # A path with a space must be shell-quoted.
        assert "'/Users/juan/dev/Open Jarvis'" in body
        assert body.rstrip().endswith(
            "exec uv run jarvis channel connect --channel-type whatsapp_baileys"
        )

    def test_wrapper_omits_vault_when_empty(self) -> None:
        from openjarvis.cli.channel_cmd import _service_wrapper_script

        body = _service_wrapper_script("/repo", "", ["channel", "connect"])
        assert "export VAULT" not in body

    def test_wrapper_prepends_path_dirs(self) -> None:
        from openjarvis.cli.channel_cmd import _service_wrapper_script

        body = _service_wrapper_script(
            "/repo",
            "",
            ["channel", "connect"],
            ["/opt/homebrew/bin", "/Users/j/.local/bin"],
        )
        assert 'export PATH="/opt/homebrew/bin:/Users/j/.local/bin:$PATH"' in body

    def test_wrapper_no_path_line_without_dirs(self) -> None:
        from openjarvis.cli.channel_cmd import _service_wrapper_script

        body = _service_wrapper_script("/repo", "", ["channel", "connect"])
        assert "export PATH" not in body


class TestChannelStatus:
    def test_status_shows_info(self) -> None:
        config_p, getch_p, _ = _patch_channel(
            status_return=ChannelStatus.DISCONNECTED,
        )
        with config_p, getch_p:
            result = CliRunner().invoke(cli, ["channel", "status"])
        assert result.exit_code == 0
        assert "disconnected" in result.output
