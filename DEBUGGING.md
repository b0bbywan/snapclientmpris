# Debugging

Quick reference for poking at the bridge, its D-Bus interface, and the
upstream snapserver. Most commands assume the user-mode (session bus)
deployment; the system-mode equivalents drop `--user` and run as root.

## The daemon

```sh
# Status & logs
systemctl --user status snapclientmpris.service
journalctl --user -u snapclientmpris.service -f
journalctl --user -u snapclientmpris.service -n 200 --no-pager

# Restart
systemctl --user restart snapclientmpris.service

# Run in the foreground with debug logging (bypass systemd)
systemctl --user stop snapclientmpris.service
snapclientmpris -v
```

`-v` flips the daemon to `DEBUG`; the most useful lines are
`refresh:` (volume / stream / status snapshot on every event) and
`control_stream:` (commands forwarded to the source).

System-mode equivalents: drop `--user`, the unit runs as `_snapclient`.

### Signals

`SIGUSR1` pauses the bound stream, `SIGUSR2` stops it (forwarded via
`Stream.Control` — affects every listener on the stream).

```sh
systemctl --user kill -s SIGUSR1 snapclientmpris.service   # pause
systemctl --user kill -s SIGUSR2 snapclientmpris.service   # stop
```

## The MPRIS bus

Bus name `org.mpris.MediaPlayer2.snapcast`, object path
`/org/mpris/MediaPlayer2`, interfaces `org.mpris.MediaPlayer2` (root)
and `org.mpris.MediaPlayer2.Player` (playback).

```sh
# Is it owned?
busctl --user list | grep MediaPlayer2.snapcast

# Full interface dump (methods, properties, signals)
busctl --user introspect org.mpris.MediaPlayer2.snapcast /org/mpris/MediaPlayer2

# Read properties
busctl --user get-property org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player PlaybackStatus
busctl --user get-property org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player Metadata
busctl --user get-property org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player Volume

# Call playback methods
busctl --user call org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player PlayPause
busctl --user call org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player Next

# Set volume (0.0 – 1.0)
busctl --user set-property org.mpris.MediaPlayer2.snapcast \
    /org/mpris/MediaPlayer2 org.mpris.MediaPlayer2.Player Volume d 0.5

# Watch PropertiesChanged in real time
busctl --user monitor org.mpris.MediaPlayer2.snapcast
```

The high-level equivalent (no path / interface boilerplate) is
[`playerctl`](https://github.com/altdesktop/playerctl):

```sh
playerctl -p snapcast status
playerctl -p snapcast metadata
playerctl -p snapcast play-pause
playerctl -p snapcast volume 0.5
playerctl -p snapcast --follow status
```

Session-activation check — `playerctl` (or any MPRIS client) asking
for the bus name should start the unit if it isn't running:

```sh
systemctl --user stop snapclientmpris.service
playerctl -p snapcast status
systemctl --user status snapclientmpris.service   # should now be active
```

System-mode deployments swap `busctl --user` for `busctl` (system bus)
and run all MPRIS clients as a user the shipped D-Bus policy allows.

## The snapserver JSON-RPC channel

Snapserver speaks newline-delimited JSON-RPC 2.0 over TCP, default
port `1705`. Pipe through `jq` for readable output (`apt install jq`).

```sh
SERVER=192.168.1.100   # or whatever Zeroconf resolved to

# Full state (groups, streams, clients, server info)
echo '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}' \
    | nc -q1 $SERVER 1705 | jq .

# Just the streams (id, status, properties, metadata)
echo '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}' \
    | nc -q1 $SERVER 1705 \
    | jq '.result.server.groups[].stream_id, .result.server.streams[] | {id, status, metadata, properties}'

# Clients seen by snapserver (match .host.mac to one of your local NICs)
echo '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}' \
    | nc -q1 $SERVER 1705 \
    | jq '.result.server.groups[].clients[] | {id, name: .host.name, mac: .host.mac, connected: .connected}'

# Control a stream's source (play / pause / playPause / stop / next / previous)
echo '{"id":1,"jsonrpc":"2.0","method":"Stream.Control","params":{"id":"Spotify","command":"playPause","params":{}}}' \
    | nc -q1 $SERVER 1705 | jq .

# Set per-client volume (0–100, identifier is the MAC)
echo '{"id":1,"jsonrpc":"2.0","method":"Client.SetVolume","params":{"id":"aa:bb:cc:dd:ee:ff","volume":{"muted":false,"percent":50}}}' \
    | nc -q1 $SERVER 1705 | jq .
```

Stream commands only work on sources that advertise
`canControl: true` in `stream.properties` — the daemon mirrors that
flag to the matching MPRIS `Can*` properties, so if `playerctl` shows
buttons greyed out, check the stream's properties first.

## Zeroconf discovery

The daemon falls back to mDNS when `server` isn't in the config.
Snapserver ≥ 0.33 advertises `_snapcast-ctrl._tcp` (host + control
port); older versions only advertise `_snapcast._tcp` (audio port).

```sh
avahi-browse -rt _snapcast-ctrl._tcp
avahi-browse -rt _snapcast._tcp
```

No output ⇒ check that snapserver is running and mDNS isn't blocked
between this host and the server (firewall, VLAN, Wi-Fi AP isolation).

## "No metadata / wrong client" — MAC mismatch

The daemon picks its snapserver-side `Snapclient` by matching one of
the local NICs' MAC addresses against `client.host.mac`. If the local
snapclient registered with a MAC the daemon can't see (e.g. it's bound
to a bridge interface that's `down` at lookup time), `refresh:` will
log `client.volume=None stream=None` and MPRIS stays empty.

```sh
# Local MACs the daemon would consider (up, non-loopback)
for d in /sys/class/net/*/; do
    iface=$(basename "$d")
    [ "$iface" = "lo" ] && continue
    [ "$(cat "$d/operstate" 2>/dev/null)" = "down" ] && continue
    printf '%-12s %s\n' "$iface" "$(cat "$d/address")"
done

# MACs snapserver actually sees
echo '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}' \
    | nc -q1 $SERVER 1705 \
    | jq -r '.result.server.groups[].clients[] | "\(.host.name)\t\(.host.mac)\t\(.connected)"'
```

The intersection must be non-empty. If it isn't, check snapclient's
`--hostID` / interface binding (it usually picks one NIC at startup
and identifies with that MAC forever).

## The local snapclient (audio side)

The daemon does not run snapclient; the shipped systemd units only
`Wants=snapclient.service`. If audio is silent but MPRIS metadata
looks right, the problem is downstream:

```sh
systemctl status snapclient.service
journalctl -u snapclient -f
```
