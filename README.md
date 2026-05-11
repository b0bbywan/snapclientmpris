# snapclientmpris

An [MPRIS2](https://specifications.freedesktop.org/mpris-spec/2.2/) D-Bus
bridge for the local Snapcast client. It surfaces the currently playing
track (title, artist, album, art) from a snapserver and forwards MPRIS
playback commands (Play / Pause / PlayPause / Stop / Next / Previous) to
the stream's source via snapserver's `Stream.Control` — so pausing from
any room pauses every listener on the stream, the multi-room semantic
MPRIS expects (à la Spotify Connect / Airplay 2).

The MPRIS interface is published under the bus name
`org.mpris.MediaPlayer2.snapcast` (the player exposes itself as the
Snapcast source, not the client implementation detail).

## Credits

This project started life as a fork of
[`hifiberry/snapcastmpris`](https://github.com/hifiberry/snapcastmpris)
— thanks to HiFiBerry for the original idea and for the work on tying
[Snapcast](https://github.com/badaix/snapcast)'s JSON-RPC API to MPRIS2.
Without their daemon there would be nothing to fork.

The current codebase is a complete rewrite around asyncio and contains
no upstream code. The repository was subsequently renamed from
`snapcastmpris` to `snapclientmpris` to better reflect what the daemon
does — it controls the local snapclient process, not the snapserver.

## What's different from upstream

* Single asyncio event loop instead of threads + GLib MainLoop +
  websocket-client + dbus-python.
* [`python-snapcast`](https://github.com/happyleavesaoc/python-snapcast)
  for the snapserver JSON-RPC channel (no bespoke RPC / WebSocket
  client) and [`dbus-fast`](https://github.com/Bluetooth-Devices/dbus-fast)
  for the MPRIS interface (no GLib).
* Picks up track metadata from the `Stream.OnProperties` snapserver
  event (snapserver ≥ 0.27) and surfaces it as `xesam:*` / `mpris:*`
  keys, so MPRIS clients see the actual track title / artist / album.
* MPRIS Play / Pause / Next / Previous / Stop are forwarded to the
  stream's source via `Stream.Control` rather than toggling the local
  client's mute, so pausing from one room pauses everyone on the
  stream. Capabilities (`CanPlay` / `CanPause` / `CanGoNext` /
  `CanGoPrevious` / `CanSeek`) are mirrored from the stream's
  properties, so MPRIS clients only enable the buttons the source
  actually supports.
* Configuration is resolved from
  `$XDG_CONFIG_HOME/snapclientmpris/snapclientmpris.conf` with
  `/etc/snapclientmpris.conf` as fallback. An example template ships at
  `/usr/share/snapclientmpris/snapclientmpris.conf`.
* The `dbus-bus` config key chooses between the session bus (default,
  for a `systemctl --user` deployment) and the system bus (legacy
  hifiberry-style, runs as `_snapclient` with a shipped D-Bus policy).
* The ALSA volume sync and the HiFiBerry pause-all integration are
  intentionally dropped; they were tied to the original HiFiBerry
  appliance and don't fit the [Odio](https://apt.odio.love) target.

## Install

From the Odio APT repository:

```sh
curl -fsSL https://apt.odio.love/key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/odio.gpg
echo "deb [signed-by=/usr/share/keyrings/odio.gpg] https://apt.odio.love stable main" \
    | sudo tee /etc/apt/sources.list.d/odio.list
sudo apt update
sudo apt install snapclientmpris
```

The package depends on `snapclient`, so APT pulls it in automatically.
Two bridge units are shipped (neither auto-enabled); pick whichever
fits your setup — enabling the bridge unit also starts
`snapclient.service` via `Wants=`.

```sh
# User mode (default, session bus)
systemctl --user enable --now snapclientmpris.service

# System mode (legacy hifiberry-style, runs as _snapclient on the system bus)
sudo cp /usr/share/snapclientmpris/snapclientmpris.conf /etc/snapclientmpris.conf
sudo sed -i 's/^dbus-bus = session/dbus-bus = system/' /etc/snapclientmpris.conf
sudo systemctl enable --now snapclientmpris.service
```

In system mode the daemon owns `org.mpris.MediaPlayer2.snapcast` on
the system bus; the package ships the matching D-Bus policy at
`/usr/share/dbus-1/system.d/org.mpris.MediaPlayer2.snapcast.conf`
(grants `_snapclient` ownership, allows any local user to talk to it).

## Configuration

```ini
# Snapcast server IP. Leave commented to use Zeroconf auto-discovery.
# server = 192.168.1.100
# Override the JSON-RPC control port. Almost never needed: snapserver
# defaults to 1705, and snapserver >= 0.33 advertises the actual port via
# _snapcast-ctrl._tcp. Only useful if you've changed snapserver's TCP
# control port AND you run snapserver < 0.33 (e.g. 0.31 in Debian trixie).
# control-port = 1705

# D-Bus bus: session (default) or system.
dbus-bus = session

```

## Architecture

```
  remote                            local host
  ----------                        ------------------------------------

                          audio       +-------------+    +----------+
  +---------------+  ---------------> | snapclient  | -> | speakers |
  |  snapserver   |                   | (own unit)  |    +----------+
  |               |                   +-------------+
  | JSON-RPC :1705| <-- python-snapcast --+
  +---------------+      (control + events) |
                                            v
                                  +---------------------------+
                                  | snapclientmpris daemon    |
                                  | (this package, asyncio)   |
                                  +-------------+-------------+
                                                | D-Bus (dbus-fast)
                                                v
                                  +---------------------------+
                                  | MPRIS2 clients            |
                                  | (gnome-music, playerctl)  |
                                  +---------------------------+
```

The daemon does **not** spawn snapclient. Snapclient runs as its own
service (`snapclient.service` from the `snapclient` Debian package);
the shipped systemd units pull it in via `Wants=snapclient.service`
and order `After=snapclient.service`, so enabling
`snapclientmpris.service` is enough.

Four Python modules:

* [`snapclientmpris/cli.py`](snapclientmpris/cli.py) — entry point.
  Parses CLI flags, loads the config file, resolves the snapserver
  address (explicit value or Zeroconf discovery), then hands off to
  the `run()` coroutine.
* [`snapclientmpris/snapclientmpris.py`](snapclientmpris/snapclientmpris.py)
  — asyncio orchestration. Connects to the snapserver, matches this
  host to its snapserver-side client by MAC, exports the MPRIS
  interface, and wires the snapserver stream/client callbacks to a
  single `refresh()` that re-publishes PlaybackStatus, Metadata,
  Volume and capabilities.
* [`snapclientmpris/mpris.py`](snapclientmpris/mpris.py) —
  `MediaPlayer2` and `MediaPlayer2.Player` `ServiceInterface`
  subclasses for dbus-fast (D-Bus interface definitions only).
* [`snapclientmpris/translate.py`](snapclientmpris/translate.py) —
  pure helpers that map snapserver's MPRIS-like metadata to
  `xesam:*` / `mpris:*` keys and snapserver stream state to an
  MPRIS `PlaybackStatus`. No D-Bus or asyncio dependencies, so
  fully unit-testable in isolation.

## Signals

* `SIGUSR1` — `Stream.Control Pause` on the bound stream.
* `SIGUSR2` — `Stream.Control Stop` on the bound stream.

## Build a .deb

Build-deps (per `debian/control`): `debhelper-compat (= 13)`,
`dh-python`, `python3`, `python3-setuptools`. Then `dpkg-buildpackage
-b -us -uc` on Debian trixie or a derivative produces the `.deb`. The
runtime deps (`python3-snapcast`, `python3-dbus-fast`,
`python3-zeroconf`, `snapclient`) are resolved by APT at install
time, not at build time.

## Continuous integration

`.github/workflows/build.yml` runs:

* **lint** on every PR to `master` — `ruff`, `mypy` and `pytest`.
* **deb** on every PR and on `v*` tags — `dpkg-buildpackage` inside a
  `debian:trixie` container; on tags, syncs `debian/changelog` with
  the tag (rewriting `-rc/-beta/-alpha` to Debian-sortable `~rc/...`
  suffixes) before building.
* **release** on `v*` tags — attaches the `.deb` to the GitHub
  release, flagging `-rc/-beta/-alpha` tags as prereleases.
* **notify-apt-repo** on `v*` tags — dispatches to
  [`b0bbywan/odio-apt-repo`](https://github.com/b0bbywan/odio-apt-repo)
  so the new `.deb` is picked up by `apt.odio.love`.

## Used in

* [Odio](https://github.com/b0bbywan/odios) — the Odio streamer
  installer turns a Linux box (typically a Raspberry Pi) into a
  multi-room audio appliance; snapclientmpris is its per-room MPRIS
  layer on top of snapcast.

## License

MIT — see [LICENSE](LICENSE).
