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
from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from snapclientmpris.mpris import (
    BUS_NAME,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
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

    def client_stream() -> snapcast.control.Snapstream | None:
        # python-snapcast >= 2.3.8 exposes Snapclient.stream directly; on
        # Debian trixie we ship against 2.3.7, where you must walk
        # client.group -> group.stream (stream id) -> server.stream(id).
        if client is None:
            return None
        g = client.group
        if g is None:
            return None
        return server.stream(g.stream)

    # MPRIS — derive playback state from snapserver-side mute + stream status.
    def current_status() -> str:
        if client is None or client.muted:
            return "Paused"
        s = client_stream()
        if s is None or s.status != "playing":
            return "Paused"
        return "Playing"

    def refresh() -> None:
        if client is None:
            return
        s = client_stream()
        logger.debug("refresh: client.volume=%s muted=%s stream=%s",
                     client.volume, client.muted,
                     s.identifier if s is not None else None)
        if s is not None:
            url = f"snapcast://{host}/{s.identifier}"
            md = translate_snapserver_metadata(s.metadata or {}, snapcast_url=url)
            player.update_metadata(md)
        player.update_playback_status(current_status())
        if client.volume is not None:
            player.update_volume(client.volume / 100.0)

    # Strong-reference fire-and-forget tasks so the event loop's weak refs
    # don't let them be GC'd mid-execution (asyncio docs explicitly warn).
    bg_tasks: set[asyncio.Task] = set()

    def schedule(coro: Coroutine) -> None:
        task = loop.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    async def set_muted(muted: bool) -> None:
        if client is None:
            return
        await client.set_muted(muted)
        refresh()

    async def set_volume(percent: int) -> None:
        if client is None:
            return
        await client.set_volume(percent)
        refresh()

    def play() -> None:
        schedule(set_muted(False))

    def pause() -> None:
        schedule(set_muted(True))

    def play_pause() -> None:
        if client is None:
            return
        schedule(set_muted(not client.muted))

    def on_volume_set(v: float) -> None:
        # MPRIS Volume is a float 0.0-1.0; snapserver expects 0-100 int.
        schedule(set_volume(int(round(v * 100))))

    player = MediaPlayer2Player(
        on_play=play,
        on_pause=pause,
        on_play_pause=play_pause,
        on_stop=pause,  # streaming workflow: Stop is treated as Pause
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

    # SIGUSR1 → pause, SIGUSR2 → stop (= pause). Matches upstream contract.
    loop.add_signal_handler(signal.SIGUSR1, pause)
    loop.add_signal_handler(signal.SIGUSR2, pause)

    stop_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        server.stop()
