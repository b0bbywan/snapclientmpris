"""Tests for the pure state helpers in snapclientmpris.snapclientmpris."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from snapclientmpris.snapclientmpris import (
    client_stream,
    control_stream,
    identify_client,
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
    stream = SimpleNamespace(identifier="s1", metadata=None)
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["xesam:url"].value == "snapcast://h/s1"
    assert md["xesam:url"].signature == "s"


def test_stream_metadata_track_url_wins_over_snapcast_url():
    stream = SimpleNamespace(
        identifier="s1", metadata={"url": "https://t.example/song"}
    )
    md = stream_metadata("h", stream)
    assert md is not None
    assert md["xesam:url"].value == "https://t.example/song"


# --- stream_capabilities -----------------------------------------------
def test_stream_capabilities_no_stream_all_false():
    caps = stream_capabilities(None)
    assert caps == {
        "CanPlay": False,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
        "CanControl": False,
    }


def test_stream_capabilities_no_properties_all_false():
    stream = SimpleNamespace(properties=None)
    assert stream_capabilities(stream) == {
        "CanPlay": False,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
        "CanControl": False,
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
    assert caps["CanControl"] is True


def test_stream_capabilities_metadata_only_canControl_false():
    # SpotifyHD case: source exposes metadata but no control_script,
    # so snapserver reports canControl=false even while playing.
    stream = SimpleNamespace(properties={
        "canPlay": False, "canPause": False,
        "canGoNext": False, "canGoPrevious": False, "canSeek": False,
        "canControl": False,
    })
    assert stream_capabilities(stream)["CanControl"] is False


def test_stream_capabilities_partial_defaults_to_false():
    stream = SimpleNamespace(properties={"canPlay": True})
    caps = stream_capabilities(stream)
    assert caps == {
        "CanPlay": True,
        "CanPause": False,
        "CanGoNext": False,
        "CanGoPrevious": False,
        "CanSeek": False,
        "CanControl": False,
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
