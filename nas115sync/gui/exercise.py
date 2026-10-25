"""圖形介面實機演練：依序按下每個分頁的每個按鈕，對話框以腳本自動回答，確認全程沒有任何錯誤。

供 pytest（tests/test_gui.py）與打包後 exe 的自我測試（--self-test）共用，
讓真正的 Windows 視窗也被完整操作一遍。只能用在示範模式（模擬 115）。
演練期間「開啟檔案總管／115 瀏覽器」與「開機自動啟動」只做紀錄，不會真的執行。
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import autostart
from ..compare import CONFLICT, SAVEDATA_GROUP, available_decisions
from ..config import DIRECTION_LABELS, TWO_WAY
from ..hashing import hash_bytes
from . import app as gui_app
from . import browser as gui_browser
from . import dialogs as gui_dialogs
from . import tabs as gui_tabs

CLOUD = "/GUI測試"
# 內容會再加上每次執行都不同的標記：與 NAS 或 115 上既有檔案的 SHA-1 相同時，程式會依設計交給本人核對
FILES = {
    "相片/旅行.jpg": b"photo" * 2000,
    "相片/旅行_複本.jpg": b"photo" * 2000,
    "文件/報告.txt": b"report",
    "文件/衝突.txt": "NAS 版本".encode(),
    "遊戲/Saves/slot1.sav": b"save v1",
}
CLOUD_FILES = {
    "只在115.txt": b"from 115",
    "文件/衝突.txt": "115 版本".encode(),
    "文件/備份1.txt": b"cloud dup",
    "文件/備份2.txt": b"cloud dup",
}


class GuiError(AssertionError):
    pass


class _ThreadSpy:
    """記錄介面開出的背景執行緒，才能等它們全部做完。"""

    def __init__(self, real, sink: list):
        self._real, self._sink = real, sink

    def __getattr__(self, name):
        return getattr(self._real, name)

    def Thread(self, *args, **kwargs):  # noqa: N802 - 對應 threading.Thread
        t = self._real.Thread(*args, **kwargs)
        self._sink.append(t)
        return t


class _ErrorLog(logging.Handler):
    def __init__(self, sink: list):
        super().__init__(logging.ERROR)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(f"紀錄錯誤：{record.getMessage()}" + (f"\n{record.exc_text or ''}" if record.exc_info else ""))


class Driver:
    def __init__(self, win, workdir: str):
        self.win, self.root, self.workdir = win, win.root, workdir
        self.threads: list[threading.Thread] = []
        self.messages: list[tuple[str, str, str]] = []
        self.strings: dict[str, list[str]] = {}
        self.files: dict[str, list[str]] = {}
        self.scripts: dict[str, list] = {}
        self.opened: list[str] = []
        self.errors: list[str] = []
        self.steps: list[dict] = []
        self._expected: set[str] = set()
        self._restore: list = []

    # ------------------------------------------------------------ 替身
    def _patch(self, obj, name: str, value) -> None:
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def install(self) -> None:
        def msg(kind, ret=None):
            def f(title=None, message=None, **_kw):
                self.messages.append((kind, str(title), str(message)))
                return ret
            return f

        for kind in ("showinfo", "showwarning", "showerror"):
            self._patch(messagebox, kind, msg(kind))
        self._patch(messagebox, "askyesno", msg("askyesno", True))
        self._patch(simpledialog, "askstring", self._askstring)
        for name in ("askdirectory", "asksaveasfilename", "askopenfilename"):
            self._patch(filedialog, name, lambda *_a, _n=name, **_k: self._pop(self.files, _n, ""))
        self._patch(gui_dialogs._Dialog, "modal", lambda dlg: self._modal(dlg))
        for mod in (gui_app, gui_browser, gui_dialogs, gui_tabs):
            if hasattr(mod, "open_local"):
                self._patch(mod, "open_local", lambda p: self.opened.append(f"NAS:{p}"))
            if hasattr(mod, "open_in_115_browser"):
                self._patch(mod, "open_in_115_browser", lambda url, *_a: self.opened.append(f"115:{url}"))
            if hasattr(mod, "threading"):
                self._patch(mod, "threading", _ThreadSpy(threading, self.threads))
        self._patch(autostart, "set_enabled", lambda on: self.opened.append(f"autostart:{on}"))
        self._old_report = self.root.report_callback_exception
        self.root.report_callback_exception = lambda *exc: self.errors.append(
            "介面例外：" + "".join(traceback.format_exception(*exc)))
        self._log = _ErrorLog(self.errors)
        logging.getLogger().addHandler(self._log)

    def uninstall(self) -> None:
        for obj, name, old in reversed(self._restore):
            setattr(obj, name, old)
        self._restore.clear()
        logging.getLogger().removeHandler(self._log)
        try:
            self.root.report_callback_exception = self._old_report
        except Exception:  # noqa: BLE001 - 視窗可能已關閉
            pass

    @staticmethod
    def _pop(table: dict, key: str, default=None):
        items = table.get(key)
        if not items:
            if default is not None:
                return default
            raise GuiError(f"未預期的輸入框：{key}")
        return items.pop(0)

    def _askstring(self, title, prompt=None, **_kw):
        self.messages.append(("askstring", str(title), str(prompt)))
        return self._pop(self.strings, str(title))

    def _modal(self, dlg):
        scripts = self.scripts.get(type(dlg).__name__)
        try:
            if not scripts:
                raise GuiError(f"未預期的對話框：{type(dlg).__name__}")
            dlg.update()
            scripts.pop(0)(dlg)
        finally:
            if dlg.winfo_exists():
                dlg.destroy()
        return dlg.result

    # ------------------------------------------------------------ 操作
    def settle(self, until=None, timeout: float = 90.0, what: str = "") -> None:
        """處理視窗事件，直到條件成立；沒有條件時等到所有背景工作與介面更新都結束。"""
        end = time.monotonic() + timeout
        while True:
            self.root.update()
            if until is not None:
                if until():
                    return
            elif (not self.win.busy and self.win.ui.q.empty()
                  and not any(t.is_alive() for t in self.threads)):
                self.root.update()
                if self.win.ui.q.empty():
                    return
            if time.monotonic() > end:
                raise GuiError(f"等待逾時（{timeout:.0f} 秒）：{what or '背景工作'}")
            time.sleep(0.02)

    def find(self, text: str, within=None, prefix: bool = False):
        found, stack = [], [within or self.root]
        while stack:
            w = stack.pop()
            if isinstance(w, (ttk.Button, ttk.Checkbutton)):
                t = str(w.cget("text"))
                if t == text or (prefix and t.startswith(text)):
                    found.append(w)
            stack.extend(w.winfo_children())
        if len(found) != 1:
            raise GuiError(f"找不到唯一的按鈕「{text}」（找到 {len(found)} 個）")
        return found[0]

    def click(self, text: str, within=None, prefix: bool = False, wait: bool = True) -> None:
        w = self.find(text, within, prefix)
        if w.instate(["disabled"]):
            raise GuiError(f"按鈕「{text}」是停用狀態")
        w.invoke()
        if wait:
            self.settle(what=text)

    def expect(self, title: str) -> None:
        """這一步預期會出現標題為 title 的警告／錯誤訊息（例如故意輸入錯誤格式）。"""
        self._expected.add(title)

    def step(self, name: str, fn) -> None:
        t0 = time.monotonic()
        n_msg, n_err = len(self.messages), len(self.errors)
        self._expected = set()
        problems: list[str] = []
        try:
            fn()
            self.settle(what=name)
        except Exception as e:  # noqa: BLE001 - 每一步獨立記錄
            problems.append(f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        new_msgs = self.messages[n_msg:]
        problems += self.errors[n_err:]
        problems += [f"跳出{kind}「{title}」：{text}" for kind, title, text in new_msgs
                     if kind in ("showerror", "showwarning") and title not in self._expected]
        seen = {title for _k, title, _t in new_msgs}
        problems += [f"預期的訊息「{t}」沒有出現" for t in self._expected - seen]
        self.steps.append({"name": name, "ok": not problems, "seconds": round(time.monotonic() - t0, 2),
                           "messages": [f"{k}:{t}" for k, t, _ in new_msgs], "problems": problems})

    # ------------------------------------------------------------ 輔助
    @property
    def mapping(self):
        return next((m for m in self.win.config.mappings if m.cloud_path == CLOUD), None)

    def cloud_files(self, rel: str = "") -> dict:
        return self.win.cur_cloud.files_in(CLOUD + ("/" + rel if rel else ""))

    def cloud_iid(self, name: str) -> str:
        b = self.win.browser
        for iid, e in b.cloud_items.items():
            if e.name == name:
                return iid
        raise GuiError(f"115 窗格找不到「{name}」：{[e.name for e in b.cloud_items.values()]}")

    def review(self, kind: str, rel_path: str | None = None):
        m = self.mapping
        for rv in self.win.db.list_reviews("pending", kind=kind):
            if rv.mapping_id == m.id and (rel_path is None or rv.rel_path == rel_path):
                return rv
        raise GuiError(f"找不到待核對項目 {kind} {rel_path or ''}")

    def decide(self, rv, decision: str) -> None:
        w = self.win
        w.nb.select(w.tab_reviews)
        w.kind_filter.set("全部")
        w._refresh_reviews()
        w.rev_tree.selection_set(str(rv.id))
        self.settle(what="選取待核對項目")
        label = dict(available_decisions(rv.kind, rv.details))[decision]
        self.click(label, within=w.decision_bar)


# ================================================================ 演練內容
def run(win, workdir: str) -> dict:
    """在已開啟的主視窗上演練所有功能；任何一步失敗都會丟出 GuiError（附上每一步的結果）。"""
    if not win.app.demo:
        raise GuiError("介面演練只能在示範模式（模擬 115）執行")
    d = Driver(win, workdir)
    d.install()
    try:
        _scenario(d)
    finally:
        d.uninstall()
    bad = [s for s in d.steps if not s["ok"]]
    summary = {"steps": d.steps, "opened": len(d.opened), "messages": len(d.messages)}
    if bad:
        raise GuiError("介面演練失敗：\n" + "\n".join(f"■ {s['name']}\n  " + "\n  ".join(s["problems"])
                                                for s in bad))
    return summary


def _scenario(d: Driver) -> None:  # noqa: PLR0915 - 一步一步照順序操作
    w, b = d.win, d.win.browser
    nas = os.path.join(d.workdir, "GUI測試NAS")
    browse_dir = os.path.join(d.workdir, "瀏覽測試")
    cloud = w.cur_cloud
    tag = f"\n{time.time_ns()}-{os.getpid()}".encode()

    def prepare():
        for rel, data in FILES.items():
            p = os.path.join(nas, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data + tag)
        for rel, data in CLOUD_FILES.items():
            cloud.put_file(f"{CLOUD}/{rel}", data + tag)
        os.makedirs(os.path.join(browse_dir, "子資料夾"), exist_ok=True)
        for rel, data in (("上傳我.txt", b"upload me" + tag), ("子資料夾/b.txt", b"sub file" + tag)):
            with open(os.path.join(browse_dir, *rel.split("/")), "wb") as f:
                f.write(data)

    d.step("準備測試資料", prepare)

    # ------------------------------------------------------------ 總覽：帳號
    def accounts():
        d.strings["新增 115 帳號"] = ["家人帳號"]
        d.click("＋", within=w.tab_dashboard)
        labels = [a.label for a in w.config.accounts]
        if "家人帳號" not in labels or w.acct_combo.get() != "家人帳號":
            raise GuiError(f"新增帳號失敗：{labels}")
        d.click("－", within=w.tab_dashboard)
        if "家人帳號" in [a.label for a in w.config.accounts]:
            raise GuiError("刪除帳號失敗")
        d.click("－", within=w.tab_dashboard)  # 預設帳號不能刪除，只會提示
        d.click("前往待核對 →", within=w.tab_dashboard)
        if w.nb.select() != str(w.tab_reviews):
            raise GuiError("沒有切換到待核對頁")

    d.step("總覽：新增／刪除 115 帳號", accounts)

    # ------------------------------------------------------------ 路徑對應
    def add_script(dlg):
        dlg.local.set(nas)
        dlg.cloud.set(CLOUD)
        dlg.direction.set(DIRECTION_LABELS[TWO_WAY])
        d.click("確定", within=dlg, wait=False)

    def confirm_script(dlg):
        d.settle(until=lambda: dlg.info is not None, what="讀取 NAS 與 115 內容")
        d.click("在 115 瀏覽器開啟", within=dlg, wait=False)
        d.click("開啟 NAS 資料夾", within=dlg, wait=False)
        d.click("我已本人核對：NAS 與 115 路徑對應正確", within=dlg, wait=False)
        d.click("確認對應", within=dlg, wait=False)

    def add_mapping():
        w.nb.select(w.tab_mappings)
        d.scripts["AddMappingDialog"] = [add_script]
        d.scripts["ConfirmMappingDialog"] = [confirm_script]
        d.click("新增…", within=w.tab_mappings)
        m = d.mapping
        if not m or not m.confirmed or m.direction != TWO_WAY:
            raise GuiError(f"新增並核對對應失敗：{m}")

    d.step("路徑對應：新增並由本人核對", add_mapping)

    def edit_mapping():
        m = d.mapping

        def script(dlg):
            dlg.exclude.set("*.tmp")
            d.click("確定", within=dlg, wait=False)

        w.map_tree.selection_set(m.id)
        d.scripts["AddMappingDialog"] = [script]
        d.click("編輯選項…", within=w.tab_mappings)
        if d.mapping.exclude_patterns != ["*.tmp"] or not d.mapping.confirmed:
            raise GuiError("編輯選項失敗或錯誤地取消了核對")
        w.map_tree.selection_set(m.id)
        d.click("啟用/停用", within=w.tab_mappings)
        if d.mapping.enabled:
            raise GuiError("停用失敗")
        d.click("啟用/停用", within=w.tab_mappings)
        n = len(d.opened)
        d.click("開啟 NAS 資料夾", within=w.tab_mappings)
        d.click("在 115 瀏覽器開啟", within=w.tab_mappings)
        if len(d.opened) != n + 2:
            raise GuiError(f"開啟 NAS／115 失敗：{d.opened[n:]}")

    d.step("路徑對應：編輯、停用／啟用、開啟位置", edit_mapping)

    def suggest():
        def script(dlg):
            dlg.nas_root.set(nas)
            dlg.cloud_root.set("/GUI建議")
            d.click("產生建議", within=dlg, wait=False)
            d.settle(until=lambda: len(dlg.tree.get_children()) >= 3, what="產生建議")
            d.click("全選", within=dlg, wait=False)
            d.click("加入選取項目", within=dlg, wait=False)

        d.scripts["SuggestDialog"] = [script]
        d.click("自動建議（NAS 所有資料夾）…", within=w.tab_mappings)
        added = [m for m in w.config.mappings if m.cloud_path.startswith("/GUI建議/")]
        if len(added) != 3 or any(m.confirmed for m in added):
            raise GuiError(f"自動建議應加入 3 個待核對對應：{[(m.cloud_path, m.confirmed) for m in added]}")
        w.map_tree.selection_set([m.id for m in added])
        d.click("核對確認…", within=w.tab_mappings)  # 多選時只提示一次核對一個
        d.click("取消核對", within=w.tab_mappings)
        d.click("刪除", within=w.tab_mappings)
        if any(m.cloud_path.startswith("/GUI建議/") for m in w.config.mappings):
            raise GuiError("刪除對應失敗")

    d.step("路徑對應：自動建議、刪除", suggest)

    # ------------------------------------------------------------ 掃描與同步
    def scan_and_sync():
        w.nb.select(w.tab_dashboard)
        d.click("① 掃描比對", within=w.tab_dashboard)
        if not any(t == "比對完成" for _k, t, _m in d.messages):
            raise GuiError("沒有出現「比對完成」")
        csv_path = os.path.join(d.workdir, "比對報告.csv")
        d.files["asksaveasfilename"] = [csv_path]
        d.click("匯出比對報告（CSV）", within=w.tab_dashboard)
        if not os.path.getsize(csv_path):
            raise GuiError("CSV 報告是空的")
        d.click("② 執行同步", within=w.tab_dashboard)
        if not any(t == "同步完成" for _k, t, _m in d.messages):
            raise GuiError("沒有出現「同步完成」")
        if not os.path.exists(os.path.join(nas, "只在115.txt")):
            raise GuiError("只在 115 的檔案沒有下載到 NAS")
        if "報告.txt" not in d.cloud_files("文件"):
            raise GuiError("NAS 檔案沒有上傳到 115")

    d.step("總覽：掃描比對、匯出 CSV、執行同步", scan_and_sync)

    def busy_and_cancel():
        d.click("① 掃描比對", within=w.tab_dashboard, wait=False)
        d.click("① 掃描比對", within=w.tab_dashboard, wait=False)  # 工作中再按：只提示請稍候
        d.click("取消目前工作", within=w.tab_dashboard)
        if not any(t == "請稍候" for _k, t, _m in d.messages):
            raise GuiError("工作進行中再按沒有提示")

    d.step("總覽：工作中重複按、取消工作", busy_and_cancel)

    # ------------------------------------------------------------ 待核對
    def show_reviews():
        w.nb.select(w.tab_reviews)
        d.click("重新整理", within=w.tab_reviews)
        for label in w.kind_filter.cget("values"):
            w.kind_filter.set(label)
            w.kind_filter.event_generate("<<ComboboxSelected>>")
            d.settle(what=f"篩選 {label}")
        w.kind_filter.set("全部")
        w._refresh_reviews()
        items = w.rev_tree.get_children()
        if not items:
            raise GuiError("沒有任何待核對項目（應有衝突、遊戲存檔等）")
        for iid in items:
            w.rev_tree.selection_set(iid)
            d.settle(what="顯示待核對項目")
            text = w.rev_detail.get("1.0", "end")
            if f"#{iid}" not in text or not w.decision_bar.winfo_children():
                raise GuiError(f"待核對 #{iid} 沒有顯示詳細內容或決定按鈕")
            d.click("開啟 NAS 位置", within=w.tab_reviews)
            d.click("在 115 瀏覽器開啟", within=w.tab_reviews)
        w.rev_tree.selection_set(items[0])
        d.click("全選此類型", within=w.tab_reviews)

    d.step("待核對：每一項都顯示 MD5/SHA-1 與可選的決定", show_reviews)

    def resolve_conflict():
        rv = d.review(CONFLICT, "文件/衝突.txt")
        d.decide(rv, "upload_keep_both")
        files = d.cloud_files("文件")
        if files.get("衝突.txt") is None or files["衝突.txt"].sha1 != hash_bytes(FILES["文件/衝突.txt"] + tag).sha1:
            raise GuiError(f"衝突處理後 115 不是 NAS 版本：{list(files)}")
        if not any(n.startswith("衝突 (115舊版") for n in files):
            raise GuiError(f"115 舊版沒有改名保留：{list(files)}")

    d.step("待核對：衝突 → 上傳並保留 115 舊版", resolve_conflict)

    def approve_savedata():
        rv = d.review(SAVEDATA_GROUP)
        w.proc_var.set("game.exe")
        d.decide(rv, "approve")
        g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
        if g.status != "approved" or g.process_names != ["game.exe"]:
            raise GuiError(f"存檔核可失敗：{g}")
        if "slot1.sav" not in d.cloud_files("遊戲/Saves"):
            raise GuiError("核可後存檔沒有上傳")

    d.step("待核對：核可遊戲存檔（填入遊戲程序）", approve_savedata)

    # ------------------------------------------------------------ 遊戲存檔
    def savedata_tab():
        w.nb.select(w.tab_savedata)
        d.settle(what="切換到遊戲存檔")
        iid = next(i for i, (m, _g) in w.group_items.items() if m.id == d.mapping.id)
        w.save_tree.selection_set(iid)
        d.strings["遊戲程序"] = ["game.exe, launcher.exe"]
        d.click("設定遊戲程序…", within=w.tab_savedata)
        g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
        if g.process_names != ["game.exe", "launcher.exe"]:
            raise GuiError(f"設定遊戲程序失敗：{g.process_names}")
        with open(os.path.join(nas, "遊戲", "Saves", "slot1.sav"), "wb") as f:
            f.write(b"save v2" + tag)
        w.save_tree.selection_set(iid)
        d.click("立即同步此存檔", within=w.tab_savedata)
        files = d.cloud_files("遊戲/Saves")
        if list(files) != ["slot1.sav"] or files["slot1.sav"].sha1 != hash_bytes(b"save v2" + tag).sha1:
            raise GuiError(f"存檔沒有更新或 115 舊版沒有刪除：{list(files)}")
        w.save_tree.selection_set(iid)
        d.click("開啟 NAS 位置", within=w.tab_savedata)
        for action, status in (("當一般檔案", "normal"), ("不同步", "exclude"), ("核可自動同步", "approved")):
            w.save_tree.selection_set(iid)
            d.click(action, within=w.tab_savedata)
            g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
            if g.status != status:
                raise GuiError(f"「{action}」後狀態應為 {status}，實際 {g.status}")

    d.step("遊戲存檔：設定程序、立即同步、變更狀態", savedata_tab)

    # ------------------------------------------------------------ 即時同步
    def realtime():
        w.nb.select(w.tab_dashboard)
        d.click("▶ 開始即時同步", within=w.tab_dashboard)
        if not w.rt.running:
            raise GuiError("即時同步沒有啟動")
        p = os.path.join(nas, "即時", "新檔案.txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"realtime" + tag)
        d.settle(until=lambda: "新檔案.txt" in d.cloud_files("即時"), timeout=90, what="即時上傳")
        d.click("■ 停止即時同步", within=w.tab_dashboard)
        d.settle(until=lambda: not w.rt.running and str(w.rt_btn.cget("text")) == "▶ 開始即時同步",
                 what="停止即時同步")

    d.step("即時同步：NAS 新增檔案自動上傳", realtime)

    # ------------------------------------------------------------ 瀏覽：NAS 窗格
    local_pane = b.local_tree.frame.master
    cloud_pane = b.cloud_tree.frame.master

    def browse_local():
        w.nb.select(w.tab_browser)
        d.files["askdirectory"] = [nas]
        d.click("瀏覽…", within=local_pane)
        if b.local_dir != nas or not b.local_tree.exists("相片"):
            raise GuiError(f"NAS 窗格沒有開啟：{b.local_dir}")
        b.local_tree.selection_set("相片")
        b._local_open()
        if os.path.basename(b.local_dir) != "相片":
            raise GuiError("雙擊資料夾沒有進入")
        d.click("↑", within=local_pane)
        d.click("⟳", within=local_pane)
        if b.local_dir != nas:
            raise GuiError("回上一層失敗")
        d.files["askdirectory"] = [browse_dir]
        d.click("瀏覽…", within=local_pane)
        d.strings["新資料夾"] = ["新資料夾A"]
        d.click("新資料夾", within=local_pane)
        b.local_tree.selection_set("新資料夾A")
        d.strings["重新命名"] = ["改名後"]
        d.click("重新命名", within=local_pane)
        if not os.path.isdir(os.path.join(browse_dir, "改名後")):
            raise GuiError("NAS 新增資料夾或改名失敗")
        b.local_tree.selection_set("改名後")
        d.click("刪除（資源回收筒）", within=local_pane)
        if os.path.exists(os.path.join(browse_dir, "改名後")):
            raise GuiError("移到資源回收筒失敗")
        d.click("在檔案總管開啟", within=local_pane)
        if b.local_tree.exists("改名後"):
            raise GuiError("刪除後列表沒有更新")

    d.step("瀏覽：NAS 窗格（開啟、上一層、新增、改名、資源回收筒）", browse_local)

    # ------------------------------------------------------------ 瀏覽：115 窗格與傳輸
    def browse_cloud():
        b.cloud_go_path(CLOUD)
        d.settle(what="開啟 115 資料夾")
        if b.cloud_var.get() != CLOUD or not b.cloud_items:
            raise GuiError(f"115 窗格沒有開啟 {CLOUD}：{b.cloud_var.get()}")
        b.cloud_tree.selection_set(d.cloud_iid("文件"))
        b._cloud_open()
        d.settle(what="進入 115 子資料夾")
        if b.cloud_var.get() != CLOUD + "/文件":
            raise GuiError(f"雙擊 115 資料夾沒有進入：{b.cloud_var.get()}")
        d.click("↑", within=cloud_pane)
        b.cloud_go_path("/")
        d.settle()
        d.strings["新資料夾"] = ["GUI瀏覽"]
        d.click("新資料夾", within=cloud_pane)
        b.cloud_tree.selection_set(d.cloud_iid("GUI瀏覽"))
        b._cloud_open()
        d.settle()
        if b.cloud_var.get() != "/GUI瀏覽":
            raise GuiError(f"115 新資料夾失敗：{b.cloud_var.get()}")

        b.local_tree.selection_set(["上傳我.txt", "子資料夾"])
        d.click("上傳選取 →", within=local_pane)
        names = {e.name for e in b.cloud_items.values()}
        if not {"上傳我.txt", "子資料夾"} <= names:
            raise GuiError(f"上傳後 115 窗格沒有檔案：{names}")
        d.click("⇄ 比較兩邊（MD5/SHA-1）", within=w.tab_browser)
        if b.compare.get("上傳我.txt".casefold()) != "same":
            raise GuiError(f"比較結果不對：{b.compare}")

        b.cloud_tree.selection_set(d.cloud_iid("上傳我.txt"))
        d.strings["重新命名"] = ["已改名.txt"]
        d.click("重新命名", within=cloud_pane)
        b.search_var.set("已改名")
        d.click("🔍 在此資料夾搜尋", within=cloud_pane)
        if not b.cloud_var.get().startswith("搜尋") or not b.cloud_items:
            raise GuiError(f"搜尋失敗：{b.cloud_var.get()}")
        d.click("⟳", within=cloud_pane)
        b.cloud_tree.selection_set(d.cloud_iid("已改名.txt"))
        d.click("← 下載選取", within=cloud_pane)
        if not os.path.exists(os.path.join(browse_dir, "已改名.txt")):
            raise GuiError("下載失敗")
        b.cloud_tree.selection_set(d.cloud_iid("子資料夾"))
        d.strings["移動到"] = ["/GUI瀏覽/移動目的"]
        d.click("移動到…", within=cloud_pane)
        if "子資料夾" in {e.name for e in b.cloud_items.values()}:
            raise GuiError("移動後原位置仍有資料夾")
        b.cloud_tree.selection_set(d.cloud_iid("已改名.txt"))
        d.click("刪除（進 115 回收站）", within=cloud_pane)
        if "已改名.txt" in {e.name for e in b.cloud_items.values()}:
            raise GuiError("115 刪除失敗")
        d.click("在 115 瀏覽器開啟", within=cloud_pane)
        b.conflict.set(gui_browser.CONFLICT_CHOICES[2][1])
        if b._conflict_mode() != gui_browser.CONFLICT_CHOICES[2][0]:
            raise GuiError("同名處理方式選擇失敗")
        b.conflict.set(gui_browser.CONFLICT_CHOICES[0][1])

    d.step("瀏覽：115 窗格（新資料夾、上傳、比較、改名、搜尋、下載、移動、刪除）", browse_cloud)

    def transfers():
        w.nb.select(w.tab_transfers)
        tt = w.transfers_tab
        tt.refresh()
        if not tt.tree.get_children():
            raise GuiError("傳輸頁沒有任何紀錄")
        d.click("⏸ 全部暫停", within=w.tab_transfers)
        if not tt.mgr.paused.is_set():
            raise GuiError("暫停失敗")
        d.click("▶ 全部繼續", within=w.tab_transfers)
        tt.up_var.set("abc")
        d.expect("格式錯誤")
        d.click("套用（0 = 不限）", within=w.tab_transfers)
        tt.up_var.set("500")
        tt.down_var.set("800")
        d.click("套用（0 = 不限）", within=w.tab_transfers)
        s = w.config.settings
        if (s.upload_limit_kb, s.download_limit_kb) != (500, 800):
            raise GuiError("限速沒有套用")
        tt.up_var.set("0")
        tt.down_var.set("0")
        d.click("套用（0 = 不限）", within=w.tab_transfers)
        tt.tree.selection_set(tt.tree.get_children()[0])
        d.click("✖ 取消選取的傳輸", within=w.tab_transfers)
        d.click("清除已完成", within=w.tab_transfers)

    d.step("傳輸：暫停／繼續、限速、取消、清除", transfers)

    # ------------------------------------------------------------ 重複檔案
    def duplicates():
        w.nb.select(w.tab_dupes)
        dt = w.dupes_tab
        d.click("完整比對後重新整理", within=w.tab_dupes)
        if not dt.tree.get_children():
            raise GuiError("沒有找到重複檔案（NAS 與 115 都各有一組）")
        d.click("刪除選取的 115 副本", within=w.tab_dupes)  # 沒選取：只提示
        cloud_items = [i for i, it in dt.items.items()
                       if it["where"] == "115" and it["path"].startswith(f"{CLOUD}/文件/備份")]
        if len(cloud_items) < 2:
            raise GuiError(f"115 上的重複檔案沒有列出：{[it['path'] for it in dt.items.values()]}")
        dt.tree.selection_set(cloud_items[-1])
        d.click("刪除選取的 115 副本", within=w.tab_dupes)
        left = [n for n in d.cloud_files("文件") if n.startswith("備份")]
        if len(left) != 1:
            raise GuiError(f"刪除 115 副本失敗：{left}")
        nas_items = [i for i, it in dt.items.items() if it["where"] == "NAS"]
        if nas_items:
            dt.tree.selection_set(nas_items[0])
            d.click("開啟位置", within=w.tab_dupes)
        d.click("重新整理（用最近一次比對的索引）", within=w.tab_dupes)

    d.step("重複檔案：分析、刪除 115 副本、開啟位置", duplicates)

    def history():
        w.nb.select(w.tab_log)
        d.click("重新整理", within=w.tab_log)
        if not w.hist_tree.get_children():
            raise GuiError("同步歷史是空的")

    d.step("紀錄：同步歷史", history)

    # ------------------------------------------------------------ 設定與排程
    def settings():
        w.nb.select(w.tab_settings)
        v = w.settings_vars
        old_browser = v["browser_path"].get()
        d.files["askopenfilename"] = [os.path.join(d.workdir, "115chrome.exe")]
        d.click("選擇 115 瀏覽器…", within=w.tab_settings)
        if not v["browser_path"].get().endswith("115chrome.exe"):
            raise GuiError("選擇 115 瀏覽器失敗")
        v["browser_path"].set(old_browser)
        old_interval = v["api_min_interval"].get()
        v["api_min_interval"].set("abc")
        d.expect("設定格式錯誤")
        d.click("儲存設定", within=w.tab_settings)
        v["api_min_interval"].set(old_interval)
        d.click("儲存設定", within=w.tab_settings)
        if str(w.statusbar.cget("text")) != "設定已儲存":
            raise GuiError(f"儲存設定失敗：{w.statusbar.cget('text')}")

    d.step("設定：選擇 115 瀏覽器、儲存（含格式錯誤提示）", settings)

    def schedules():
        box = w.sched_tree.frame.master
        n = len(w.config.settings.schedules)
        w.sched_time.set("25:99")
        d.expect("時間格式錯誤")
        d.click("新增", within=box)
        w.sched_time.set("03:15")
        w.sched_action.set("只比對")
        d.click("新增", within=box)
        if len(w.config.settings.schedules) != n + 1:
            raise GuiError("新增排程失敗")
        x = w.config.settings.schedules[-1]
        w.sched_tree.selection_set(x["id"])
        d.click("啟用/停用", within=box)
        if x.get("enabled", True):
            raise GuiError("停用排程失敗")
        w.sched_tree.selection_set(x["id"])
        d.click("啟用/停用", within=box)
        w.sched_tree.selection_set(x["id"])
        d.click("立即執行", within=box)
        w.sched_tree.selection_set(x["id"])
        d.click("刪除", within=box)
        if len(w.config.settings.schedules) != n:
            raise GuiError("刪除排程失敗")

    d.step("排程：新增（含格式錯誤提示）、停用、立即執行、刪除", schedules)
