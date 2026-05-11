"""MPRIS2 D-Bus interface, exposed via dbus-fast.

The two ServiceInterface subclasses below correspond to the two
interfaces every MPRIS2 player must implement on the object path
``/org/mpris/MediaPlayer2``:

* ``org.mpris.MediaPlayer2``       — identity + capabilities (root)
* ``org.mpris.MediaPlayer2.Player`` — playback state + controls

Behaviour is driven from the outside: callbacks injected at construction
time handle Play/Pause/Stop/PlayPause, and ``update_playback_status`` /
``update_metadata`` push state changes back to subscribed MPRIS clients.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from dbus_fast import Variant
from dbus_fast.errors import DBusError
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"

logger = logging.getLogger(__name__)

ROOT_PATH = "/org/mpris/MediaPlayer2"
MEDIA_PLAYER_IFACE = "org.mpris.MediaPlayer2"
BUS_NAME = f"{MEDIA_PLAYER_IFACE}.snapcast"
PLAYER_IFACE = f"{MEDIA_PLAYER_IFACE}.Player"

IDENTITY = "Snapcast client"
DESKTOP_ENTRY = "snapclientmpris"


class MediaPlayer2(ServiceInterface):
    """Root MPRIS interface."""

    def __init__(self) -> None:
        super().__init__(MEDIA_PLAYER_IFACE)

    @method()
    def Raise(self):
        raise DBusError(NOT_SUPPORTED, "Raise is not supported")

    @method()
    def Quit(self):
        raise DBusError(NOT_SUPPORTED, "Quit is not supported")

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return IDENTITY

    @dbus_property(access=PropertyAccess.READ)
    def DesktopEntry(self) -> "s":
        return DESKTOP_ENTRY

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class MediaPlayer2Player(ServiceInterface):
    """Player MPRIS interface — playback state and controls."""

    def __init__(
        self,
        on_play: Callable[[], None] | None = None,
        on_pause: Callable[[], None] | None = None,
        on_play_pause: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
        on_volume_set: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(PLAYER_IFACE)
        self._playback_status = "Stopped"
        self._metadata: dict = {}
        self._volume = 1.0
        self._on_play = on_play
        self._on_pause = on_pause
        self._on_play_pause = on_play_pause
        self._on_stop = on_stop
        self._on_volume_set = on_volume_set

    # --- MPRIS methods ------------------------------------------------
    @method()
    def Play(self):
        logger.debug("MPRIS Play")
        if self._on_play:
            self._on_play()

    @method()
    def Pause(self):
        logger.debug("MPRIS Pause")
        if self._on_pause:
            self._on_pause()

    @method()
    def PlayPause(self):
        logger.debug("MPRIS PlayPause")
        if self._on_play_pause:
            self._on_play_pause()

    @method()
    def Stop(self):
        logger.debug("MPRIS Stop")
        if self._on_stop:
            self._on_stop()

    @method()
    def Next(self):
        pass

    @method()
    def Previous(self):
        pass

    @method()
    def Seek(self, Offset: "x"):  # noqa: N803, ARG002
        raise DBusError(NOT_SUPPORTED, "Seek is not supported")

    @method()
    def SetPosition(self, TrackId: "o", Position: "x"):  # noqa: N803, ARG002
        raise DBusError(NOT_SUPPORTED, "SetPosition is not supported")

    @method()
    def OpenUri(self, Uri: "s"):  # noqa: N803, ARG002
        raise DBusError(NOT_SUPPORTED, "OpenUri is not supported")

    # --- MPRIS properties --------------------------------------------
    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._playback_status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._metadata

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True

    @dbus_property()
    def Volume(self) -> "d":
        return self._volume

    @Volume.setter  # type: ignore[no-redef]
    def Volume(self, val: "d") -> None:
        clamped = max(0.0, min(1.0, float(val)))
        logger.debug("MPRIS Set Volume: %.3f -> %.3f", self._volume, clamped)
        if clamped == self._volume:
            # Still trigger the backend hook so the snapserver-side state
            # follows even when the value is identical (idempotent retry).
            if self._on_volume_set:
                self._on_volume_set(clamped)
            return
        self._volume = clamped
        # Emit synchronously so every MPRIS subscriber (gnome-music, KDE
        # plasma, etc.) learns about the change. The follow-up refresh()
        # triggered by client.OnVolumeChanged will early-return in
        # update_volume() because self._volume already matches — no double
        # emit.
        self.emit_properties_changed({"Volume": clamped})
        if self._on_volume_set:
            self._on_volume_set(clamped)

    # --- External update API -----------------------------------------
    def update_playback_status(self, status: str) -> None:
        """Set PlaybackStatus and notify subscribers."""
        if status not in ("Playing", "Paused", "Stopped"):
            logger.warning("ignoring invalid playback status: %s", status)
            return
        if status == self._playback_status:
            return
        self._playback_status = status
        self.emit_properties_changed({"PlaybackStatus": status})

    def update_metadata(self, metadata: dict) -> None:
        """Replace Metadata and notify subscribers."""
        self._metadata = metadata
        self.emit_properties_changed({"Metadata": metadata})

    def update_volume(self, volume: float) -> None:
        """Set Volume from external state (e.g. snapserver event)."""
        clamped = max(0.0, min(1.0, float(volume)))
        if clamped == self._volume:
            logger.debug("update_volume: no change (%.3f), skip emit", clamped)
            return
        logger.debug("update_volume: %.3f -> %.3f, emitting PropertiesChanged",
                     self._volume, clamped)
        self._volume = clamped
        self.emit_properties_changed({"Volume": clamped})


# --- Helpers ---------------------------------------------------------
def translate_snapserver_metadata(snap_md: dict, snapcast_url: str | None = None) -> dict:
    """Convert snapserver's MPRIS-like metadata dict to xesam:/mpris: keys,
    each wrapped in a ``Variant`` for dbus-fast.

    ``snap_md`` is the dict found at ``params.properties.metadata`` of a
    ``Stream.OnProperties`` notification (also exposed by
    ``snapcast.control.Snapstream.metadata``).
    """
    out: dict = {}

    def setv(key: str, sig: str, val) -> None:
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


def snapserver_to_playback_status(stream_status: str | None) -> str:
    """Map a snapserver stream.status string to an MPRIS PlaybackStatus."""
    return {
        "playing": "Playing",
        "idle": "Paused",
    }.get(stream_status or "", "Stopped")
