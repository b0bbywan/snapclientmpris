"""Tests for the MediaPlayer2Player state machine."""

from snapclientmpris.mpris import MediaPlayer2Player


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
    })
    assert player.CanPlay is True
    assert player.CanPause is True
    assert player.CanGoNext is True
    assert player.CanGoPrevious is False
    assert player.CanSeek is False


def test_can_control_is_always_true():
    # MPRIS clients refuse to set Volume when CanControl=False, but
    # snapcast volume is per-client and we want it controllable even
    # when the stream source itself reports canControl=false. Backends
    # that need the truth read the per-operation Can* flags.
    player = MediaPlayer2Player()
    assert player.CanControl is True
    player.update_capabilities({"CanPlay": False, "CanPause": False})
    assert player.CanControl is True


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
