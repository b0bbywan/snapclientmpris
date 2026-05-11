"""Tests for the snapserver -> MPRIS translation helpers."""

from snapclientmpris.translate import (
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
    assert snapserver_to_playback_status("paused") == "Paused"
    assert snapserver_to_playback_status("stopped") == "Stopped"
    # Idle snapserver streams have no track loaded -> Stopped, not Paused.
    assert snapserver_to_playback_status("idle") == "Stopped"
    assert snapserver_to_playback_status(None) == "Stopped"
    assert snapserver_to_playback_status("unknown") == "Stopped"
    # Case-insensitive (properties.playbackStatus may capitalise).
    assert snapserver_to_playback_status("Playing") == "Playing"
