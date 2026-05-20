"""Tests for the pure state helpers in snapclientmpris.snapclientmpris."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import snapclientmpris.snapclientmpris as scm
from snapclientmpris.snapclientmpris import (
    _connect_with_backoff,
    client_stream,
    control_stream,
    identify_client,
    playback_status,
    stream_capabilities,
    stream_metadata,
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


# --- identify_client ---------------------------------------------------
def test_identify_client_matches_first_mac():
    c1 = SimpleNamespace(identifier="aa:bb:cc:dd:ee:ff")
    c2 = SimpleNamespace(identifier="11:22:33:44:55:66")
    server = SimpleNamespace(clients=[c1, c2])
    assert identify_client(server, ["11:22:33:44:55:66"]) is c2


def test_identify_client_no_match():
    server = SimpleNamespace(clients=[SimpleNamespace(identifier="aa:bb")])
    assert identify_client(server, ["cc:dd"]) is None


def test_identify_client_empty_macs():
    server = SimpleNamespace(clients=[SimpleNamespace(identifier="aa:bb")])
    assert identify_client(server, []) is None


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
