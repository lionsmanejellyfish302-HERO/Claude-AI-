import sys

import pytest

from nas115sync.cloud.open115 import TokenStore
from nas115sync.paths import long_path

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows 專用")


def test_token_store_roundtrip(tmp_path):
    store = TokenStore(tmp_path / "tokens.dat")
    store.save({"access_token": "A", "refresh_token": "R"})
    assert store.load() == {"access_token": "A", "refresh_token": "R"}


@windows_only
def test_token_store_is_encrypted_with_dpapi(tmp_path):
    store = TokenStore(tmp_path / "tokens.dat")
    store.save({"refresh_token": "SECRET-123"})
    raw = (tmp_path / "tokens.dat").read_bytes()
    assert raw.startswith(b"DPAPI:") and b"SECRET-123" not in raw
    assert store.load()["refresh_token"] == "SECRET-123"


@windows_only
def test_long_path_prefix():
    unc = "\\\\NAS\\share\\" + "a" * 250
    assert long_path(unc).startswith("\\\\?\\UNC\\NAS\\share\\")
    short = "C:\\x\\y.txt"
    assert long_path(short) == short


@windows_only
def test_autostart_command():
    from nas115sync import autostart

    cmd = autostart.launch_command()
    assert "--start-realtime" in cmd and "--minimized" in cmd
