"""Tests for the MPRIS metadata translation + player state machine."""

from snapclientmpris.mpris import (
    MediaPlayer2Player,
    snapserver_to_playback_status,
    translate_snapserver_metadata,
)


# --- translate_snapserver_metadata -------------------------------------
def test_translate_empty():
    assert translate_snapserver_metadata({}) == {}


def test_translate_only_snapcast_url():
    md = translate_snapserver_metadata({}, snapcast_url="snapcast://h/s")
    assert md["xesam:url"].value == "snapcast://h/s"
    assert md["xesam:url"].signature == "s"


def test_translate_title_artist_album():
    md = translate_snapserver_metadata(
        {"title": "Foo", "artist": "Bar", "album": "Baz"}
    )
    assert md["xesam:title"].value == "Foo"
    assert md["xesam:artist"].value == ["Bar"]
    assert md["xesam:artist"].signature == "as"
    assert md["xesam:album"].value == "Baz"


def test_translate_artist_list_preserved():
    md = translate_snapserver_metadata({"artist": ["A", "B"]})
    assert md["xesam:artist"].value == ["A", "B"]


def test_translate_album_artist_string_wrapped():
    md = translate_snapserver_metadata({"albumArtist": "Y"})
    assert md["xesam:albumArtist"].value == ["Y"]
    assert md["xesam:albumArtist"].signature == "as"


def test_translate_genre_string_wrapped():
    md = translate_snapserver_metadata({"genre": "Jazz"})
    assert md["xesam:genre"].value == ["Jazz"]


def test_translate_duration_seconds_to_microseconds():
    md = translate_snapserver_metadata({"duration": 12.5})
    assert md["mpris:length"].value == 12_500_000
    assert md["mpris:length"].signature == "x"


def test_translate_track_url_overrides_snapcast_url():
    md = translate_snapserver_metadata(
        {"url": "https://t.example/song"},
        snapcast_url="snapcast://h/s",
    )
    assert md["xesam:url"].value == "https://t.example/song"


def test_translate_track_and_disc_number_to_int():
    md = translate_snapserver_metadata({"trackNumber": "3", "discNumber": 1})
    assert md["xesam:trackNumber"].value == 3
    assert md["xesam:trackNumber"].signature == "i"
    assert md["xesam:discNumber"].value == 1


def test_translate_art_url():
    md = translate_snapserver_metadata({"artUrl": "https://example/cover.png"})
    assert md["mpris:artUrl"].value == "https://example/cover.png"


# --- snapserver_to_playback_status -------------------------------------
def test_snapserver_to_playback_status():
    assert snapserver_to_playback_status("playing") == "Playing"
    assert snapserver_to_playback_status("idle") == "Paused"
    assert snapserver_to_playback_status(None) == "Stopped"
    assert snapserver_to_playback_status("unknown") == "Stopped"


# --- MediaPlayer2Player state updates ----------------------------------
def _spy_emit(player, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(player, "emit_properties_changed", calls.append)
    return calls


def test_update_volume_emits_once(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_volume(0.5)
    assert calls == [{"Volume": 0.5}]


def test_update_volume_dedups_identical_values(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_volume(0.5)
    player.update_volume(0.5)
    assert calls == [{"Volume": 0.5}]


def test_update_volume_clamps_to_unit_interval(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_volume(0.5)  # seed away from the default 1.0
    player.update_volume(1.5)  # clamps to 1.0
    player.update_volume(-0.5)  # clamps to 0.0
    assert calls == [{"Volume": 0.5}, {"Volume": 1.0}, {"Volume": 0.0}]


def test_update_playback_status_ignores_invalid(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_playback_status("Bogus")
    assert calls == []


def test_update_playback_status_dedups(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_playback_status("Playing")
    player.update_playback_status("Playing")
    assert calls == [{"PlaybackStatus": "Playing"}]


def test_update_metadata_emits(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_metadata({"a": 1})
    assert calls == [{"Metadata": {"a": 1}}]


# --- update_capabilities -----------------------------------------------
def test_update_capabilities_emits_changed(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_capabilities({
        "CanPlay": True, "CanPause": True,
        "CanGoNext": True, "CanGoPrevious": False, "CanSeek": False,
    })
    # Only flags that flipped from the False default get emitted.
    assert calls == [{"CanPlay": True, "CanPause": True, "CanGoNext": True}]


def test_update_capabilities_dedups(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_capabilities({"CanPlay": True})
    player.update_capabilities({"CanPlay": True})
    assert calls == [{"CanPlay": True}]


def test_update_capabilities_partial_change_only_emits_diff(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_capabilities({"CanPlay": True, "CanPause": True})
    player.update_capabilities({"CanPlay": True, "CanPause": False})
    assert calls == [
        {"CanPlay": True, "CanPause": True},
        {"CanPause": False},
    ]


def test_update_capabilities_unknown_key_ignored(monkeypatch):
    player = MediaPlayer2Player()
    calls = _spy_emit(player, monkeypatch)
    player.update_capabilities({"CanFlyToTheMoon": True})
    assert calls == []


def test_update_capabilities_reflected_in_properties():
    player = MediaPlayer2Player()
    player.update_capabilities({
        "CanPlay": True, "CanPause": True, "CanGoNext": True,
        "CanControl": True,
    })
    assert player.CanPlay is True
    assert player.CanPause is True
    assert player.CanGoNext is True
    assert player.CanGoPrevious is False
    assert player.CanSeek is False
    assert player.CanControl is True


def test_can_control_defaults_false():
    # Before any refresh() runs there's no stream bound, so CanControl
    # must report False — the hardcoded True we used to ship lied to
    # MPRIS clients in that state.
    assert MediaPlayer2Player().CanControl is False


# --- Method → callback wiring ------------------------------------------
def test_next_invokes_callback():
    calls = []
    player = MediaPlayer2Player(on_next=lambda: calls.append("next"))
    player.Next()
    assert calls == ["next"]


def test_previous_invokes_callback():
    calls = []
    player = MediaPlayer2Player(on_previous=lambda: calls.append("prev"))
    player.Previous()
    assert calls == ["prev"]


def test_next_previous_no_callback_noop():
    player = MediaPlayer2Player()
    # Should simply do nothing instead of crashing.
    player.Next()
    player.Previous()
