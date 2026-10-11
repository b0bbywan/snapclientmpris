"""Tests for the pure state helpers in snapclientmpris.snapclientmpris."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import snapclientmpris.snapclientmpris as scm
from snapclientmpris.snapclientmpris import (
    Connection,
    MprisBridge,
    _connect_with_backoff,
    client_stream,
    control_stream,
    identify_client,
    playback_status,
    stream_capabilities,
    stream_metadata,
    stream_position,
)


# --- client_stream -----------------------------------------------------
def test_client_stream_no_client():
    assert client_stream(SimpleNamespace(), None) is None


def test_client_stream_no_group():
    assert client_stream(SimpleNamespace(), SimpleNamespace(group=None)) is None


def test_client_stream_resolves_via_group():
    stream = SimpleNamespace(identifier="default")
    server = SimpleNamespace(stream=lambda sid: stream if sid == "stream-id" else None)
    client = SimpleNamespace(group=SimpleNamespace(stream="stream-id"))
    assert client_stream(server, client) is stream


# --- stream_metadata ---------------------------------------------------
def test_stream_metadata_no_stream_returns_none():
    assert stream_metadata("host", None) is None


def test_stream_metadata_empty_metadata_still_has_snapcast_url():
    stream = SimpleNamespace(identifier="s1", metadata=None, status="idle")
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["xesam:url"].value == "snapcast://h/s1"
    assert md["xesam:url"].signature == "s"


def test_stream_metadata_track_url_wins_over_snapcast_url():
    stream = SimpleNamespace(
        identifier="s1", metadata={"url": "https://t.example/song"}, status="playing"
    )
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["xesam:url"].value == "https://t.example/song"


def test_stream_metadata_includes_snapcast_stream_keys():
    # Custom snapcast:* keys identify which snapserver stream is bound
    # and its raw status, on top of the MPRIS-standard xesam:/mpris: keys.
    stream = SimpleNamespace(identifier="SpotifyHD", metadata=None, status="playing")
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["snapcast:stream"].value == "SpotifyHD"
    assert md["snapcast:stream"].signature == "s"
    assert md["snapcast:streamStatus"].value == "playing"
    assert md["snapcast:streamStatus"].signature == "s"


def test_stream_metadata_streamStatus_handles_none_status():
    stream = SimpleNamespace(identifier="MPD", metadata=None, status=None)
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["snapcast:streamStatus"].value == ""


# --- playback_status ---------------------------------------------------
def test_playback_status_no_stream():
    assert playback_status(None) == "Stopped"


def test_playback_status_prefers_explicit_properties():
    # Sources with a metadata script report MPRIS state directly; we
    # take that over the snapserver-side stream.status because it
    # distinguishes "paused mid-track" from "no track loaded".
    stream = SimpleNamespace(
        properties={"playbackStatus": "paused"}, status="playing"
    )
    assert playback_status(stream) == "Paused"


def test_playback_status_falls_back_to_stream_status():
    stream = SimpleNamespace(properties={}, status="playing")
    assert playback_status(stream) == "Playing"


def test_playback_status_idle_no_metadata_script_is_stopped():
    # Plain MPD pipe with no metadata script: stream.status=idle, no
    # explicit playbackStatus -> Stopped (was Paused before, which made
    # MPRIS clients show an "active" player for an empty MPD).
    stream = SimpleNamespace(properties=None, status="idle")
    assert playback_status(stream) == "Stopped"


# --- stream_capabilities -----------------------------------------------
def test_stream_capabilities_no_stream_all_false():
    caps = stream_capabilities(None)
    assert caps == {
        "CanPlay": False,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
    }


def test_stream_capabilities_no_properties_all_false():
    stream = SimpleNamespace(properties=None)
    assert stream_capabilities(stream) == {
        "CanPlay": False,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
    }


def test_stream_capabilities_full():
    stream = SimpleNamespace(properties={
        "canPlay": True, "canPause": True,
        "canGoNext": True, "canGoPrevious": True, "canSeek": False,
        "canControl": True,
    })
    caps = stream_capabilities(stream)
    assert caps["CanPlay"] is True
    assert caps["CanPause"] is True
    assert caps["CanGoNext"] is True
    assert caps["CanGoPrevious"] is True
    assert caps["CanSeek"] is False


def test_stream_capabilities_partial_defaults_to_false():
    stream = SimpleNamespace(properties={"canPlay": True})
    caps = stream_capabilities(stream)
    assert caps == {
        "CanPlay": True,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
    }


# --- stream_position ---------------------------------------------------
def test_stream_position_no_stream():
    assert stream_position(None) is None


def test_stream_position_not_reported():
    assert stream_position(SimpleNamespace(properties=None)) is None
    assert stream_position(SimpleNamespace(properties={"canSeek": True})) is None


def test_stream_position_seconds_to_microseconds():
    assert stream_position(SimpleNamespace(properties={"position": 13.5})) == 13_500_000
    assert stream_position(SimpleNamespace(properties={"position": 0})) == 0


def test_stream_position_ignores_non_numbers():
    assert stream_position(SimpleNamespace(properties={"position": "13.5"})) is None
    assert stream_position(SimpleNamespace(properties={"position": True})) is None


# --- control_stream ----------------------------------------------------
def _mock_server():
    server = MagicMock()
    server.stream_control = AsyncMock()
    return server


def test_control_stream_no_stream_noop():
    server = _mock_server()
    asyncio.run(control_stream(server, None, "play"))
    server.stream_control.assert_not_called()


def test_control_stream_canControl_false_noop():
    server = _mock_server()
    stream = SimpleNamespace(identifier="s1", properties={"canControl": False})
    asyncio.run(control_stream(server, stream, "play"))
    server.stream_control.assert_not_called()


def test_control_stream_properties_none_noop():
    server = _mock_server()
    stream = SimpleNamespace(identifier="s1", properties=None)
    asyncio.run(control_stream(server, stream, "play"))
    server.stream_control.assert_not_called()


def test_control_stream_forwards_command():
    server = _mock_server()
    stream = SimpleNamespace(identifier="s1", properties={"canControl": True})
    asyncio.run(control_stream(server, stream, "play"))
    server.stream_control.assert_awaited_once_with("s1", "play", {})


def test_control_stream_nests_the_command_parameters():
    # snapserver reads them under "params"; python-snapcast merges the dict
    # it is given into the request.
    server = _mock_server()
    stream = SimpleNamespace(identifier="s1", properties={"canControl": True})
    asyncio.run(control_stream(server, stream, "seek", {"offset": -5.0}))
    server.stream_control.assert_awaited_once_with("s1", "seek", {"params": {"offset": -5.0}})


# --- identify_client ---------------------------------------------------
def fake_client(identifier: str, connected: bool = True, stream: str = "s1") -> SimpleNamespace:
    callbacks: list = []
    return SimpleNamespace(
        identifier=identifier,
        connected=connected,
        volume=100,
        group=SimpleNamespace(stream=stream),
        callbacks=callbacks,
        set_callback=callbacks.append,
    )


def test_identify_client_matches_first_mac():
    c1 = fake_client("aa:bb:cc:dd:ee:ff")
    c2 = fake_client("11:22:33:44:55:66")
    server = SimpleNamespace(clients=[c1, c2])
    assert identify_client(server, ["11:22:33:44:55:66"]) is c2


def test_identify_client_no_match():
    server = SimpleNamespace(clients=[fake_client("aa:bb")])
    assert identify_client(server, ["cc:dd"]) is None


def test_identify_client_empty_macs():
    server = SimpleNamespace(clients=[fake_client("aa:bb")])
    assert identify_client(server, []) is None


def test_identify_client_prefers_connected_entry():
    # Host on wifi + ethernet: the roster keeps a stale entry for the
    # interface it no longer registers with.
    stale = fake_client("eth", connected=False)
    live = fake_client("wlan")
    server = SimpleNamespace(clients=[stale, live])
    assert identify_client(server, ["eth", "wlan"]) is live


def test_identify_client_none_connected_falls_back_to_first_match():
    c1 = fake_client("eth", connected=False)
    c2 = fake_client("wlan", connected=False)
    server = SimpleNamespace(clients=[c1, c2])
    assert identify_client(server, ["eth", "wlan"]) is c1


# --- MprisBridge client binding ----------------------------------------
def fake_bridge(clients: list, macs: list[str]) -> MprisBridge:
    streams = {
        "mpd": SimpleNamespace(identifier="mpd", metadata=None, status="idle", properties=None),
        "spotify": SimpleNamespace(identifier="spotify", metadata=None, status="playing", properties=None),
    }
    server = SimpleNamespace(clients=clients, streams=list(streams.values()), groups=[], stream=streams.get)
    for stream in server.streams:
        stream.set_callback = lambda _cb: None
    bridge = MprisBridge(loop=None, macs=macs)
    bridge.conn = Connection(server, "host")
    bridge.client = identify_client(server, macs)
    bridge.wire_entity_callbacks()
    return bridge


def test_bridge_forwards_seek_and_set_position_in_seconds():
    bridge = fake_bridge([fake_client("eth", stream="mpd")], ["eth"])
    calls: list = []
    bridge.control = lambda command, params=None: calls.append((command, params))
    bridge.player.update_capabilities({"CanSeek": True})
    bridge.player.Seek(-5_000_000)
    bridge.player.SetPosition("/org/snapcast/Track/14", 60_500_000)
    assert calls == [("seek", {"offset": -5.0}), ("setPosition", {"position": 60.5})]


def position_updates(bridge: MprisBridge) -> list:
    """Record what the bridge hands to the player's update_position."""
    updates: list = []
    bridge.player.update_position = updates.append
    return updates


def test_bridge_passes_a_position_sample_once():
    bridge = fake_bridge([fake_client("eth", stream="mpd")], ["eth"])
    updates = position_updates(bridge)
    stream = bridge.conn.server.stream("mpd")
    stream.properties = {"position": 13.5}
    bridge.refresh()
    # snapserver repeats the last sample on unrelated updates
    stream.properties = {"position": 13.5, "volume": 80}
    bridge.refresh()
    assert updates == [13_500_000]


def test_bridge_passes_each_new_position_sample():
    bridge = fake_bridge([fake_client("eth", stream="mpd")], ["eth"])
    updates = position_updates(bridge)
    stream = bridge.conn.server.stream("mpd")
    stream.properties = {"position": 13.5}
    bridge.refresh()
    stream.properties = {"position": 28.5}
    bridge.refresh()
    stream.properties = {}
    bridge.refresh()
    assert updates == [13_500_000, 28_500_000, None]


def test_bridge_takes_the_position_of_the_stream_it_moves_to():
    client = fake_client("eth", stream="mpd")
    bridge = fake_bridge([client], ["eth"])
    updates = position_updates(bridge)
    bridge.conn.server.stream("mpd").properties = {"position": 13.5}
    bridge.conn.server.stream("spotify").properties = {"position": 13.5}
    bridge.refresh()
    client.group.stream = "spotify"
    bridge.refresh()
    assert updates == [13_500_000, 13_500_000]


def test_bridge_forgets_the_position_on_disconnect():
    bridge = fake_bridge([fake_client("eth", stream="mpd")], ["eth"])
    bridge.schedule = lambda coro: coro.close()
    updates = position_updates(bridge)
    bridge.conn.server.stream("mpd").properties = {"position": 13.5}
    bridge.refresh()
    bridge.on_disconnect(None)
    assert updates == [13_500_000, None]


def test_bridge_follows_entry_that_connects():
    stale = fake_client("eth", connected=False, stream="mpd")
    ours = fake_client("wlan", connected=False, stream="spotify")
    bridge = fake_bridge([stale, ours], ["eth", "wlan"])
    assert bridge.client is stale

    ours.connected = True
    (callback,) = ours.callbacks
    callback(ours)
    assert bridge.client is ours
    assert bridge.player.PlaybackStatus == "Playing"


def test_bridge_keeps_binding_when_nothing_is_connected():
    stale = fake_client("eth", connected=False, stream="mpd")
    ours = fake_client("wlan", stream="spotify")
    bridge = fake_bridge([stale, ours], ["eth", "wlan"])
    assert bridge.client is ours

    ours.connected = False
    ours.callbacks[0](ours)
    assert bridge.client is ours


def test_bridge_ignores_callbacks_of_foreign_clients():
    other = fake_client("other-host")
    ours = fake_client("wlan")
    fake_bridge([other, ours], ["wlan"])
    assert other.callbacks == []
    assert len(ours.callbacks) == 1


def test_new_client_replaces_disconnected_binding():
    stale = fake_client("eth", connected=False, stream="mpd")
    bridge = fake_bridge([stale], ["eth", "wlan"])
    assert bridge.client is stale

    ours = fake_client("wlan", stream="spotify")
    bridge.conn.server.clients.append(ours)
    bridge.on_new_client(ours)
    assert bridge.client is ours
    assert len(ours.callbacks) == 1


def test_new_client_does_not_replace_connected_binding():
    ours = fake_client("wlan", stream="spotify")
    bridge = fake_bridge([ours], ["eth", "wlan"])
    other = fake_client("eth", stream="mpd")
    bridge.conn.server.clients.insert(0, other)
    bridge.on_new_client(other)
    assert bridge.client is ours


def test_server_update_drops_binding_deleted_from_roster():
    # Server.DeleteClient on the stale entry we were bound to.
    stale = fake_client("eth", connected=False, stream="mpd")
    ours = fake_client("wlan", connected=False, stream="spotify")
    bridge = fake_bridge([stale, ours], ["eth", "wlan"])
    assert bridge.client is stale

    bridge.conn.server.clients.remove(stale)
    bridge.on_server_update()
    assert bridge.client is ours


def test_server_update_unbinds_when_no_entry_is_left():
    ours = fake_client("wlan")
    bridge = fake_bridge([ours], ["wlan"])
    bridge.conn.server.clients.clear()
    bridge.on_server_update()
    assert bridge.client is None


# --- _connect_with_backoff ---------------------------------------------
def test_connect_with_backoff_retries_until_resolved(monkeypatch):
    # resolve() returns None (no snapserver yet) then an endpoint.
    endpoints = [None, ("host.example", 1705)]
    seen = {}

    class FakeServer:
        def __init__(self, loop, host, port, reconnect):
            seen.update(host=host, port=port, reconnect=reconnect)

        async def start(self):
            pass

    monkeypatch.setattr(scm.snapcast.control, "Snapserver", FakeServer)
    monkeypatch.setattr(scm.asyncio, "sleep", AsyncMock())

    async def go():
        loop = asyncio.get_running_loop()
        return await _connect_with_backoff(loop, lambda: endpoints.pop(0))

    server, host = asyncio.run(go())
    assert isinstance(server, FakeServer)
    assert host == "host.example"
    assert seen == {"host": "host.example", "port": 1705, "reconnect": False}


def test_connect_with_backoff_retries_on_oserror(monkeypatch):
    attempts = {"n": 0}

    class FakeServer:
        def __init__(self, loop, host, port, reconnect):
            pass

        async def start(self):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise OSError("connection refused")

    monkeypatch.setattr(scm.snapcast.control, "Snapserver", FakeServer)
    monkeypatch.setattr(scm.asyncio, "sleep", AsyncMock())

    async def go():
        loop = asyncio.get_running_loop()
        return await _connect_with_backoff(loop, lambda: ("h", 1705))

    _server, host = asyncio.run(go())
    assert attempts["n"] == 2
    assert host == "h"
