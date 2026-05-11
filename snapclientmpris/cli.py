"""CLI entry point: argparse, config-file parsing, Zeroconf discovery.

Kept separate from the daemon runtime (``snapclientmpris.snapclientmpris``)
so the bootstrap surface (argument parsing, config resolution, host
discovery) is testable in isolation from the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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


def discover_snapserver() -> str | None:
    """Return the snapserver host discovered via Zeroconf, or ``None``."""
    zc = Zeroconf()
    try:
        info = zc.get_service_info(
            "_snapcast._tcp.local.",
            "Snapcast._snapcast._tcp.local.",
            timeout=3000,
        )
        if info is None or info.port is None:
            return None
        for addr in info.parsed_addresses(IPVersion.V4Only):
            if addr != "0.0.0.0":
                return str(addr)
        return None
    finally:
        zc.close()


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
    host = cfg.get("server") or None
    if not host:
        host = discover_snapserver()
        if not host:
            logger.critical("no snapserver configured and Zeroconf discovery failed")
            sys.exit(1)
        logger.info("discovered snapserver via Zeroconf: %s", host)

    try:
        control_port = int(cfg.get("control-port") or SNAPSERVER_CONTROL_PORT)
    except ValueError:
        logger.warning("invalid control-port %r; using default %d",
                       cfg.get("control-port"), SNAPSERVER_CONTROL_PORT)
        control_port = SNAPSERVER_CONTROL_PORT
    bus_choice = (cfg.get("dbus-bus") or "session").lower()
    if bus_choice == "session":
        bus_type = BusType.SESSION
    elif bus_choice == "system":
        bus_type = BusType.SYSTEM
    else:
        # Fail loudly rather than silently picking a default: a typo in
        # dbus-bus would otherwise land the daemon on a different (often
        # more privileged) bus than intended.
        logger.critical("invalid dbus-bus %r (expected 'session' or 'system')",
                        bus_choice)
        sys.exit(1)
    logger.info("using %s D-Bus", bus_choice)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(host, control_port, bus_type))


if __name__ == "__main__":
    main()
