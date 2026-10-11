"""Tests for the MediaPlayer2Player state machine."""

from dbus_fast import Variant

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


# --- Position ----------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _playing_player(monkeypatch, position_us: int):
    """A playing player that was just told ``position_us``, with its clock
    and the positions it sent with Seeked."""
    clock = FakeClock()
    player = MediaPlayer2Player(clock=clock)
    monkeypatch.setattr(player, "emit_properties_changed", lambda _changed: None)
    seeked: list[int] = []
    monkeypatch.setattr(player, "Seeked", seeked.append)
    player.update_position(position_us)
    player.update_playback_status("Playing")
    return player, clock, seeked


def test_position_is_zero_when_unknown():
    assert MediaPlayer2Player().Position == 0


def test_position_advances_while_playing(monkeypatch):
    player, clock, _ = _playing_player(monkeypatch, 10_000_000)
    clock.now += 5
    assert player.Position == 15_000_000


def test_position_stands_still_while_paused(monkeypatch):
    player, clock, _ = _playing_player(monkeypatch, 10_000_000)
    clock.now += 5
    player.update_playback_status("Paused")
    clock.now += 60
    assert player.Position == 15_000_000


def test_position_resumes_from_where_it_paused(monkeypatch):
    player, clock, _ = _playing_player(monkeypatch, 10_000_000)
    clock.now += 5
    player.update_playback_status("Paused")
    clock.now += 60
    player.update_playback_status("Playing")
    clock.now += 2
    assert player.Position == 17_000_000


def test_position_stops_at_the_track_length(monkeypatch):
    player, clock, _ = _playing_player(monkeypatch, 10_000_000)
    player.update_metadata({"mpris:length": Variant("x", 12_000_000)})
    clock.now += 5
    assert player.Position == 12_000_000


def test_first_known_position_emits_seeked(monkeypatch):
    _, _, seeked = _playing_player(monkeypatch, 10_000_000)
    assert seeked == [10_000_000]


def test_update_position_on_track_does_not_emit_seeked(monkeypatch):
    player, clock, seeked = _playing_player(monkeypatch, 10_000_000)
    seeked.clear()
    clock.now += 15
    player.update_position(25_200_000)
    assert seeked == []
    assert player.Position == 25_200_000


def test_update_position_off_track_emits_seeked(monkeypatch):
    player, clock, seeked = _playing_player(monkeypatch, 10_000_000)
    seeked.clear()
    clock.now += 15
    player.update_position(90_000_000)
    assert seeked == [90_000_000]


def test_update_position_unknown_resets_without_seeked(monkeypatch):
    player, _, seeked = _playing_player(monkeypatch, 10_000_000)
    seeked.clear()
    player.update_position(None)
    assert seeked == []
    assert player.Position == 0
