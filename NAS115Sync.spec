# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包設定：產生兩個單一檔案程式
#   dist\NAS115Sync.exe      圖形介面（無主控台視窗）
#   dist\NAS115Sync-CLI.exe  指令列（排程、批次用）
# 用法：pyinstaller --noconfirm NAS115Sync.spec
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("watchdog") + collect_submodules("oss2")

a = Analysis(
    ["NAS115Sync.pyw"],
    pathex=["."],
    datas=[("assets/app.ico", "assets")],
    hiddenimports=hidden,
    excludes=["pytest", "PIL", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)

common = dict(icon="assets/app.ico", version="version_info.txt", upx=False, strip=False,
              bootloader_ignore_signals=False, runtime_tmpdir=None)

gui = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="NAS115Sync", console=False, **common)
cli = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="NAS115Sync-CLI", console=True, **common)
