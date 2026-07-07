"""``jarvis channel`` -- channel management commands."""

from __future__ import annotations

from typing import Any, Dict, Optional

import click
from rich.console import Console
from rich.table import Table

_CHANNEL_TYPE_HELP = (
    "Channel type (sendblue, telegram, discord, slack, webhook, email, "
    "whatsapp, whatsapp_baileys, signal, google_chat, irc, webchat, teams, "
    "matrix, mattermost, feishu, bluebubbles)."
)


def _get_channel(
    channel_type: str | None,
    config: Any,
) -> Any:
    """Resolve a channel backend by type.

    Resolution order: ``--channel-type`` flag >
    ``config.channel.default_channel`` > error.
    """
    import openjarvis.channels  # noqa: F401 -- trigger registration
    from openjarvis.core.registry import ChannelRegistry

    key = channel_type or config.channel.default_channel
    if not key:
        raise click.ClickException(
            "No channel type specified. Use --channel-type or set "
            "default_channel in [channel] config."
        )

    kwargs: Dict[str, Any] = {}
    if key == "telegram":
        tc = config.channel.telegram
        if tc.bot_token:
            kwargs["bot_token"] = tc.bot_token
    elif key == "discord":
        dc = config.channel.discord
        if dc.bot_token:
            kwargs["bot_token"] = dc.bot_token
    elif key == "slack":
        sc = config.channel.slack
        if sc.bot_token:
            kwargs["bot_token"] = sc.bot_token
        if sc.app_token:
            kwargs["app_token"] = sc.app_token
    elif key == "webhook":
        wc = config.channel.webhook
        if wc.url:
            kwargs["url"] = wc.url
        if wc.secret:
            kwargs["secret"] = wc.secret
        if wc.method:
            kwargs["method"] = wc.method
    elif key == "email":
        ec = config.channel.email
        if ec.smtp_host:
            kwargs["smtp_host"] = ec.smtp_host
        kwargs["smtp_port"] = ec.smtp_port
        if ec.username:
            kwargs["username"] = ec.username
        if ec.password:
            kwargs["password"] = ec.password
        kwargs["use_tls"] = ec.use_tls
    elif key == "whatsapp":
        wac = config.channel.whatsapp
        if wac.access_token:
            kwargs["access_token"] = wac.access_token
        if wac.phone_number_id:
            kwargs["phone_number_id"] = wac.phone_number_id
    elif key == "signal":
        sgc = config.channel.signal
        if sgc.api_url:
            kwargs["api_url"] = sgc.api_url
        if sgc.phone_number:
            kwargs["phone_number"] = sgc.phone_number
    elif key == "google_chat":
        gcc = config.channel.google_chat
        if gcc.webhook_url:
            kwargs["webhook_url"] = gcc.webhook_url
    elif key == "irc":
        ic = config.channel.irc
        if ic.server:
            kwargs["server"] = ic.server
        kwargs["port"] = ic.port
        if ic.nick:
            kwargs["nick"] = ic.nick
        if ic.password:
            kwargs["password"] = ic.password
        kwargs["use_tls"] = ic.use_tls
    elif key == "webchat":
        pass  # no config needed
    elif key == "teams":
        tmc = config.channel.teams
        if tmc.app_id:
            kwargs["app_id"] = tmc.app_id
        if tmc.app_password:
            kwargs["app_password"] = tmc.app_password
        if tmc.service_url:
            kwargs["service_url"] = tmc.service_url
    elif key == "matrix":
        mc = config.channel.matrix
        if mc.homeserver:
            kwargs["homeserver"] = mc.homeserver
        if mc.access_token:
            kwargs["access_token"] = mc.access_token
    elif key == "mattermost":
        mmc = config.channel.mattermost
        if mmc.url:
            kwargs["url"] = mmc.url
        if mmc.token:
            kwargs["token"] = mmc.token
    elif key == "feishu":
        fc = config.channel.feishu
        if fc.app_id:
            kwargs["app_id"] = fc.app_id
        if fc.app_secret:
            kwargs["app_secret"] = fc.app_secret
    elif key == "bluebubbles":
        bbc = config.channel.bluebubbles
        if bbc.url:
            kwargs["url"] = bbc.url
        if bbc.password:
            kwargs["password"] = bbc.password
    elif key == "whatsapp_baileys":
        wbc = config.channel.whatsapp_baileys
        if wbc.auth_dir:
            kwargs["auth_dir"] = wbc.auth_dir
        if wbc.assistant_name:
            kwargs["assistant_name"] = wbc.assistant_name
        kwargs["assistant_has_own_number"] = wbc.assistant_has_own_number
    elif key == "sendblue":
        import os

        kwargs["api_key_id"] = os.environ.get("SENDBLUE_API_KEY_ID", "")
        kwargs["api_secret_key"] = os.environ.get("SENDBLUE_API_SECRET_KEY", "")
        kwargs["from_number"] = os.environ.get("SENDBLUE_FROM_NUMBER", "")
        sbc = getattr(config.channel, "sendblue", None)
        if sbc:
            if getattr(sbc, "api_key_id", ""):
                kwargs["api_key_id"] = sbc.api_key_id
            if getattr(sbc, "api_secret_key", ""):
                kwargs["api_secret_key"] = sbc.api_secret_key
            if getattr(sbc, "from_number", ""):
                kwargs["from_number"] = sbc.from_number

    if not ChannelRegistry.contains(key):
        raise click.ClickException(f"Unknown channel type: {key}")

    return ChannelRegistry.create(key, **kwargs)


@click.group()
def channel() -> None:
    """Manage messaging channels."""


@channel.command("list")
@click.option(
    "--channel-type",
    default=None,
    help=_CHANNEL_TYPE_HELP,
)
def channel_list(
    channel_type: Optional[str],
) -> None:
    """List available channels."""
    console = Console()
    from openjarvis.core.config import load_config

    config = load_config()

    try:
        ch = _get_channel(channel_type, config)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
        return

    try:
        channels = ch.list_channels()
    except Exception as exc:
        console.print(f"[red]Failed to list channels: {exc}[/red]")
        return

    if not channels:
        console.print("[yellow]No channels available[/yellow]")
        return

    table = Table(title="Available Channels")
    table.add_column("Channel", style="cyan")
    for name in channels:
        table.add_row(name)
    console.print(table)


@channel.command("send")
@click.argument("target")
@click.argument("message")
@click.option(
    "--channel-type",
    default=None,
    help=_CHANNEL_TYPE_HELP,
)
def channel_send(
    target: str,
    message: str,
    channel_type: Optional[str],
) -> None:
    """Send a message to a channel."""
    console = Console()
    from openjarvis.core.config import load_config

    config = load_config()

    try:
        ch = _get_channel(channel_type, config)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
        return

    ok = ch.send(target, message)
    if ok:
        console.print(f"[green]Message sent to {target}[/green]")
    else:
        console.print(
            f"[red]Failed to send message to {target}[/red]",
        )


def _resolve_engine_model(config: Any) -> Any:
    """Resolve an ``(engine, model)`` pair from config for task extraction.

    Mirrors the resolution chain used by ``jarvis ask``.  Returns ``None`` when
    no engine is reachable or no model can be determined.
    """
    from openjarvis.engine import discover_engines, discover_models, get_engine
    from openjarvis.intelligence import register_builtin_models

    register_builtin_models()

    resolved = get_engine(config, config.intelligence.preferred_engine or None)
    if resolved is None:
        return None
    engine_name, engine = resolved

    all_models = discover_models(discover_engines(config))
    model_name = config.intelligence.default_model
    if not model_name:
        engine_models = all_models.get(engine_name, [])
        model_name = engine_models[0] if engine_models else ""
    if not model_name:
        model_name = config.intelligence.fallback_model
    if not model_name:
        return None
    return engine, model_name


@channel.command("connect")
@click.option(
    "--channel-type",
    default=None,
    help=_CHANNEL_TYPE_HELP,
)
@click.option(
    "--extract-tasks/--no-extract-tasks",
    default=False,
    help="Extract tasks/meetings from incoming messages with the LLM and save "
    "them to ~/.openjarvis/extracted_tasks.jsonl.",
)
@click.option(
    "--to-reminders/--no-to-reminders",
    default=False,
    help="Also create macOS Reminders for extracted items (implies "
    "--extract-tasks; no-op off macOS).",
)
@click.option(
    "--reminders-list",
    default="WhatsApp",
    help="Reminders.app list for --to-reminders (default: WhatsApp).",
)
def channel_connect(
    channel_type: Optional[str],
    extract_tasks: bool,
    to_reminders: bool,
    reminders_list: str,
) -> None:
    """Connect a live channel and stream incoming messages.

    Spawns the channel backend (for ``whatsapp_baileys`` this pairs your
    personal WhatsApp account by QR code), prints the QR to scan, and then
    prints each incoming message until interrupted with Ctrl+C.

    With ``--extract-tasks`` each incoming message is run through the LLM to
    detect actionable tasks and meetings, which are saved for later review
    (see ``jarvis channel inbox``).  Add ``--to-reminders`` on macOS to also
    push them into Reminders.app, where the HUD picks them up.

    Example::

        jarvis channel connect --channel-type whatsapp_baileys --to-reminders
    """
    import time

    console = Console()
    from openjarvis.core.config import load_config

    config = load_config()

    try:
        ch = _get_channel(channel_type, config)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
        return

    key = (
        channel_type
        or config.channel.default_channel
        or getattr(ch, "channel_id", "unknown")
    )

    if not (hasattr(ch, "connect") and hasattr(ch, "on_message")):
        console.print(f"[red]Channel '{key}' does not support live connections.[/red]")
        return

    # Surface the pairing QR.  Bridge-based channels (e.g. whatsapp_baileys)
    # render a scannable ASCII QR to their subprocess stderr; forward it
    # verbatim so it stays scannable.
    if hasattr(ch, "set_stderr_handler"):
        ch.set_stderr_handler(lambda line: click.echo(line))
    if hasattr(ch, "on_qr"):
        ch.on_qr(
            lambda _data: console.print(
                "\n[bold cyan]Scan this QR in WhatsApp → Settings → "
                "Linked Devices → Link a Device:[/bold cyan]\n"
            )
        )

    # Build the task/meeting extractor when requested.
    extractor = None
    if extract_tasks or to_reminders:
        resolved = _resolve_engine_model(config)
        if resolved is None:
            console.print(
                "[yellow]Task extraction requested but no inference engine/model "
                "is available — streaming messages only. Start Ollama or set a "
                "cloud API key.[/yellow]"
            )
        else:
            engine, model = resolved
            sink = None
            if to_reminders:
                from openjarvis.channels.task_sinks import AppleRemindersSink

                sink = AppleRemindersSink(list_name=reminders_list)
                if not sink.available:
                    console.print(
                        "[yellow]--to-reminders is macOS-only; extracted items "
                        "will be saved but not pushed to Reminders.app.[/yellow]"
                    )
            from openjarvis.channels.task_extraction import (
                MessageTaskExtractor,
                default_store_path,
            )

            extractor = MessageTaskExtractor(engine, model=model, sink=sink)
            console.print(
                f"[cyan]Task extraction on[/cyan] (model: {model}) → "
                f"{default_store_path()}"
            )

    def _on_message(msg: Any) -> None:
        sender = getattr(msg, "sender", "") or getattr(msg, "conversation_id", "")
        content = getattr(msg, "content", "")
        console.print(f"[green]{sender}[/green]: {content}")
        if extractor is not None:
            try:
                items = extractor.process(msg)
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]  extraction failed: {exc}[/red]")
                return
            for it in items:
                due = f" [dim](vence {it.due})[/dim]" if it.due else ""
                icon = "📅" if it.kind == "meeting" else "✅"
                console.print(f"  {icon} [bold]{it.title}[/bold]{due}")

    ch.on_message(_on_message)

    console.print(f"[cyan]Connecting channel:[/cyan] {key}")
    ch.connect()

    last_status: Any = None
    try:
        while True:
            st = ch.status()
            if st != last_status:
                if st.value == "connected":
                    console.print(
                        "[green]✓ Connected. Listening for messages "
                        "(press Ctrl+C to stop)…[/green]"
                    )
                elif st.value == "error":
                    console.print(
                        "[red]Channel entered an error state. "
                        "Run with logging enabled for details.[/red]"
                    )
                last_status = st
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Disconnecting…[/yellow]")
    finally:
        try:
            ch.disconnect()
        except Exception:  # noqa: BLE001
            pass
        console.print("[dim]Channel disconnected.[/dim]")


@channel.command("status")
@click.option(
    "--channel-type",
    default=None,
    help=_CHANNEL_TYPE_HELP,
)
def channel_status(
    channel_type: Optional[str],
) -> None:
    """Show channel connection status."""
    console = Console()
    from openjarvis.core.config import load_config

    config = load_config()

    try:
        ch = _get_channel(channel_type, config)
    except click.ClickException as exc:
        console.print(f"[red]{exc.message}[/red]")
        return

    st = ch.status()
    color = {
        "connected": "green",
        "disconnected": "yellow",
        "connecting": "blue",
        "error": "red",
    }.get(st.value, "white")

    key = channel_type or config.channel.default_channel or "unknown"
    console.print(f"Channel: [cyan]{key}[/cyan]")
    console.print(f"Status: [{color}]{st.value}[/{color}]")


@channel.command("inbox")
@click.option(
    "--limit",
    default=20,
    show_default=True,
    help="Maximum number of most-recent items to show.",
)
@click.option(
    "--kind",
    type=click.Choice(["all", "task", "meeting"]),
    default="all",
    show_default=True,
    help="Filter by item kind.",
)
def channel_inbox(limit: int, kind: str) -> None:
    """List tasks/meetings extracted from incoming messages.

    Reads ``~/.openjarvis/extracted_tasks.jsonl`` populated by
    ``jarvis channel connect --extract-tasks``.
    """
    console = Console()
    from openjarvis.channels.task_extraction import default_store_path, load_items

    items = load_items()
    if kind != "all":
        items = [it for it in items if it.kind == kind]

    if not items:
        console.print(
            "[yellow]No extracted items yet.[/yellow]\n"
            "[dim]Populate with: jarvis channel connect "
            "--channel-type whatsapp_baileys --extract-tasks[/dim]"
        )
        return

    items = items[-limit:][::-1]  # most recent first

    table = Table(title=f"Extracted items ({default_store_path()})")
    table.add_column("Kind", style="magenta")
    table.add_column("Title", style="bold")
    table.add_column("Due", style="cyan")
    table.add_column("From", style="green")
    table.add_column("Extracted", style="dim")
    for it in items:
        icon = "📅 meeting" if it.kind == "meeting" else "✅ task"
        table.add_row(
            icon,
            it.title,
            it.due or "—",
            it.source_sender or "—",
            (it.created_at or "")[:16].replace("T", " "),
        )
    console.print(table)
