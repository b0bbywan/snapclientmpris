# snapclientmpris

An [MPRIS2](https://specifications.freedesktop.org/mpris-spec/2.2/) D-Bus
bridge for the local Snapcast client. It surfaces the currently playing
track (title, artist, album, art) from a snapserver and lets any
MPRIS2-aware client pause and resume audio by toggling the local
client's mute on the server.

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
mkdir -p ~/.config/snapclientmpris
cp /usr/share/snapclientmpris/snapclientmpris.conf ~/.config/snapclientmpris/
systemctl --user daemon-reload
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

# D-Bus bus: session (default) or system.
dbus-bus = session

# Override the snapserver JSON-RPC control port (default: 1705).
# control-port = 1705
```

## Architecture

```
                   +------------+
                   | snapclient |  ← own systemd unit (snapclient.service)
                   +-----+------+
                         | audio out
                         v
+-----------+        +-----------+
| snapserver| <───── | speakers  |
+-----+-----+         (per room)
      | JSON-RPC
      v
+------------------+
| snapcast.control |  ── snapclientmpris daemon (this package)
|   (asyncio)      |       single asyncio loop, no threads, no GLib
+--------+---------+
         | dbus-fast (ServiceInterface)
         v
+------------------+
| /org/mpris/      |  ← MPRIS2 clients (gnome-music, playerctl, …)
|   MediaPlayer2   |
+------------------+
```

The daemon does **not** spawn snapclient. Snapclient runs as its own
service (`snapclient.service` from the `snapclient` Debian package);
the shipped systemd units pull it in via `Wants=snapclient.service`
and order `After=snapclient.service`, so enabling
`snapclientmpris.service` is enough.

Two Python modules:

* [`snapclientmpris/snapclientmpris.py`](snapclientmpris/snapclientmpris.py)
  — daemon entry point. Reads config, discovers the snapserver,
  matches this host to its snapserver-side client by MAC, exports the
  MPRIS interface, wires the snapserver stream/client callbacks to a
  single `refresh()` that re-publishes PlaybackStatus, Metadata and
  Volume, and runs the asyncio loop.
* [`snapclientmpris/mpris.py`](snapclientmpris/mpris.py) —
  `MediaPlayer2` and `MediaPlayer2.Player` `ServiceInterface`
  subclasses for dbus-fast, plus a helper that maps snapserver's
  MPRIS-like metadata to `xesam:*` / `mpris:*` keys.

## Signals

* `SIGUSR1` — pause (mute the local snapserver client).
* `SIGUSR2` — also pause; the daemon treats Stop as Pause since
  `snapclient.service` keeps running for instant resume.

## Build a .deb

`dpkg-buildpackage` on Debian trixie or any derivative with
`debhelper-compat (= 13)`, `dh-python`, `python3-setuptools`,
`python3-snapcast`, `python3-dbus-fast`. CI builds tagged releases
(`v*`) automatically and attaches the `.deb` to the GitHub release.

## License

MIT — see [LICENSE](LICENSE).
