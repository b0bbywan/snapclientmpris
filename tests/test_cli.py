"""Tests for snapclientmpris.cli: config parsing, discovery, main()."""

import logging
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbus_fast import BusType

from snapclientmpris import cli


# --- read_config -------------------------------------------------------
def _patch_paths(monkeypatch, paths):
    monkeypatch.setattr(cli, "CONFIG_PATHS", [str(p) for p in paths])


def test_read_config_no_files(monkeypatch, tmp_path):
    _patch_paths(monkeypatch, [tmp_path / "missing.conf"])
    assert cli.read_config() == {}


def test_read_config_simple_key_value(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("server = host.example\n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {"server": "host.example"}


def test_read_config_multiple_keys(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("server = h\ncontrol-port = 1705\ndbus-bus = system\n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {
        "server": "h",
        "control-port": "1705",
        "dbus-bus": "system",
    }


def test_read_config_strips_inline_comments(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("control-port = 1705  # default snapserver port\n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {"control-port": "1705"}


def test_read_config_skips_full_line_comments(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("# comment line\nserver = h\n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {"server": "h"}


def test_read_config_skips_blank_and_malformed(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("\n\ngarbage line without equals\nserver = h\n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {"server": "h"}


def test_read_config_trims_whitespace(monkeypatch, tmp_path):
    p = tmp_path / "snapclientmpris.conf"
    p.write_text("   server   =   spaced.host   \n")
    _patch_paths(monkeypatch, [p])
    assert cli.read_config() == {"server": "spaced.host"}


def test_read_config_first_path_wins(monkeypatch, tmp_path):
    user = tmp_path / "user.conf"
    user.write_text("server = userspace\n")
    etc = tmp_path / "etc.conf"
    etc.write_text("server = system\n")
    _patch_paths(monkeypatch, [user, etc])
    assert cli.read_config() == {"server": "userspace"}


def test_read_config_falls_back_to_next_path(monkeypatch, tmp_path):
    missing = tmp_path / "missing.conf"
    fallback = tmp_path / "etc.conf"
    fallback.write_text("server = system\n")
    _patch_paths(monkeypatch, [missing, fallback])
    assert cli.read_config() == {"server": "system"}


def test_read_config_oserror_logs_and_continues(monkeypatch, tmp_path, caplog):
    # Opening a directory raises IsADirectoryError (an OSError subclass that
    # isn't FileNotFoundError), exercising the warn-and-continue branch.
    bad = tmp_path / "subdir"
    bad.mkdir()
    good = tmp_path / "good.conf"
    good.write_text("server = h\n")
    _patch_paths(monkeypatch, [bad, good])
    with caplog.at_level(logging.WARNING, logger="snapclientmpris"):
        assert cli.read_config() == {"server": "h"}
    assert any("failed to read" in r.message for r in caplog.records)


# --- discover_snapserver -----------------------------------------------
CTRL_TYPE = cli.CTRL_SERVICE_TYPE
AUDIO_TYPE = cli.AUDIO_SERVICE_TYPE
HTTP_TYPE = cli.HTTP_SERVICE_TYPE


def _make_info(port=1705, addresses=("192.168.1.10",)):
    info = MagicMock()
    info.port = port
    info.parsed_addresses.return_value = list(addresses)
    return info


def _patch_zeroconf(monkeypatch, info_by_type=None):
    """info_by_type maps service type -> info
    anything missing -> None."""
    info_by_type = info_by_type or {}
    instance = MagicMock()
    instance.get_service_info.side_effect = (
        lambda type_, name, timeout: info_by_type.get(type_)
    )
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(cli, "Zeroconf", cls)
    return instance


def test_discover_no_service(monkeypatch):
    _patch_zeroconf(monkeypatch)
    assert cli.discover_snapserver() is None


def test_discover_uses_ctrl_service_when_available(monkeypatch):
    # snapcast >= 0.33: _snapcast-ctrl._tcp exposes the JSON-RPC port.
    _patch_zeroconf(monkeypatch, {CTRL_TYPE: _make_info(port=1705)})
    assert cli.discover_snapserver() == ("192.168.1.10", 1705)


def test_discover_falls_back_to_audio_service(monkeypatch):
    # snapcast < 0.33 (e.g. 0.31 in Debian trixie) only advertises
    # _snapcast._tcp : we get the host but no control port, caller supplies one.
    _patch_zeroconf(monkeypatch, {AUDIO_TYPE: _make_info(port=1704)})
    assert cli.discover_snapserver() == ("192.168.1.10", None)


def test_discover_prefers_ctrl_over_audio(monkeypatch):
    _patch_zeroconf(monkeypatch, {
        CTRL_TYPE: _make_info(port=1705, addresses=["10.0.0.1"]),
        AUDIO_TYPE: _make_info(port=1704, addresses=["192.168.1.10"]),
    })
    assert cli.discover_snapserver() == ("10.0.0.1", 1705)


def test_discover_falls_through_ctrl_without_port(monkeypatch):
    # Malformed ctrl advertisement -> fall back to audio.
    _patch_zeroconf(monkeypatch, {
        CTRL_TYPE: _make_info(port=None),
        AUDIO_TYPE: _make_info(port=1704),
    })
    assert cli.discover_snapserver() == ("192.168.1.10", None)


def test_discover_falls_through_ctrl_without_real_address(monkeypatch):
    _patch_zeroconf(monkeypatch, {
        CTRL_TYPE: _make_info(port=1705, addresses=["0.0.0.0"]),
        AUDIO_TYPE: _make_info(port=1704, addresses=["192.168.1.10"]),
    })
    assert cli.discover_snapserver() == ("192.168.1.10", None)


def test_discover_skips_unspecified_address(monkeypatch):
    # ctrl present but only 0.0.0.0, no audio fallback -> nothing usable.
    _patch_zeroconf(monkeypatch, {
        CTRL_TYPE: _make_info(port=1705, addresses=["0.0.0.0"]),
    })
    assert cli.discover_snapserver() is None


def test_discover_returns_first_real_address(monkeypatch):
    _patch_zeroconf(monkeypatch, {
        CTRL_TYPE: _make_info(
            port=1705, addresses=["0.0.0.0", "192.168.1.10", "10.0.0.5"]
        ),
    })
    assert cli.discover_snapserver() == ("192.168.1.10", 1705)


def test_discover_closes_zeroconf_on_no_service(monkeypatch):
    instance = _patch_zeroconf(monkeypatch)
    cli.discover_snapserver()
    instance.close.assert_called_once()


def test_discover_closes_zeroconf_on_exception(monkeypatch):
    instance = MagicMock()
    instance.get_service_info.side_effect = RuntimeError("boom")
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(cli, "Zeroconf", cls)
    with pytest.raises(RuntimeError):
        cli.discover_snapserver()
    instance.close.assert_called_once()


# --- discover_snapweb --------------------------------------------------
def test_discover_snapweb_found(monkeypatch):
    _patch_zeroconf(monkeypatch, {HTTP_TYPE: _make_info(port=1780)})
    assert cli.discover_snapweb() == ("192.168.1.10", 1780)


def test_discover_snapweb_none(monkeypatch):
    _patch_zeroconf(monkeypatch)
    assert cli.discover_snapweb() is None


def test_discover_snapweb_skips_unspecified_address(monkeypatch):
    _patch_zeroconf(monkeypatch, {HTTP_TYPE: _make_info(port=1780, addresses=["0.0.0.0"])})
    assert cli.discover_snapweb() is None


def test_discover_snapweb_closes_zeroconf(monkeypatch):
    instance = _patch_zeroconf(monkeypatch)
    cli.discover_snapweb()
    instance.close.assert_called_once()


# --- run_discovery -----------------------------------------------------
def test_run_discovery_prints_both(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_snapserver", lambda: ("192.168.1.10", 1705))
    monkeypatch.setattr(cli, "discover_snapweb", lambda: ("192.168.1.10", 1780))
    cli.run_discovery()
    out = capsys.readouterr().out
    assert "snapserver:  tcp://192.168.1.10:1705" in out
    assert "snapweb:     http://192.168.1.10:1780" in out


def test_run_discovery_audio_only_omits_port(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_snapserver", lambda: ("192.168.1.10", None))
    monkeypatch.setattr(cli, "discover_snapweb", lambda: None)
    cli.run_discovery()
    out = capsys.readouterr().out
    assert "snapserver:  tcp://192.168.1.10\n" in out
    assert "snapweb" not in out


def test_run_discovery_nothing_found(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_snapserver", lambda: None)
    monkeypatch.setattr(cli, "discover_snapweb", lambda: None)
    cli.run_discovery()
    assert "No Snapcast services found" in capsys.readouterr().out


# --- main --------------------------------------------------------------
def _setup_main(monkeypatch, cfg, discovered=None):
    monkeypatch.setattr(cli, "read_config", lambda: cfg)
    monkeypatch.setattr(cli, "discover_snapserver", lambda: discovered)
    monkeypatch.setattr(sys, "argv", ["snapclientmpris"])
    fake_run = AsyncMock()
    monkeypatch.setattr(cli, "run", fake_run)
    return fake_run


# main() now passes run() a lazy resolver + bus_type instead of a resolved
# host/port, so endpoint assertions go through calling resolve() (which uses
# the patched discover_snapserver).
def _resolved(fake_run):
    return fake_run.await_args.args[0]()


def _bus(fake_run):
    return fake_run.await_args.args[1]


def test_main_uses_config_host(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "from-config"})
    cli.main()
    assert _resolved(fake_run)[0] == "from-config"


def test_main_falls_back_to_discovery_when_no_config_host(monkeypatch):
    fake_run = _setup_main(monkeypatch, {}, discovered=("discovered.host", None))
    cli.main()
    assert _resolved(fake_run)[0] == "discovered.host"


def test_main_no_host_resolves_to_none(monkeypatch):
    # No config host and Zeroconf empty is transient now: main() doesn't
    # exit, resolve() returns None and the connect loop retries.
    fake_run = _setup_main(monkeypatch, {}, discovered=None)
    cli.main()
    assert _resolved(fake_run) is None


def test_main_uses_discovered_control_port(monkeypatch):
    # snapserver >= 0.33: the discovered port wins over config.
    fake_run = _setup_main(
        monkeypatch, {"control-port": "9999"}, discovered=("h", 1705)
    )
    cli.main()
    assert _resolved(fake_run)[1] == 1705


def test_main_audio_fallback_uses_configured_control_port(monkeypatch):
    # snapserver < 0.33: discovery returns no port; config / default applies.
    fake_run = _setup_main(
        monkeypatch, {"control-port": "9999"}, discovered=("h", None)
    )
    cli.main()
    assert _resolved(fake_run)[1] == 9999


def test_main_uses_default_control_port(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h"})
    cli.main()
    assert _resolved(fake_run)[1] == cli.SNAPSERVER_CONTROL_PORT


def test_main_uses_configured_control_port(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h", "control-port": "9999"})
    cli.main()
    assert _resolved(fake_run)[1] == 9999


def test_main_invalid_control_port_exits(monkeypatch, caplog):
    # A typo in control-port must abort rather than silently fall back to
    # the default, same fail-loudly contract as dbus-bus, so the daemon
    # never quietly connects to the wrong port.
    _setup_main(monkeypatch, {"server": "h", "control-port": "not-a-number"})
    with (
        caplog.at_level(logging.CRITICAL, logger="snapclientmpris"),
        pytest.raises(SystemExit) as exc,
    ):
        cli.main()
    assert exc.value.code == 1
    assert any("invalid control-port" in r.message for r in caplog.records)


def test_main_session_bus_is_default(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h"})
    cli.main()
    assert _bus(fake_run) == BusType.SESSION


def test_main_system_bus_from_config(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h", "dbus-bus": "system"})
    cli.main()
    assert _bus(fake_run) == BusType.SYSTEM


def test_main_discover_flag_prints_and_skips_run(monkeypatch, capsys):
    fake_run = _setup_main(monkeypatch, {})
    monkeypatch.setattr(cli, "discover_snapserver", lambda: ("192.168.1.10", 1705))
    monkeypatch.setattr(cli, "discover_snapweb", lambda: ("192.168.1.10", 1780))
    monkeypatch.setattr(sys, "argv", ["snapclientmpris", "--discover"])
    cli.main()
    out = capsys.readouterr().out
    assert "snapserver:  tcp://192.168.1.10:1705" in out
    assert "snapweb:     http://192.168.1.10:1780" in out
    fake_run.assert_not_awaited()


def test_main_invalid_bus_choice_exits(monkeypatch, caplog):
    # A typo in dbus-bus must abort rather than silently picking a default,
    # otherwise misconfiguration lands the daemon on the wrong bus.
    _setup_main(monkeypatch, {"server": "h", "dbus-bus": "bogus"})
    with (
        caplog.at_level(logging.CRITICAL, logger="snapclientmpris"),
        pytest.raises(SystemExit) as exc,
    ):
        cli.main()
    assert exc.value.code == 1
    assert any("invalid dbus-bus" in r.message for r in caplog.records)
