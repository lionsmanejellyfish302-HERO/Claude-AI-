"""Windows 開機自動啟動（寫入目前使用者的 Run 機碼，不需系統管理員權限）。"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "NAS115Sync"


def launch_command() -> str:
    args = "--start-realtime --minimized"
    if getattr(sys, "frozen", False):  # PyInstaller 打包的 exe
        return f'"{sys.executable}" {args}'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NAS115Sync.pyw")
    return f'"{pyw}" "{launcher}" {args}'


def supported() -> bool:
    return sys.platform == "win32"


def is_enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    if not supported():
        raise OSError("只支援 Windows")
    import winreg

    if enabled:
        # 新建立的使用者帳號可能還沒有 Run 機碼：不存在時建立
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:  # 沒有機碼或沒有這個值：本來就沒有開機啟動
        pass
