"""asyncio bridge: snapserver (python-snapcast) <-> MPRIS2 (dbus-fast).

Single event loop, no threads/GLib. Local audio (snapclient) runs as its
own unit. CLI/config/discovery live in ``snapclientmpris.cli``; this module
holds the runtime helpers and the ``run`` coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from os import listdir

import snapcast.control
from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus

from snapclientmpris.mpris import (
    BUS_NAME,
    ROOT_PATH,
    MediaPlayer2,
    MediaPlayer2Player,
)
from snapclientmpris.translate import (
    snapserver_to_playback_status,
    translate_snapserver_metadata,
)

logger = logging.getLogger("snapclientmpris")

CONNECT_BACKOFF_MIN = 1.0
CONNECT_BACKOFF_MAX = 30.0
CONNECT_BACKOFF_FACTOR = 1.5


async def _backoff_schedule() -> AsyncIterator[None]:
    """Yield once per attempt: first immediately, then after a growing,
    capped backoff sleep. Callers own each attempt's work and logging."""
    delay = CONNECT_BACKOFF_MIN
    yield
    while True:
        await asyncio.sleep(delay)
        delay = min(delay * CONNECT_BACKOFF_FACTOR, CONNECT_BACKOFF_MAX)
        yield


async def _reconnect_with_backoff(server: snapcast.control.Snapserver) -> None:
    """Retry ``server.start()`` on an existing server until it reconnects.
    Warns once, then stays quiet until recovery, to spare the journal."""
    warned = False
    async for _ in _backoff_schedule():
        try:
            await server.start()
        except OSError as e:
            if not warned:
                logger.warning(
                    "snapserver reconnect failed (%s); retrying with backoff", e
                )
                warned = True
            continue
        logger.info("snapserver reconnected")
        return


async def _connect_with_backoff(
    loop: asyncio.AbstractEventLoop,
    resolve: Callable[[], tuple[str, int] | None],
) -> tuple[snapcast.control.Snapserver, str]:
    """Resolve, construct the Snapserver, and connect, retrying with backoff;
    return the connected ``(server, host)``. ``resolve`` does blocking
    Zeroconf I/O (hence the executor) and returns the endpoint or ``None``.
    Warns once, then stays quiet, to spare the journal."""
    warned = False
    async for _ in _backoff_schedule():
        endpoint = await loop.run_in_executor(None, resolve)
        if endpoint is None:
            if not warned:
                logger.warning(
                    "snapserver not found (no configured host, Zeroconf empty); "
                    "retrying with backoff"
                )
                warned = True
            continue
        host, control_port = endpoint
        # reconnect=False: we own the retry policy, not python-snapcast.
        server = snapcast.control.Snapserver(loop, host, control_port, reconnect=False)
        try:
            await server.start()
        except OSError as e:
            if not warned:
                logger.warning(
                    "snapserver connect failed (%s); retrying with backoff", e
                )
                warned = True
            continue
        logger.info("connected to snapserver at %s:%d", host, control_port)
        return server, host
    raise AssertionError("unreachable: _backoff_schedule never ends")


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


def identify_client(
    server: snapcast.control.Snapserver, macs: list[str]
) -> snapcast.control.Snapclient | None:
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
    """Resolve the stream bound to ``client``. We walk client->group->stream
    by id (python-snapcast 2.3.7 on trixie has no ``Snapclient.stream``)."""
    if client is None:
        return None
    g = client.group
    if g is None:
        return None
    return server.stream(g.stream)


def playback_status(stream: snapcast.control.Snapstream | None) -> str:
    """MPRIS PlaybackStatus for ``stream``. Prefer the source's explicit
    ``properties.playbackStatus``; else map ``stream.status`` (idle->Stopped,
    so Paused only ever comes from the source saying so)."""
    if stream is None:
        return "Stopped"
    explicit = (stream.properties or {}).get("playbackStatus")
    if explicit:
        return snapserver_to_playback_status(explicit)
    return snapserver_to_playback_status(stream.status)


def stream_metadata(
    host: str, stream: snapcast.control.Snapstream | None
) -> dict | None:
    """MPRIS Metadata dict for ``stream``, or ``None`` if no stream (caller
    keeps existing metadata, doesn't clear). Adds custom ``snapcast:stream``
    and ``snapcast:streamStatus`` keys (raw id + status the PlaybackStatus
    mapping would otherwise lose)."""
    if stream is None:
        return None
    url = f"snapcast://{host}/{stream.identifier}"
    md = translate_snapserver_metadata(stream.metadata or {}, snapcast_url=url)
    md["snapcast:stream"] = Variant("s", stream.identifier)
    md["snapcast:streamStatus"] = Variant("s", str(stream.status or ""))
    return md


def stream_capabilities(stream: snapcast.control.Snapstream | None) -> dict:
    """Mirror the stream's ``can*`` properties to MPRIS ``Can*`` flags, so
    clients only enable the buttons the source supports."""
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
    """Forward an MPRIS command to the stream's source via Stream.Control.
    No-op with no stream or ``canControl=false``."""
    if stream is None:
        return
    props = stream.properties or {}
    if not props.get("canControl", False):
        logger.debug("control_stream: %s ignored (canControl=False)", command)
        return
    logger.debug("control_stream: %s -> %s", command, stream.identifier)
    await server.stream_control(stream.identifier, command, {})


# --- Daemon ------------------------------------------------------------
@dataclass(frozen=True)
class Connection:
    """``server`` + ``host``, one lifecycle. Bundled so a single
    ``conn is None`` check narrows both and ``host`` can't go missing."""

    server: snapcast.control.Snapserver
    host: str


class MprisBridge:
    """Bridges a snapserver connection to the exported MPRIS player.

    Two independent lifecycles, hence two attributes: ``conn`` is set once
    and kept; ``client`` comes and goes (nulled on disconnect, re-identified
    on reconnect or late registration). Callbacks guard on whichever they
    need; pre-connection MPRIS calls are no-ops.
    """

    def __init__(
        self, loop: asyncio.AbstractEventLoop, macs: list[str]
    ) -> None:
        self.loop = loop
        self.macs = macs
        self.conn: Connection | None = None
        self.client: snapcast.control.Snapclient | None = None
        self.reconnect_task: asyncio.Task | None = None
        # Strong-reference fire-and-forget tasks so the event loop's weak
        # refs don't let them be GC'd mid-execution (asyncio docs warn).
        self.bg_tasks: set[asyncio.Task] = set()
        self.player = MediaPlayer2Player(
            on_play=lambda: self.control("play"),
            on_pause=lambda: self.control("pause"),
            on_play_pause=lambda: self.control("playPause"),
            on_stop=lambda: self.control("stop"),
            on_next=lambda: self.control("next"),
            on_previous=lambda: self.control("previous"),
            on_volume_set=self.on_volume_set,
        )

    def schedule(self, coro: Coroutine) -> asyncio.Task:
        task = self.loop.create_task(coro)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task

    # --- MPRIS <- snapserver (state push) ------------------------------
    def refresh(self) -> None:
        try:
            if self.conn is None or self.client is None:
                return
            s = client_stream(self.conn.server, self.client)
            logger.debug("refresh: client.volume=%s stream=%s status=%s",
                         self.client.volume,
                         s.identifier if s is not None else None,
                         s.status if s is not None else None)
            md = stream_metadata(self.conn.host, s)
            if md is not None:
                self.player.update_metadata(md)
            self.player.update_playback_status(playback_status(s))
            self.player.update_capabilities(stream_capabilities(s))
            if self.client.volume is not None:
                self.player.update_volume(self.client.volume / 100.0)
        except Exception:
            logger.exception("refresh failed")

    # --- MPRIS -> snapserver (commands) --------------------------------
    async def set_volume(self, percent: int) -> None:
        if self.client is None:
            return
        await self.client.set_volume(percent)
        self.refresh()

    def control(self, command: str) -> None:
        # Affects every listener on the stream (intended multi-room semantic).
        if self.conn is None:
            return
        self.schedule(control_stream(
            self.conn.server,
            client_stream(self.conn.server, self.client),
            command,
        ))

    def on_volume_set(self, v: float) -> None:
        # MPRIS Volume is a float 0.0-1.0; snapserver expects 0-100 int.
        self.schedule(self.set_volume(int(round(v * 100))))

    # --- snapserver callbacks ------------------------------------------
    def wire_entity_callbacks(self) -> None:
        # Hook stream (property/metadata) and group (volume/mute/rebind, where
        # snapcast routes client volume) updates. Re-run on every sync: freshly
        # appeared streams/groups carry none of our callbacks.
        if self.conn is None:
            return
        for s in self.conn.server.streams:
            s.set_callback(lambda _stream: self.refresh())
        for g in self.conn.server.groups:
            g.set_callback(lambda _group: self.refresh())

    def on_server_update(self) -> None:
        self.wire_entity_callbacks()
        self.refresh()

    def on_new_client(self, new_client: snapcast.control.Snapclient) -> None:
        # Catch a snapclient that registered late (separate unit).
        if self.client is None and new_client.identifier in self.macs:
            self.client = new_client
            logger.info("snapclient registered: %s", new_client.identifier)
        self.refresh()

    def on_disconnect(self, exception: Exception | None) -> None:
        # Stale state is untrustworthy: clear MPRIS until on_connect re-syncs,
        # then drive our own reconnect (Snapserver(reconnect=False)).
        if self.conn is None:
            return
        logger.info("snapserver disconnected (%s); clearing MPRIS state", exception)
        self.client = None
        self.player.update_playback_status("Stopped")
        self.player.update_metadata({})
        self.player.update_capabilities({
            "CanPlay": False, "CanPause": False,
            "CanGoNext": False, "CanGoPrevious": False, "CanSeek": False,
        })
        if self.reconnect_task is None or self.reconnect_task.done():
            self.reconnect_task = self.schedule(
                _reconnect_with_backoff(self.conn.server)
            )

    def on_connect(self) -> None:
        # Post-synchronize(): matching-id entities keep our callbacks, new
        # ones don't. Re-identify the client and re-wire to cover both.
        if self.conn is None:
            return
        logger.info("snapserver (re)connected; re-syncing MPRIS state")
        self.client = identify_client(self.conn.server, self.macs)
        self.wire_entity_callbacks()
        self.refresh()

    # --- lifecycle -----------------------------------------------------
    async def connect(
        self, resolve: Callable[[], tuple[str, int] | None]
    ) -> None:
        """Connect (retrying with backoff), wire callbacks, seed MPRIS state."""
        server, host = await _connect_with_backoff(self.loop, resolve)
        self.conn = Connection(server, host)

        self.client = identify_client(server, self.macs)
        if self.client is None:
            logger.info("local MAC %s not in snapserver roster yet; waiting "
                        "for snapclient to register", self.macs)

        server.set_on_update_callback(self.on_server_update)
        server.set_new_client_callback(self.on_new_client)
        server.set_on_disconnect_callback(self.on_disconnect)
        server.set_on_connect_callback(self.on_connect)
        self.wire_entity_callbacks()
        self.refresh()  # seed

    def shutdown(self) -> None:
        logger.info("shutting down")
        if self.reconnect_task is not None and not self.reconnect_task.done():
            self.reconnect_task.cancel()
        if self.conn is not None:
            self.conn.server.stop()


async def run(
    resolve: Callable[[], tuple[str, int] | None],
    bus_type: BusType,
) -> None:
    loop = asyncio.get_running_loop()
    bridge = MprisBridge(loop, local_mac_addresses())

    # Claim the D-Bus name before connecting: with Type=dbus that marks the
    # unit started, so we come up even with no snapserver yet.
    bus = await MessageBus(bus_type=bus_type).connect()
    bus.export(ROOT_PATH, MediaPlayer2())
    bus.export(ROOT_PATH, bridge.player)
    await bus.request_name(BUS_NAME)
    logger.info("D-Bus name acquired: %s", BUS_NAME)

    await bridge.connect(resolve)

    # SIGUSR1/USR2 → pause/stop (upstream contract), forwarded to the source.
    loop.add_signal_handler(signal.SIGUSR1, lambda: bridge.control("pause"))
    loop.add_signal_handler(signal.SIGUSR2, lambda: bridge.control("stop"))

    # SIGTERM/SIGINT cancel this task; the CancelledError from the idle await
    # below unwinds through finally for orderly teardown.
    task = asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    loop.add_signal_handler(signal.SIGINT, task.cancel)

    try:
        await asyncio.Event().wait()
    finally:
        bridge.shutdown()
