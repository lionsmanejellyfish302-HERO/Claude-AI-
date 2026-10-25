import os
import sys

import pytest

from nas115sync.cloud.tokens import TokenStore
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


@windows_only
def test_autostart_registry_roundtrip(monkeypatch):
    import winreg

    from nas115sync import autostart

    monkeypatch.setattr(autostart, "VALUE_NAME", f"NAS115Sync_pytest_{os.getpid()}")  # 不動到正式的設定
    assert not autostart.is_enabled()
    try:
        autostart.set_enabled(True)
        assert autostart.is_enabled()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as k:
            assert winreg.QueryValueEx(k, autostart.VALUE_NAME)[0] == autostart.launch_command()
    finally:
        autostart.set_enabled(False)
    assert not autostart.is_enabled()
    autostart.set_enabled(False)  # 已經關閉時再關閉不會出錯


@windows_only
def test_single_instance_blocks_second_copy():
    from nas115sync.app import single_instance

    name = f"NAS115Sync_pytest_{os.getpid()}"
    assert single_instance(name) is True
    assert single_instance(name) is False


@windows_only
def test_recycle_bin_file_and_folder(tmp_path):
    from nas115sync.manual import send_to_recycle_bin

    f = tmp_path / "刪除測試.txt"
    f.write_text("x", encoding="utf-8")
    d = tmp_path / "資料夾 有空白"
    (d / "子").mkdir(parents=True)
    (d / "子" / "a.txt").write_text("y", encoding="utf-8")
    send_to_recycle_bin(str(f))
    send_to_recycle_bin(str(d))
    assert not f.exists() and not d.exists()
    with pytest.raises(FileNotFoundError):
        send_to_recycle_bin(str(tmp_path / "不存在.txt"))


@windows_only
def test_hidden_and_system_files_are_skipped(env):
    import ctypes

    env.mapping.skip_hidden = True
    env.config.save()
    env.write("a.txt", "x")
    p = env.write("desktop.ini", "y")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(p), 0x2 | 0x4)  # 隱藏 + 系統
    env.sync()
    assert set(env.cloud_files()) == {"a.txt"}


@windows_only
def test_running_process_names_include_this_process():
    from nas115sync.savedata import running_process_names

    assert os.path.basename(sys.executable).casefold() in running_process_names()


@windows_only
def test_find_115_browser_uses_configured_path(tmp_path):
    from nas115sync.app import find_115_browser

    exe = tmp_path / "115chrome.exe"
    exe.write_bytes(b"MZ")
    assert find_115_browser(str(exe)) == str(exe)
    found = find_115_browser(str(tmp_path / "沒有.exe"))  # 設定的路徑不存在：改找常見安裝位置
    assert found is None or os.path.isfile(found)
