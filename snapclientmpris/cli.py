"""CLI entry point: argparse, config-file parsing, Zeroconf discovery.

Kept separate from the daemon runtime (``snapclientmpris.snapclientmpris``)
so the bootstrap surface (argument parsing, config resolution, host
discovery) is testable in isolation from the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import logging
import os
import sys

from dbus_fast import BusType
from zeroconf import IPVersion, Zeroconf

from snapclientmpris.snapclientmpris import run

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

# Zeroconf service types advertised by snapserver. The instance name is
# always "Snapcast." + the service type.
CTRL_SERVICE_TYPE = "_snapcast-ctrl._tcp.local."  # JSON-RPC control (>= 0.33)
AUDIO_SERVICE_TYPE = "_snapcast._tcp.local."  # audio stream (all versions)
HTTP_SERVICE_TYPE = "_snapcast-http._tcp.local."  # snapweb UI


class ConfigError(Exception):
    """Raised when the daemon can't start because of invalid / missing config."""


def read_config() -> dict[str, str]:
    """Parse the first existing config file as flat ``key = value`` pairs.

    The format is intentionally minimal: three keys (``server``,
    ``control-port``, ``dbus-bus``), no sections. ``#`` introduces a
    comment to end-of-line; blank and malformed lines are skipped.
    """
    for path in CONFIG_PATHS:
        try:
            with open(path) as f:
                content = f.read()
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.warning("failed to read %s: %s", path, e)
            continue
        cfg: dict[str, str] = {}
        for raw in content.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
        logger.info("read %s", path)
        return cfg
    logger.info("no config file found, using defaults")
    return {}


def _lookup_service(
    zc: Zeroconf, service_type: str
) -> tuple[str | None, int | None]:
    """Resolve the "Snapcast" instance of a service type to (host, port)."""
    info = zc.get_service_info(service_type, f"Snapcast.{service_type}", timeout=3000)
    if info is None or info.port is None:
        return None, None
    for addr in info.parsed_addresses(IPVersion.V4Only):
        if addr != "0.0.0.0":
            return str(addr), info.port
    return None, None


def discover_snapserver() -> tuple[str, int | None] | None:
    """Return ``(host, control_port)`` for the snapserver, or ``None``.

    snapserver >= 0.33 advertises its JSON-RPC control socket as
    ``_snapcast-ctrl._tcp``, which yields both the host and the actual
    control port. Older snapservers (e.g. 0.31 in Debian trixie) only
    advertise ``_snapcast._tcp`` (the audio port); in that case the host
    is returned with ``port=None`` and the caller falls back to the
    configured / default control port.
    """
    zc = Zeroconf()
    try:
        host, port = _lookup_service(zc, CTRL_SERVICE_TYPE)
        if host is not None:
            return host, port

        host, _ = _lookup_service(zc, AUDIO_SERVICE_TYPE)
        if host is not None:
            return host, None
        return None
    finally:
        zc.close()


def discover_snapweb() -> tuple[str, int] | None:
    """Return ``(host, http_port)`` for the snapweb UI, or ``None``.

    snapserver advertises its built-in web UI as ``_snapcast-http._tcp``,
    which carries the actual HTTP port (default 1780).
    """
    zc = Zeroconf()
    try:
        host, port = _lookup_service(zc, HTTP_SERVICE_TYPE)
        if host is not None and port is not None:
            return host, port
        return None
    finally:
        zc.close()


def run_discovery() -> None:
    """Probe Zeroconf for snapserver + snapweb and print findings to stdout.

    Diagnostic one-shot for ``--discover``: never starts the daemon.
    """
    server = discover_snapserver()
    web = discover_snapweb()
    if server is None and web is None:
        print("No Snapcast services found on the local network.")
        return
    if server is not None:
        host, port = server
        print(f"snapserver:  tcp://{host}" + (f":{port}" if port else ""))
    if web is not None:
        host, port = web
        print(f"snapweb:     http://{host}:{port}")


def parse_control_port(cfg: dict[str, str]) -> int:
    """Validate the configured control port (fallback only, a discovered
    port wins). Fail-fast on a typo. Raises ConfigError if unparseable.
    """
    raw_port = cfg.get("control-port") or SNAPSERVER_CONTROL_PORT
    try:
        return int(raw_port)
    except ValueError as e:
        raise ConfigError(f"invalid control-port {raw_port!r}") from e


def resolve_endpoint(
    cfg: dict[str, str], default_control_port: int
) -> tuple[str, int] | None:
    """Return ``(host, control_port)`` from config server or Zeroconf, or
    ``None`` if no host is found yet (transient, caller retries).

    A snapserver >= 0.33 advertises its control port (wins); older ones only
    give the host, so the port falls back to ``default_control_port``.
    """
    host = cfg.get("server") or None
    discovered_port: int | None = None
    if not host:
        discovered = discover_snapserver()
        if not discovered:
            return None
        host, discovered_port = discovered
    if discovered_port is not None:
        return host, discovered_port
    return host, default_control_port


def resolve_dbus_bus(cfg: dict[str, str]) -> BusType:
    """Return the BusType selected by the ``dbus-bus`` config key.

    Raises ConfigError on an unrecognised value, failing loudly avoids
    landing the daemon on a different (often more privileged) bus than
    intended through a config typo.
    """
    bus_choice = (cfg.get("dbus-bus") or "session").lower()
    if bus_choice == "session":
        return BusType.SESSION
    if bus_choice == "system":
        return BusType.SYSTEM
    raise ConfigError(
        f"invalid dbus-bus {bus_choice!r} (expected 'session' or 'system')"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="snapclientmpris",
        description=("MPRIS2 D-Bus bridge for a local Snapcast client. "
                     "Talks to snapserver and exposes the local client's "
                     "PlaybackStatus, Metadata and Volume over D-Bus. "
                     "Snapclient itself runs as its own service."),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="probe the network for snapserver and snapweb, print the result and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(levelname)s: %(name)s - %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    if not args.verbose:
        logging.getLogger("snapcast").setLevel(logging.WARNING)

    if args.discover:
        run_discovery()
        return

    cfg = read_config()
    try:
        control_port = parse_control_port(cfg)
        bus_type = resolve_dbus_bus(cfg)
    except ConfigError as e:
        logger.critical("%s", e)
        sys.exit(1)
    logger.info("%s D-Bus; snapserver resolved lazily (config host or Zeroconf)",
                "session" if bus_type == BusType.SESSION else "system")

    # Resolved lazily in run()'s connect loop so a snapserver that isn't up
    # yet doesn't abort startup. resolve() -> (host, control_port) | None.
    resolve = functools.partial(resolve_endpoint, cfg, control_port)

    # SIGTERM/SIGINT inside run() cancel the main task, asyncio.run()
    # then re-raises the CancelledError; suppress both that and the
    # pre-loop KeyboardInterrupt path for a clean exit code.
    with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
        asyncio.run(run(resolve, bus_type))


if __name__ == "__main__":
    main()
