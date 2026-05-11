#!/usr/bin/env python3
"""snapclientmpris — MPRIS2 D-Bus bridge for a local Snapcast client.

Connects to a snapserver via python-snapcast (asyncio JSON-RPC) and
publishes an MPRIS2 interface on D-Bus via dbus-fast. The local audio
side (snapclient) is expected to run as its own service — typically
``snapclient.service`` from the Debian ``snapclient`` package, which
this unit Wants= and orders After=.

Everything runs in a single asyncio event loop — no threads, no GLib.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import contextlib
import logging
import os
import signal
import sys
from os import listdir

import snapcast.control
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from zeroconf import IPVersion, Zeroconf

from snapclientmpris.mpris import (
    BUS_NAME,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
    translate_snapserver_metadata,
)

logger = logging.getLogger("snapclientmpris")

CONFIG_PATHS = [
    os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "snapclientmpris",
        "snapclientmpris.conf",
    ),
    "/etc/snapclientmpris.conf",
]

SNAPSERVER_CONTROL_PORT = 1705


# --- Config / discovery ------------------------------------------------
def read_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    for path in CONFIG_PATHS:
        try:
            with open(path) as f:
                cfg.read_string("[snapcast]\n" + f.read())
            logger.info("read %s", path)
            return cfg
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("failed to read %s: %s", path, e)
    logger.info("no config file found, using defaults")
    return cfg


def discover_snapserver() -> str | None:
    """Return the snapserver host discovered via Zeroconf, or ``None``."""
    zc = Zeroconf()
    try:
        info = zc.get_service_info(
            "_snapcast._tcp.local.",
            "Snapcast._snapcast._tcp.local.",
            timeout=3000,
        )
        if info is None:
            return None
        for addr in info.parsed_addresses(IPVersion.V4Only):
            if addr != "0.0.0.0":
                return str(addr)
        return None
    finally:
        zc.close()


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
    loop = asyncio.get_event_loop()

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
        if s is not None:
            url = f"snapcast://{host}/{s.identifier}"
            md = translate_snapserver_metadata(s.metadata or {}, snapcast_url=url)
            player.update_metadata(md)
        player.update_playback_status(current_status())
        if client.volume is not None:
            player.update_volume(client.volume / 100.0)

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
        loop.create_task(set_muted(False))

    def pause() -> None:
        loop.create_task(set_muted(True))

    def play_pause() -> None:
        if client is None:
            return
        loop.create_task(set_muted(not client.muted))

    def on_volume_set(v: float) -> None:
        # MPRIS Volume is a float 0.0-1.0; snapserver expects 0-100 int.
        loop.create_task(set_volume(int(round(v * 100))))

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


# --- CLI ---------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snapclientmpris",
        description=("MPRIS2 D-Bus bridge for a local Snapcast client. "
                     "Talks to snapserver and exposes the local client's "
                     "PlaybackStatus, Metadata and Volume over D-Bus. "
                     "Snapclient itself runs as its own service."),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(levelname)s: %(name)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    if not args.verbose:
        logging.getLogger("snapcast").setLevel(logging.WARNING)

    cfg = read_config()
    host = cfg.get("snapcast", "server", fallback=None)
    if not host:
        host = discover_snapserver()
        if not host:
            logger.critical("no snapserver configured and Zeroconf discovery failed")
            sys.exit(1)
        logger.info("discovered snapserver via Zeroconf: %s", host)

    control_port = cfg.getint("snapcast", "control-port", fallback=SNAPSERVER_CONTROL_PORT)
    bus_choice = cfg.get("snapcast", "dbus-bus", fallback="session").strip().lower()
    bus_type = BusType.SESSION if bus_choice == "session" else BusType.SYSTEM
    logger.info("using %s D-Bus", bus_choice)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(host, control_port, bus_type))


if __name__ == "__main__":
    main()
