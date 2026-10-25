"""自我測試：在實際的 Windows 電腦上驗證打包好的 exe 每個元件都能運作。

使用模擬 115（不需帳號、不會連線），資料放在暫存資料夾，不影響正式設定。
執行：NAS115Sync.exe --self-test <報告資料夾>   （或 NAS115Sync-CLI.exe --self-test …）
結果寫入 <報告資料夾>\\selftest_report.json；全部通過時結束代碼為 0。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback

from . import __version__


def run(out_dir: str, gui: bool = True, tab_seconds: float = 0.0) -> int:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    home = tempfile.mkdtemp(prefix="nas115sync_selftest_")
    os.environ["NAS115SYNC_HOME"] = os.path.join(home, "appdata")
    report = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "executable": sys.executable, "python": sys.version, "platform": platform.platform(),
              "steps": [], "ok": False}
    report_path = os.path.join(out_dir, "selftest_report.json")

    def write() -> None:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def step(name: str, fn) -> object:
        t0 = time.time()
        try:
            detail = fn()
            report["steps"].append({"name": name, "ok": True, "seconds": round(time.time() - t0, 2),
                                    "detail": detail})
            return detail
        except Exception as e:  # noqa: BLE001 - 記錄每一步的錯誤
            report["steps"].append({"name": name, "ok": False, "seconds": round(time.time() - t0, 2),
                                    "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()})
            return None
        finally:
            write()

    ctx: dict = {}
    try:
        step("元件載入", _imports)
        step("QR 碼", _qr)
        step("登入資訊加密保存（DPAPI）", lambda: _tokens(home))
        step("MD5/SHA-1 與超長路徑", lambda: _hashing(home))
        step("115 通訊協定（秒傳簽章、加密、下載網址編碼）", _codec)
        step("路徑安全（拒絕寫到 NAS 資料夾以外）", lambda: _path_safety(home))
        step("比對與雙向同步（模擬 115）", lambda: _sync(home, ctx))
        step("遊戲存檔核可與同步", lambda: _savedata(ctx))
        step("即時監看（NAS 變動自動上傳）", lambda: _realtime(ctx))
        step("手動上傳／下載與資源回收筒", lambda: _manual(home, ctx))
        step("程序偵測與開機啟動指令", _system)
        if gui:
            step("圖形介面（所有分頁與按鈕）", lambda: _gui(ctx, out_dir, tab_seconds))
    finally:
        app = ctx.get("app")
        if app is not None:
            try:
                app.db.close()
            except Exception:  # noqa: BLE001
                pass
        report["ok"] = all(s["ok"] for s in report["steps"])
        write()
        shutil.rmtree(home, ignore_errors=True)
    return 0 if report["ok"] else 1


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _imports() -> dict:
    import sqlite3
    import tkinter

    import cryptography
    import oss2
    import qrcode
    import requests
    import watchdog
    from watchdog.observers import Observer

    def ver(dist: str) -> str:
        try:
            from importlib.metadata import version

            return version(dist)
        except Exception:  # noqa: BLE001 - 打包後可能沒有套件中繼資料
            return "ok"

    _ = (qrcode, watchdog)
    return {"requests": requests.__version__, "oss2": oss2.__version__, "cryptography": cryptography.__version__,
            "watchdog": ver("watchdog"),
            "qrcode": ver("qrcode"), "sqlite": sqlite3.sqlite_version, "tk": tkinter.TkVersion,
            "observer": Observer.__name__}


def _qr() -> dict:
    import qrcode

    q = qrcode.QRCode(border=1)
    q.add_data("https://115.com/scan/selftest")
    q.make(fit=True)
    return {"modules": len(q.get_matrix())}


def _tokens(home: str) -> dict:
    from .cloud.tokens import TokenStore

    store = TokenStore(os.path.join(home, "tokens.dat"))
    store.save({"refresh_token": "SELFTEST-SECRET"})
    raw = open(os.path.join(home, "tokens.dat"), "rb").read()
    if sys.platform == "win32":
        _check(raw.startswith(b"DPAPI:") and b"SELFTEST-SECRET" not in raw, "token 未以 DPAPI 加密")
    _check(store.load().get("refresh_token") == "SELFTEST-SECRET", "token 讀回不一致")
    return {"encrypted": raw.startswith(b"DPAPI:")}


def _hashing(home: str) -> dict:
    import hashlib

    from .hashing import hash_file
    from .paths import long_path

    deep = os.path.join(home, *(["很長的資料夾名稱_" + "x" * 40] * 6))  # 超過 260 字元
    os.makedirs(long_path(deep), exist_ok=True)
    path = os.path.join(deep, "測試檔案.bin")
    data = os.urandom(300_000)
    with open(long_path(path), "wb") as f:
        f.write(data)
    h = hash_file(path)
    _check(h.sha1 == hashlib.sha1(data, usedforsecurity=False).hexdigest().upper(), "SHA-1 錯誤")
    _check(h.md5 == hashlib.md5(data, usedforsecurity=False).hexdigest(), "MD5 錯誤")
    return {"path_length": len(path), "sha1": h.sha1}


def _codec() -> dict:
    """打包後的 exe 也必須帶齊加密函式庫，且與參考實作的結果完全相同。"""
    import hashlib

    from .cloud import web115_codec as c

    data = os.urandom(5000)
    _check(c.aes_decrypt(c.aes_encrypt(data)) == data, "AES 加解密不一致")
    _check(c.ec_token(1700000000) == "HQMOgKF43O7OzaN33hKNAHMAAAAA8VNljtndz1WuYe1G6hIaHPyBAAEAAACWHoRq",
           "k_ec 與參考實作不同")
    fields = {"appid": 0, "target": "U_1_0", "topupload": "true", "appversion": "36.2.28", "filename": "a.txt",
              "fileid": "A" * 40, "filesize": 1048576, "path": "", "userid": 123456}
    params, body = c.sign_upload(fields, "ABCDEF0123", 1792946228)
    _check(params["k_ec"] == "HQMOgKF43O7OzaN33hKNAHMAAAA0MN5qjtndz1WuYe1G6hIaHPyBAAEAAAAr3yDm", "上傳簽章參數錯誤")
    _check(hashlib.sha256(body).hexdigest() == "40f93fa025f06d2c4bc2cd4d50d9c0ec7597e3407d0cc80e0d2147dc04a589b1",
           "上傳簽章內容與參考實作不同")
    enc = c.m115_encode_json({"pickcode": "abc123"})
    _check(len(enc) % 172 == 0, "下載網址編碼長度錯誤")
    return {"aes": "ok", "upload_signature": "ok", "m115": len(enc)}


def _path_safety(home: str) -> dict:
    from .paths import is_inside, unsafe_name

    root = os.path.join(home, "NAS")
    for bad in ("..", "..\\..\\x.txt", "C:\\Windows\\x.dll", "a:b.txt", "CON", "結尾."):
        _check(unsafe_name(bad) is not None, f"沒有擋下不安全的名稱：{bad!r}")
    _check(unsafe_name("相片 (1).jpg") is None, "誤擋正常名稱")
    _check(not is_inside(root, os.path.join(root, "..", "外面.txt")), "沒有擋下 .. 路徑")
    _check(is_inside(root, os.path.join(root, "子", "x.txt")), "誤擋正常路徑")
    return {"checked": True}


def _sync(home: str, ctx: dict) -> dict:
    from .app import build_app, setup_logging
    from .config import TWO_WAY

    setup_logging()
    app = ctx["app"] = build_app(demo=True)
    s = app.config.settings
    s.stable_seconds, s.savedata_quiet_seconds, s.api_min_interval = 0.5, 1, 0
    nas = ctx["nas"] = os.path.join(home, "NAS")
    files = {"相片/2024/旅行.jpg": os.urandom(200_000), "文件/報告.docx": b"report",
             "文件/報告_複本.docx": b"report", "大檔案.bin": os.urandom(3_000_000)}
    for rel, data in files.items():
        p = os.path.join(nas, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    app.cloud.put_file("/NAS備份/只在115.txt", b"from 115")
    m = ctx["mapping"] = app.config.add_mapping(nas, "/NAS備份", direction=TWO_WAY)
    app.config.confirm_mapping(m.id, app.cloud.ensure_path("/NAS備份"))
    plans = app.engine.analyze()
    stats = app.engine.execute(plans)
    _check(stats["uploaded"] >= 2, f"上傳數量不對：{stats}")
    _check(stats["downloaded"] == 1 and os.path.exists(os.path.join(nas, "只在115.txt")), f"下載失敗：{stats}")
    kinds = sorted({r.kind for r in app.db.list_reviews()})
    _check("duplicate_hash" in kinds, f"相同 SHA-1 未列入待核對：{kinds}")
    return {"stats": stats, "reviews": kinds}


def _savedata(ctx: dict) -> dict:
    app, nas, m = ctx["app"], ctx["nas"], ctx["mapping"]
    p = os.path.join(nas, "遊戲", "Saves", "slot1.sav")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(b"save v1")
    app.engine.analyze()
    rv = next(r for r in app.db.list_reviews() if r.kind == "savedata_group")
    app.engine.apply_review(rv.id, "approve")
    with open(p, "wb") as f:
        f.write(b"save v2")
    app.engine.sync_savedata_group(m, rv.rel_path)
    files = app.cloud.files_in("/NAS備份/遊戲/Saves")
    from .hashing import hash_bytes

    _check(list(files) == ["slot1.sav"] and files["slot1.sav"].sha1 == hash_bytes(b"save v2").sha1,
           "存檔沒有更新或舊版沒有刪除")
    return {"cloud_files": list(files)}


def _realtime(ctx: dict) -> dict:
    from .watcher import RealtimeSync

    app, nas = ctx["app"], ctx["nas"]
    rt = RealtimeSync(app.engine)
    rt.start(initial_rescan=False)
    try:
        p = os.path.join(nas, "即時", "新檔案.txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("realtime")
        end = time.time() + 40
        while time.time() < end:
            if "新檔案.txt" in app.cloud.files_in("/NAS備份/即時"):
                return {"seconds": round(40 - (end - time.time()), 1)}
            time.sleep(0.5)
        raise AssertionError("40 秒內沒有偵測到 NAS 變動並上傳")
    finally:
        rt.stop()


def _manual(home: str, ctx: dict) -> dict:
    from . import manual

    app = ctx["app"]
    src = os.path.join(home, "手動")
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(src, "a.txt"), "w", encoding="utf-8") as f:
        f.write("manual")
    cid = app.cloud.ensure_path("/手動上傳")
    up = manual.upload_paths(app.engine, "default", [src], cid)
    folder = app.cloud.find_child(cid, "手動")
    dest = os.path.join(home, "下載")
    os.makedirs(dest, exist_ok=True)
    down = manual.download_entries(app.engine, "default", [folder], dest)
    _check(os.path.exists(os.path.join(dest, "手動", "a.txt")), "下載的檔案不存在")
    trash = os.path.join(home, "刪除我.txt")
    with open(trash, "w") as f:
        f.write("x")
    manual.send_to_recycle_bin(trash)
    _check(not os.path.exists(trash), "無法移到資源回收筒")
    return {"upload": up, "download": down}


def _system() -> dict:
    from . import autostart
    from .savedata import running_process_names

    names = running_process_names()
    _check(len(names) > 0, "無法取得執行中的程序清單")
    return {"processes": len(names), "autostart_command": autostart.launch_command()}


def _gui(ctx: dict, out_dir: str, tab_seconds: float) -> dict:
    import tkinter as tk

    from .gui.app import MainWindow, apply_window_icon, enable_dpi_awareness, setup_fonts

    enable_dpi_awareness()
    root = tk.Tk()
    setup_fonts(root)
    apply_window_icon(root)
    from .gui import exercise

    win = MainWindow(root, ctx["app"])
    titles = []
    try:
        root.update()
        open(os.path.join(out_dir, "gui_ready"), "w").close()
        for i, tab in enumerate(win.nb.tabs()):
            win.nb.select(tab)
            titles.append(win.nb.tab(tab, "text"))
            root.update()
            if tab_seconds:
                open(os.path.join(out_dir, f"tab_{i:02d}"), "w").close()
                end = time.time() + tab_seconds
                while time.time() < end:
                    root.update()
                    time.sleep(0.05)
        _check(len(titles) >= 9, f"分頁數量不對：{titles}")
        # 依序按下每個分頁的每個按鈕（對話框自動回答），全程不可出現錯誤
        result = exercise.run(win, os.path.join(os.path.dirname(ctx["nas"]), "介面演練"))
    finally:
        win.scheduler.stop()
        if win.rt.running:
            win.rt.stop()
        root.destroy()
    return {"tabs": titles, "exercise": [f"{s['name']}（{s['seconds']} 秒）" for s in result["steps"]]}
