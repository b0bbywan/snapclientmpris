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
def _patch_zeroconf(monkeypatch, info):
    instance = MagicMock()
    instance.get_service_info.return_value = info
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(cli, "Zeroconf", cls)
    return instance


def test_discover_no_service(monkeypatch):
    _patch_zeroconf(monkeypatch, None)
    assert cli.discover_snapserver() is None


def test_discover_no_port(monkeypatch):
    info = MagicMock()
    info.port = None
    _patch_zeroconf(monkeypatch, info)
    assert cli.discover_snapserver() is None


def test_discover_skips_unspecified_address(monkeypatch):
    info = MagicMock()
    info.port = 1705
    info.parsed_addresses.return_value = ["0.0.0.0"]
    _patch_zeroconf(monkeypatch, info)
    assert cli.discover_snapserver() is None


def test_discover_returns_first_real_address(monkeypatch):
    info = MagicMock()
    info.port = 1705
    info.parsed_addresses.return_value = ["0.0.0.0", "192.168.1.10", "10.0.0.5"]
    _patch_zeroconf(monkeypatch, info)
    assert cli.discover_snapserver() == "192.168.1.10"


def test_discover_closes_zeroconf_on_no_service(monkeypatch):
    instance = _patch_zeroconf(monkeypatch, None)
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


# --- main --------------------------------------------------------------
def _setup_main(monkeypatch, cfg, discovered=None):
    monkeypatch.setattr(cli, "read_config", lambda: cfg)
    monkeypatch.setattr(cli, "discover_snapserver", lambda: discovered)
    monkeypatch.setattr(sys, "argv", ["snapclientmpris"])
    fake_run = AsyncMock()
    monkeypatch.setattr(cli, "run", fake_run)
    return fake_run


def test_main_uses_config_host(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "from-config"})
    cli.main()
    assert fake_run.await_args.args[0] == "from-config"


def test_main_falls_back_to_discovery_when_no_config_host(monkeypatch):
    fake_run = _setup_main(monkeypatch, {}, discovered="discovered.host")
    cli.main()
    assert fake_run.await_args.args[0] == "discovered.host"


def test_main_exits_when_no_host_anywhere(monkeypatch):
    _setup_main(monkeypatch, {}, discovered=None)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


def test_main_uses_default_control_port(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h"})
    cli.main()
    assert fake_run.await_args.args[1] == cli.SNAPSERVER_CONTROL_PORT


def test_main_uses_configured_control_port(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h", "control-port": "9999"})
    cli.main()
    assert fake_run.await_args.args[1] == 9999


def test_main_invalid_control_port_falls_back_to_default(monkeypatch, caplog):
    fake_run = _setup_main(
        monkeypatch, {"server": "h", "control-port": "not-a-number"}
    )
    with caplog.at_level(logging.WARNING, logger="snapclientmpris"):
        cli.main()
    assert fake_run.await_args.args[1] == cli.SNAPSERVER_CONTROL_PORT
    assert any("invalid control-port" in r.message for r in caplog.records)


def test_main_session_bus_is_default(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h"})
    cli.main()
    assert fake_run.await_args.args[2] == BusType.SESSION


def test_main_system_bus_from_config(monkeypatch):
    fake_run = _setup_main(monkeypatch, {"server": "h", "dbus-bus": "system"})
    cli.main()
    assert fake_run.await_args.args[2] == BusType.SYSTEM


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
