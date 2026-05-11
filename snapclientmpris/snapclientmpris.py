"""snapclientmpris daemon: asyncio bridge between snapserver and MPRIS2.

Connects to a snapserver via python-snapcast (asyncio JSON-RPC) and
publishes an MPRIS2 interface on D-Bus via dbus-fast. The local audio
side (snapclient) is expected to run as its own service — typically
``snapclient.service`` from the Debian ``snapclient`` package, which
this unit Wants= and orders After=.

Everything runs in a single asyncio event loop — no threads, no GLib.
The CLI entry point (argparse, config, Zeroconf discovery) lives in
``snapclientmpris.cli``; this module only exposes the runtime helpers
and the ``run`` coroutine the CLI dispatches to.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Coroutine
from os import listdir

import snapcast.control
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from snapclientmpris.mpris import (
    BUS_NAME,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
    snapserver_to_playback_status,
    translate_snapserver_metadata,
)

logger = logging.getLogger("snapclientmpris")


def local_mac_addresses() -> list[str]:
    """Return MAC addresses of all up, non-loopback interfaces."""
    macs: list[str] = []
    for iface in listdir("/sys/class/net/"):
        if iface == "lo":
            continue
        try:
            with open(f"/sys/class/net/{iface}/operstate") as f:
                if f.readline().strip() == "down":
                    continue
            with open(f"/sys/class/net/{iface}/address") as f:
                macs.append(f.readline().strip())
        except OSError:
            continue
    return macs


def identify_client(server: snapcast.control.Snapserver, macs: list[str]):
    """Locate this host's Snapclient in ``server.clients`` by MAC."""
    for client in server.clients:
        if client.identifier in macs:
            return client
    return None


# --- Pure state helpers ------------------------------------------------
def client_stream(
    server: snapcast.control.Snapserver,
    client: snapcast.control.Snapclient | None,
) -> snapcast.control.Snapstream | None:
    """Resolve the stream currently bound to ``client``.

    python-snapcast >= 2.3.8 exposes ``Snapclient.stream`` directly; on
    Debian trixie we ship against 2.3.7, where you must walk
    ``client.group`` -> ``group.stream`` (stream id) -> ``server.stream(id)``.
    """
    if client is None:
        return None
    g = client.group
    if g is None:
        return None
    return server.stream(g.stream)


def playback_status(stream: snapcast.control.Snapstream | None) -> str:
    """Derive the MPRIS PlaybackStatus for ``stream``.

    Prefer the source's explicit MPRIS state (``properties.playbackStatus``,
    populated by metadata scripts on sources like Spotifyd / Librespot)
    when present; fall back to mapping snapserver's ``stream.status``.
    An idle snapserver stream means "no audio flowing", not "track is
    paused mid-play", so it maps to Stopped — Paused is only reported
    when the source explicitly says so.
    """
    if stream is None:
        return "Stopped"
    explicit = (stream.properties or {}).get("playbackStatus")
    if explicit:
        return snapserver_to_playback_status(explicit)
    return snapserver_to_playback_status(stream.status)


def stream_metadata(
    host: str, stream: snapcast.control.Snapstream | None
) -> dict | None:
    """Compute the MPRIS Metadata dict for ``stream``, or ``None`` if there
    is no stream — callers should leave existing metadata in place rather
    than clearing it in that case.

    Adds two ``snapcast:`` namespaced custom keys (MPRIS spec explicitly
    allows player-specific keys in Metadata): ``snapcast:stream`` is the
    snapserver stream identifier (e.g. ``SpotifyHD`` / ``MPD``), and
    ``snapcast:streamStatus`` is the raw snapserver stream status
    (``playing`` / ``idle`` / ...). Together they let an observer
    distinguish, for instance, an attached-but-idle MPD source from a
    SpotifyHD source actually producing audio — info that doesn't
    survive the MPRIS PlaybackStatus mapping.
    """
    if stream is None:
        return None
    url = f"snapcast://{host}/{stream.identifier}"
    md = translate_snapserver_metadata(stream.metadata or {}, snapcast_url=url)
    md["snapcast:stream"] = Variant("s", stream.identifier)
    md["snapcast:streamStatus"] = Variant("s", str(stream.status or ""))
    return md


def stream_capabilities(stream: snapcast.control.Snapstream | None) -> dict:
    """Extract MPRIS-relevant capability flags from ``stream.properties``.

    Snapserver streams report ``canPlay`` / ``canPause`` / ``canGoNext`` /
    ``canGoPrevious`` / ``canSeek`` / ``canControl`` in ``stream.properties``;
    we mirror them to the matching MPRIS ``Can*`` properties so clients
    (gnome-music, KDE plasma, playerctl) only enable the buttons the source
    actually supports.
    """
    caps = {
        "CanPlay": False,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
    }
    if stream is None:
        return caps
    p = stream.properties or {}
    caps["CanPlay"] = bool(p.get("canPlay", False))
    caps["CanPause"] = bool(p.get("canPause", False))
    caps["CanGoNext"] = bool(p.get("canGoNext", False))
    caps["CanGoPrevious"] = bool(p.get("canGoPrevious", False))
    caps["CanSeek"] = bool(p.get("canSeek", False))
    return caps


async def control_stream(
    server: snapcast.control.Snapserver,
    stream: snapcast.control.Snapstream | None,
    command: str,
) -> None:
    """Forward an MPRIS-style command to the snapserver stream's source.

    Snapserver's ``Stream.Control`` JSON-RPC method relays commands
    (``play``, ``pause``, ``playPause``, ``stop``, ``next``, ``previous``,
    ``seek``, ``setPosition``) to the source process. No-op when there is
    no stream bound or the stream reports ``canControl=false``.
    """
    if stream is None:
        return
    props = stream.properties or {}
    if not props.get("canControl", False):
        logger.debug("control_stream: %s ignored (canControl=False)", command)
        return
    logger.debug("control_stream: %s -> %s", command, stream.identifier)
    await server.stream_control(stream.identifier, command, {})


# --- Daemon ------------------------------------------------------------
async def run(host: str, control_port: int, bus_type: BusType) -> None:
    loop = asyncio.get_running_loop()

    server = snapcast.control.Snapserver(loop, host, control_port)
    await server.start()
    logger.info("connected to snapserver at %s:%d", host, control_port)

    # Find this host's snapclient in the snapserver roster (matched by MAC).
    # The local snapclient is started by its own systemd unit, so it may
    # take a moment to register after us; retry once.
    macs = local_mac_addresses()
    client = identify_client(server, macs)
    if client is None:
        await asyncio.sleep(2)
        await server.status()
        client = identify_client(server, macs)
    if client is None:
        logger.warning("local MAC %s not in snapserver roster; controls and "
                       "metadata will be inert until snapclient registers", macs)

    def refresh() -> None:
        if client is None:
            return
        s = client_stream(server, client)
        logger.debug("refresh: client.volume=%s stream=%s status=%s",
                     client.volume,
                     s.identifier if s is not None else None,
                     s.status if s is not None else None)
        md = stream_metadata(host, s)
        if md is not None:
            player.update_metadata(md)
        player.update_playback_status(playback_status(s))
        player.update_capabilities(stream_capabilities(s))
        if client.volume is not None:
            player.update_volume(client.volume / 100.0)

    # Strong-reference fire-and-forget tasks so the event loop's weak refs
    # don't let them be GC'd mid-execution (asyncio docs explicitly warn).
    bg_tasks: set[asyncio.Task] = set()

    def schedule(coro: Coroutine) -> None:
        task = loop.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    async def set_volume(percent: int) -> None:
        if client is None:
            return
        await client.set_volume(percent)
        refresh()

    def control(command: str) -> None:
        # Delegates MPRIS playback commands to the snapserver stream's
        # source (Spotifyd / Librespot / etc.) via Stream.Control. With
        # multiple snapclients on the same stream, this affects every
        # listener — that's the intended multi-room semantic.
        schedule(control_stream(server, client_stream(server, client), command))

    def on_volume_set(v: float) -> None:
        # MPRIS Volume is a float 0.0-1.0; snapserver expects 0-100 int.
        schedule(set_volume(int(round(v * 100))))

    player = MediaPlayer2Player(
        on_play=lambda: control("play"),
        on_pause=lambda: control("pause"),
        on_play_pause=lambda: control("playPause"),
        on_stop=lambda: control("stop"),
        on_next=lambda: control("next"),
        on_previous=lambda: control("previous"),
        on_volume_set=on_volume_set,
    )

    bus = await MessageBus(bus_type=bus_type).connect()
    bus.export(ROOT_PATH, MediaPlayer2())
    bus.export(ROOT_PATH, player)
    await bus.request_name(BUS_NAME)
    logger.info("D-Bus name acquired: %s", BUS_NAME)

    # Any change to streams (status, metadata, properties) or to our
    # client (volume, mute) triggers a full refresh.
    for s in server.streams:
        s.set_callback(lambda _stream: refresh())
    if client is not None:
        client.set_callback(lambda _client: refresh())

    refresh()  # seed

    # SIGUSR1 → pause, SIGUSR2 → stop. Matches the upstream signal contract;
    # both are now forwarded to the snapserver stream's source rather than
    # toggling local mute.
    loop.add_signal_handler(signal.SIGUSR1, lambda: control("pause"))
    loop.add_signal_handler(signal.SIGUSR2, lambda: control("stop"))

    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        server.stop()
