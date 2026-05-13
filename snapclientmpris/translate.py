"""Translation helpers: snapserver state -> MPRIS values.

Kept separate from ``mpris`` (D-Bus interface definitions) and from
``snapclientmpris`` (event-loop orchestration) so that the snapserver-
to-MPRIS mapping stays a pure, easily-testable transformation with no
D-Bus or asyncio dependencies beyond ``dbus_fast.Variant``.
"""

from __future__ import annotations

from dbus_fast import Variant


def translate_snapserver_metadata(snap_md: dict, snapcast_url: str | None = None) -> dict:
    """Convert snapserver's MPRIS-like metadata dict to xesam:/mpris: keys,
    each wrapped in a ``Variant`` for dbus-fast.

    ``snap_md`` is the dict found at ``params.properties.metadata`` of a
    ``Stream.OnProperties`` notification (also exposed by
    ``snapcast.control.Snapstream.metadata``).
    """
    out: dict = {}

    def setv(key: str, sig: str, val: object) -> None:
        out[key] = Variant(sig, val)

    if snapcast_url:
        setv("xesam:url", "s", snapcast_url)

    if not snap_md:
        return out

    if "title" in snap_md:
        setv("xesam:title", "s", str(snap_md["title"]))
    if "artist" in snap_md:
        artists = snap_md["artist"]
        if isinstance(artists, str):
            artists = [artists]
        setv("xesam:artist", "as", [str(a) for a in artists])
    if "album" in snap_md:
        setv("xesam:album", "s", str(snap_md["album"]))
    if "albumArtist" in snap_md:
        aa = snap_md["albumArtist"]
        if isinstance(aa, str):
            aa = [aa]
        setv("xesam:albumArtist", "as", [str(a) for a in aa])
    if "artUrl" in snap_md:
        setv("mpris:artUrl", "s", str(snap_md["artUrl"]))
    if "trackNumber" in snap_md:
        setv("xesam:trackNumber", "i", int(snap_md["trackNumber"]))
    if "discNumber" in snap_md:
        setv("xesam:discNumber", "i", int(snap_md["discNumber"]))
    if "genre" in snap_md:
        g = snap_md["genre"]
        if isinstance(g, str):
            g = [g]
        setv("xesam:genre", "as", [str(x) for x in g])
    if "duration" in snap_md:
        # snapserver: seconds (float); MPRIS: microseconds (int64).
        setv("mpris:length", "x", int(float(snap_md["duration"]) * 1_000_000))
    if "url" in snap_md:
        # Track URL takes priority over the snapcast stream URL.
        setv("xesam:url", "s", str(snap_md["url"]))

    return out


def snapserver_to_playback_status(status: str | None) -> str:
    """Map a state string to an MPRIS PlaybackStatus.

    Accepts both snapserver's ``stream.status`` (``playing`` / ``idle``)
    and the explicit MPRIS states a source reports via
    ``stream.properties.playbackStatus`` (``playing`` / ``paused`` /
    ``stopped``). An idle stream maps to Stopped — Paused only applies
    when the source explicitly says so, because an idle snapserver
    stream means "no audio flowing", not "track is paused mid-play".
    """
    return {
        "playing": "Playing",
        "paused": "Paused",
        "stopped": "Stopped",
        "idle": "Stopped",
    }.get((status or "").lower(), "Stopped")
