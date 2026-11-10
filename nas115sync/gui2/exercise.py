"""圖形介面實機演練：依序操作每一頁的每個按鈕與選單，對話框以腳本自動回答，確認全程沒有任何錯誤。

供 pytest（tests/test_gui.py）與打包後 exe 的自我測試（--self-test）共用，
讓真正的 Windows 視窗也被完整操作一遍。只能用在示範模式（模擬 115）。
演練期間「開啟檔案總管／115 瀏覽器／網頁」與「開機自動啟動」只做紀錄，不會真的執行；
下拉選單只建立不彈出（彈出的選單在 Windows 上會等使用者點選）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import autostart
from ..compare import CONFLICT, SAVEDATA_GROUP, available_decisions
from ..config import DIRECTION_LABELS, TWO_WAY
from ..hashing import hash_bytes
from . import app as gui_app
from . import dialogs as gui_dialogs
from . import pages as gui_pages
from . import panels as gui_panels
from . import tasks as gui_tasks
from .panels import FilePanel
from .util import build_menu, menu_item

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
MODULES = (gui_app, gui_dialogs, gui_pages, gui_panels, gui_tasks)


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
        self.menus = 0
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
        self._patch(webbrowser, "open", lambda url, *_a, **_k: self.opened.append(f"web:{url}"))
        for mod in MODULES:
            if hasattr(mod, "open_local"):
                self._patch(mod, "open_local", lambda p: self.opened.append(f"NAS:{p}"))
            if hasattr(mod, "open_file"):
                self._patch(mod, "open_file", lambda p: self.opened.append(f"file:{p}"))
            if hasattr(mod, "open_in_115_browser"):
                self._patch(mod, "open_in_115_browser", lambda url, *_a: self.opened.append(f"115:{url}"))
            if hasattr(mod, "threading"):
                self._patch(mod, "threading", _ThreadSpy(threading, self.threads))
            if hasattr(mod, "popup"):
                self._patch(mod, "popup", lambda parent, items, *_a: self.menu(items, parent))
            if hasattr(mod, "popup_below"):
                self._patch(mod, "popup_below", lambda widget, items: self.menu(items, widget))
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

    def menu(self, items: list, parent=None) -> list:
        """建立選單（確認每一項的圖示與文字都能建立），但不彈出。"""
        m = build_menu(parent or self.root, items)
        if m.index("end") is None:
            raise GuiError("選單是空的")
        m.destroy()
        self.menus += 1
        return items

    def pick(self, items: list, label: str) -> None:
        """執行選單中的一項。"""
        it = menu_item(self.menu(items), label)
        if not it.get("enabled", True):
            raise GuiError(f"選單項目「{label}」是停用狀態")
        it["command"]()
        self.settle(what=label)

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
        self.press(self.find(text, within, prefix), wait, text)

    def press(self, widget, wait: bool = True, what: str = "") -> None:
        if widget.instate(["disabled"]):
            raise GuiError(f"按鈕「{what or widget.cget('text')}」是停用狀態")
        widget.invoke()
        if wait:
            self.settle(what=what or str(widget.cget("text")))

    def expect(self, title: str) -> None:
        """這一步預期會出現標題為 title 的警告／錯誤訊息（例如故意輸入錯誤格式）。"""
        self._expected.add(title)

    def said(self, title: str, since: int = 0) -> bool:
        return any(t == title for _k, t, _m in self.messages[since:])

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
        return self.win.cloud("default").files_in(CLOUD + ("/" + rel if rel else ""))

    def page(self, name: str):
        self.win.show_page(name)
        self.settle(what=f"切換到 {name}")
        if self.win.pages.current != name:
            raise GuiError(f"沒有切換到頁面 {name}")
        return self.win.pages.pages[name]

    def panels(self) -> tuple[FilePanel, FilePanel]:
        pair = self.win.explore.pairs[0]
        left, right = pair.left.active, pair.right.active
        if left is None or right is None:
            raise GuiError("左右面板沒有開啟的分頁")
        return left, right

    def names(self, panel: FilePanel) -> set[str]:
        return {it.name for it in panel.items}

    def select(self, panel: FilePanel, *names: str) -> None:
        panel.select_names(names)
        got = {it.name for it in panel.selected()}
        if got != set(names):
            raise GuiError(f"面板中選不到 {names}：目前有 {sorted(self.names(panel))}")

    def review(self, kind: str, rel_path: str | None = None):
        m = self.mapping
        for rv in self.win.db.list_reviews("pending", kind=kind):
            if rv.mapping_id == m.id and (rel_path is None or rv.rel_path == rel_path):
                return rv
        raise GuiError(f"找不到待核對項目 {kind} {rel_path or ''}")

    def decide(self, rv, decision: str) -> None:
        pg = self.page("reviews")
        pg.kind_filter.set("全部")
        pg.refresh()
        pg.tree.selection_set(str(rv.id))
        self.settle(what="選取待核對項目")
        label = dict(available_decisions(rv.kind, rv.details))[decision]
        self.click(label, within=pg.decision_bar)


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
    summary = {"steps": d.steps, "opened": len(d.opened), "messages": len(d.messages), "menus": d.menus}
    if bad:
        raise GuiError("介面演練失敗：\n" + "\n".join(f"■ {s['name']}\n  " + "\n  ".join(s["problems"])
                                                for s in bad))
    return summary


def _scenario(d: Driver) -> None:  # noqa: PLR0915 - 一步一步照順序操作
    w, ex = d.win, d.win.explore
    nas = os.path.join(d.workdir, "GUI測試NAS")
    browse_dir = os.path.join(d.workdir, "瀏覽測試")
    cloud = w.cloud("default")
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

    # ------------------------------------------------------------ 主工具列
    def toolbar():
        for name in ("explore", "accounts", "search", "sync", "scheduler", "reviews", "savedata", "dupes", "log"):
            d.page(name)
        for name in ("accounts", "sync", "explore"):
            d.press(w.nav[name], what=name)
            if w.pages.current != name:
                raise GuiError(f"主工具列的「{name}」沒有切換頁面")
        for btn in (w.btn_view, w.btn_tools, w.btn_help):
            n = d.menus
            d.press(btn, what="下拉選單")
            if d.menus != n + 1:
                raise GuiError("主工具列的下拉選單沒有開啟")
        d.pick(w.help_items(), "使用說明")
        d.pick(w.help_items(), "指令列說明")
        d.pick(w.help_items(), "關於 NAS115Sync")
        d.pick(w.tool_items(), "開啟設定資料夾")
        if not d.said("關於 NAS115Sync") or not any(o.startswith("web:") for o in d.opened):
            raise GuiError("說明選單沒有作用")

    d.step("主工具列：切換每一頁、檢視／工具／說明選單", toolbar)

    # ------------------------------------------------------------ 帳號
    def accounts():
        pg = d.page("accounts")
        d.strings["新增 115 帳號"] = ["家人帳號"]
        d.press(pg.btn_add)
        extra = next((a for a in w.config.accounts if a.label == "家人帳號"), None)
        if extra is None or pg.selected_id() != extra.id:
            raise GuiError(f"新增帳號失敗：{[a.label for a in w.config.accounts]}")
        d.strings["重新命名帳號"] = ["家人"]
        d.click("重新命名…", within=pg.detail)
        if extra.label != "家人":
            raise GuiError("帳號改名失敗")
        pg.select_account(extra.id)
        d.menu(pg.menu_items())
        d.click("在「瀏覽」頁開啟", within=pg.detail)
        right = d.panels()[1]
        if w.pages.current != "explore" or right.source.kind != "cloud" or right.source.account_id != extra.id:
            raise GuiError("沒有在「瀏覽」頁開啟這個帳號")
        ex.show_location(ex.cloud_source("default"), place=ex.cloud_source("default").root)
        d.page("accounts")
        pg.select_account(extra.id)
        d.click("移除帳號", within=pg.detail)
        if any(a.label == "家人" for a in w.config.accounts):
            raise GuiError("移除帳號失敗")
        pg.select_account("default")
        d.click("移除帳號", within=pg.detail)  # 預設帳號不能移除，只會提示
        d.click("掃碼登入…", within=pg.detail)  # 示範模式：只提示
        d.click("登出", within=pg.detail)
        d.click("115 連線測試…", within=pg.detail)
        if not any(t == "115 連線測試通過" and "✔ 秒傳" in m for _k, t, m in d.messages):
            raise GuiError(f"115 連線測試沒有通過：{d.messages[-1:]}")
        for cat in ("cloud", "nas", "all"):
            pg.cats.selection_set(cat)
            d.settle(what="帳號分類")
        pg.filter.set("不存在的名稱")
        d.settle()
        if pg.cards.order:
            raise GuiError("帳號篩選沒有作用")
        pg.filter.set("")
        d.press(pg.btn_reload)
        if "a:default" not in pg.cards.cards or pg.info.get("default", {}).get("state") != "demo":
            raise GuiError(f"帳號卡片沒有顯示：{pg.cards.order} {pg.info}")

    d.step("帳號：新增、改名、在瀏覽頁開啟、移除、115 連線測試、分類與篩選", accounts)

    # ------------------------------------------------------------ 同步
    def add_script(dlg):
        dlg.name.set("GUI 測試同步")
        dlg.local.set(nas)
        dlg.cloud.set(CLOUD)
        d.click(DIRECTION_LABELS[TWO_WAY], within=dlg, wait=False)
        d.click("確定", within=dlg, wait=False)

    def confirm_script(dlg):
        d.settle(until=lambda: dlg.info is not None, what="讀取 NAS 與 115 內容")
        d.click("在 115 瀏覽器開啟", within=dlg, wait=False)
        d.click("開啟 NAS 資料夾", within=dlg, wait=False)
        d.click("我已本人核對：NAS 與 115 路徑對應正確", within=dlg, wait=False)
        d.click("確認對應", within=dlg, wait=False)

    def add_sync():
        pg = d.page("sync")
        d.scripts["AddMappingDialog"] = [add_script]
        d.scripts["ConfirmMappingDialog"] = [confirm_script]
        d.press(pg.btn_new)
        m = d.mapping
        if not m or not m.confirmed or m.direction != TWO_WAY or m.title != "GUI 測試同步":
            raise GuiError(f"建立並核對同步失敗：{m}")
        if pg.cards.selected != m.id or m.id not in pg.cards.cards:
            raise GuiError("新的同步沒有出現在清單")

    d.step("同步：建立新的同步並由本人核對", add_sync)

    def edit_sync():
        pg = d.page("sync")
        m = d.mapping

        def script(dlg):
            dlg.exclude.set("*.tmp")
            d.scripts["CloudFolderDialog"] = [lambda c: (d.settle(until=lambda: len(c.nodes) > 1, what="115 資料夾樹"),
                                                         c.path.set(CLOUD), d.click("確定", within=c, wait=False))]
            dlg._browse_cloud()
            d.click("確定", within=dlg, wait=False)

        pg.select(m.id)
        d.scripts["AddMappingDialog"] = [script]
        d.click("編輯…", within=pg.detail)
        if d.mapping.exclude_patterns != ["*.tmp"] or not d.mapping.confirmed:
            raise GuiError("編輯失敗或錯誤地取消了核對")
        d.click("停用", within=pg.detail)
        if d.mapping.enabled:
            raise GuiError("停用失敗")
        d.click("啟用", within=pg.detail)
        n = len(d.opened)
        d.click("開啟 NAS 資料夾", within=pg.detail)
        d.click("在 115 瀏覽器開啟", within=pg.detail)
        if len(d.opened) != n + 2:
            raise GuiError(f"開啟 NAS／115 失敗：{d.opened[n:]}")
        d.menu(pg.menu_items())
        d.click("在「瀏覽」頁並排", within=pg.detail)
        left, right = d.panels()
        if w.pages.current != "explore" or left.place != nas or right.source.path_text(right.place) != CLOUD:
            raise GuiError(f"沒有在瀏覽頁並排開啟：{left.place} {right.source.path_text(right.place)}")

    d.step("同步：編輯、停用／啟用、開啟位置、在瀏覽頁並排", edit_sync)

    def suggest():
        pg = d.page("sync")

        def script(dlg):
            dlg.nas_root.set(nas)
            dlg.cloud_root.set("/GUI建議")
            d.click("產生建議", within=dlg, wait=False)
            d.settle(until=lambda: len(dlg.tree.get_children()) >= 3, what="產生建議")
            d.click("全選", within=dlg, wait=False)
            d.click("加入選取項目", within=dlg, wait=False)

        d.scripts["SuggestDialog"] = [script]
        d.press(pg.btn_suggest)
        added = [m for m in w.config.mappings if m.cloud_path.startswith("/GUI建議/")]
        if len(added) != 3 or any(m.confirmed for m in added):
            raise GuiError(f"自動建議應加入 3 組待核對的同步：{[(m.cloud_path, m.confirmed) for m in added]}")
        pg.select(added[0].id)
        d.click("比較", within=pg.detail)  # 尚未核對：只提示要先核對
        d.click("取消核對", within=pg.detail)
        export = os.path.join(d.workdir, "同步設定.json")
        d.files["asksaveasfilename"] = [export]
        d.press(pg.btn_export)
        with open(export, encoding="utf-8") as f:
            saved = json.load(f)["nas115sync_syncs"]
        if len(saved) != 4:
            raise GuiError(f"匯出的同步數量不對：{len(saved)}")
        for m in added:
            pg.select(m.id)
            d.click("刪除同步", within=pg.detail)
        if any(m.cloud_path.startswith("/GUI建議/") for m in w.config.mappings):
            raise GuiError("刪除同步失敗")
        d.files["askopenfilename"] = [export]
        d.press(pg.btn_import)
        back = [m for m in w.config.mappings if m.cloud_path.startswith("/GUI建議/")]
        if len(back) != 3 or any(m.confirmed for m in back) or len(w.config.mappings) != 4:
            raise GuiError(f"匯入後應多 3 組待核對的同步：{[(m.cloud_path, m.confirmed) for m in w.config.mappings]}")
        for m in back:
            pg.select(m.id)
            d.click("刪除同步", within=pg.detail)
        bad = os.path.join(d.workdir, "不是設定.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        d.files["askopenfilename"] = [bad]
        d.expect("匯入失敗")
        d.press(pg.btn_import)

    d.step("同步：自動建議、匯出／匯入、刪除", suggest)

    # ------------------------------------------------------------ 比較與同步
    def compare_and_sync():
        pg = d.page("sync")
        m = d.mapping
        d.press(pg.btn_compare)
        if not d.said("比對完成") or m.id not in pg.plans:
            raise GuiError("沒有出現「比對完成」")
        csv_path = os.path.join(d.workdir, "比對報告.csv")
        d.files["asksaveasfilename"] = [csv_path]
        d.pick(w.tool_items(), "匯出比對報告（CSV）…")
        if not os.path.getsize(csv_path):
            raise GuiError("CSV 報告是空的")
        d.click("開啟", within=pg.cards.cards[m.id])
        sw = w.sync_window
        if sw is None or not sw.winfo_exists() or sw.plan is None or not sw.rows:
            raise GuiError("同步視窗沒有顯示比較結果")
        actions = {r[1] for r in sw.rows}
        if not {"upload", "download", "review"} <= actions:
            raise GuiError(f"比較結果應有上傳、下載與待核對：{actions}")
        d.menu(sw.menu_items())
        for key in sw.action_on:
            d.pick(sw._filter_items(gui_dialogs.ACTION_FILTERS, sw.action_on), dict(gui_dialogs.ACTION_FILTERS)[key])
            d.pick(sw._filter_items(gui_dialogs.ACTION_FILTERS, sw.action_on), dict(gui_dialogs.ACTION_FILTERS)[key])
        d.pick(sw._filter_items(gui_dialogs.STATE_FILTERS, sw.state_on), "不處理")
        if any(r[1] in ("review", "equal") for r in sw.shown):
            raise GuiError("狀態篩選沒有作用")
        d.pick(sw._filter_items(gui_dialogs.STATE_FILTERS, sw.state_on), "不處理")
        d.press(sw.btn_state, what="狀態篩選")
        d.press(sw.btn_action, what="動作篩選")
        sw.search.set("報告")
        sw.apply_filters()
        if [r[2] for r in sw.shown] != ["文件/報告.txt"]:
            raise GuiError(f"同步視窗的搜尋不對：{[r[2] for r in sw.shown]}")
        sw.search.set("")
        sw.apply_filters()
        sw.grid_list.selection_set([sw.shown[0][0]])
        sw.open_local(sw.shown[0][2])
        report = os.path.join(d.workdir, "同步視窗.csv")
        d.files["asksaveasfilename"] = [report]
        d.click("匯出 CSV…", within=sw)
        if not os.path.getsize(report):
            raise GuiError("同步視窗匯出的 CSV 是空的")
        d.press(sw.btn_start, what="開始")
        if not d.said("同步完成"):
            raise GuiError("沒有出現「同步完成」")
        if not os.path.exists(os.path.join(nas, "只在115.txt")):
            raise GuiError("只在 115 的檔案沒有下載到 NAS")
        if "報告.txt" not in d.cloud_files("文件"):
            raise GuiError("NAS 檔案沒有上傳到 115")
        if not any(s[0] == "done" for s in sw.status.values()):
            raise GuiError(f"同步視窗沒有更新每個檔案的狀態：{sw.status}")
        if m.id in pg.plans:
            raise GuiError("同步完成後比較結果應清除")
        d.click("比較", within=sw)
        if sw.plan is None:
            raise GuiError("同步視窗的「比較」沒有作用")
        d.click("關閉", within=sw)
        if sw.winfo_exists():
            raise GuiError("同步視窗沒有關閉")
        d.press(pg.btn_sync)  # 沒有比較結果或沒有要做的事：不可出錯

    d.step("同步：全部比較、匯出 CSV、同步視窗（篩選、搜尋、開始）", compare_and_sync)

    def busy_and_cancel():
        pg = d.page("sync")
        n = len(d.messages)
        d.press(pg.btn_compare, wait=False)
        d.press(pg.btn_compare, wait=False)  # 工作中再按：只提示請稍候
        d.press(w.cancel_btn, what="取消")
        if not d.said("請稍候", n):
            raise GuiError("工作進行中再按沒有提示")

    d.step("同步：工作中重複按、取消工作", busy_and_cancel)

    # ------------------------------------------------------------ 待核對
    def show_reviews():
        d.press(w.nav["reviews"], what="待核對")
        pg = w.reviews_page
        if w.pages.current != "reviews" or "(" not in str(w.nav["reviews"].cget("text")):
            raise GuiError(f"主工具列的待核對沒有顯示數量：{w.nav['reviews'].cget('text')}")
        d.press(pg.btn_refresh)
        for label in pg.kind_filter.cget("values"):
            pg.kind_filter.set(label)
            pg.kind_filter.event_generate("<<ComboboxSelected>>")
            d.settle(what=f"篩選 {label}")
        pg.kind_filter.set("全部")
        pg.refresh()
        items = pg.tree.get_children()
        if not items:
            raise GuiError("沒有任何待核對項目（應有衝突、遊戲存檔等）")
        for iid in items:
            pg.tree.selection_set(iid)
            d.settle(what="顯示待核對項目")
            text = pg.detail.get("1.0", "end")
            if f"#{iid}" not in text or not pg.decision_bar.winfo_children():
                raise GuiError(f"待核對 #{iid} 沒有顯示詳細內容或決定按鈕")
            d.press(pg.btn_open_local)
            d.press(pg.btn_open_cloud)
        pg.tree.selection_set(items[0])
        d.press(pg.btn_all)
        rv = d.review(CONFLICT, "文件/衝突.txt")
        pg.select_path(rv.mapping_id, rv.rel_path)
        if pg.tree.selection() != (str(rv.id),):
            raise GuiError("從同步視窗跳到待核對項目失敗")

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
        w.reviews_page.proc_var.set("game.exe")
        d.decide(rv, "approve")
        g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
        if g.status != "approved" or g.process_names != ["game.exe"]:
            raise GuiError(f"存檔核可失敗：{g}")
        if "slot1.sav" not in d.cloud_files("遊戲/Saves"):
            raise GuiError("核可後存檔沒有上傳")

    d.step("待核對：核可遊戲存檔（填入遊戲程序）", approve_savedata)

    # ------------------------------------------------------------ 遊戲存檔
    def savedata():
        d.pick(w.tool_items(), "遊戲存檔")
        pg = w.savedata_page
        if w.pages.current != "savedata":
            raise GuiError("工具選單沒有開啟遊戲存檔頁")
        iid = next(i for i, (m, _g) in pg.items.items() if m.id == d.mapping.id)
        pg.tree.selection_set(iid)
        d.menu(pg.menu_items())
        d.strings["遊戲程序"] = ["game.exe, launcher.exe"]
        d.press(pg.btn_procs)
        g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
        if g.process_names != ["game.exe", "launcher.exe"]:
            raise GuiError(f"設定遊戲程序失敗：{g.process_names}")
        with open(os.path.join(nas, "遊戲", "Saves", "slot1.sav"), "wb") as f:
            f.write(b"save v2" + tag)
        pg.tree.selection_set(iid)
        d.press(pg.btn_sync)
        files = d.cloud_files("遊戲/Saves")
        if list(files) != ["slot1.sav"] or files["slot1.sav"].sha1 != hash_bytes(b"save v2" + tag).sha1:
            raise GuiError(f"存檔沒有更新或 115 舊版沒有刪除：{list(files)}")
        pg.tree.selection_set(iid)
        d.press(pg.btn_open)
        for btn, status in ((pg.btn_normal, "normal"), (pg.btn_exclude, "exclude"), (pg.btn_approve, "approved")):
            pg.tree.selection_set(iid)
            d.press(btn)
            g = next(g for g in w.db.list_groups() if g.mapping_id == d.mapping.id)
            if g.status != status:
                raise GuiError(f"「{btn.cget('text')}」後狀態應為 {status}，實際 {g.status}")

    d.step("遊戲存檔：設定程序、立即同步、變更狀態", savedata)

    # ------------------------------------------------------------ 即時同步
    def realtime():
        pg = d.page("sync")
        d.press(pg.rt_toggle, what="即時同步")
        if not w.rt.running or not pg.rt_var.get() or "開" not in str(w.rt_btn.cget("text")):
            raise GuiError("即時同步沒有啟動")
        p = os.path.join(nas, "即時", "新檔案.txt")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"realtime" + tag)
        d.settle(until=lambda: "新檔案.txt" in d.cloud_files("即時"), timeout=90, what="即時上傳")
        d.press(w.rt_btn, wait=False, what="停止即時同步")
        d.settle(until=lambda: not w.rt.running and str(w.rt_btn.cget("text")) == "即時同步：關" and not pg.rt_var.get(),
                 what="停止即時同步")

    d.step("即時同步：NAS 新增檔案自動上傳", realtime)

    # ------------------------------------------------------------ 瀏覽：本機面板
    def browse_local():
        d.page("explore")
        ex.set_view("two")
        ex.show_location(ex.local, text=nas)
        d.settle(what="開啟 NAS 資料夾")
        left, _right = d.panels()
        if left.place != nas or "相片" not in d.names(left):
            raise GuiError(f"本機面板沒有開啟：{left.place}")
        d.select(left, "相片")
        left.open_selected()
        d.settle()
        if os.path.basename(left.place) != "相片":
            raise GuiError("開啟資料夾沒有進入")
        d.select(left, "旅行.jpg")
        left.open_selected()  # 檔案：用預設程式開啟（演練時只記錄）
        d.scripts["PropertiesDialog"] = [lambda dlg: (
            d.settle(until=lambda: len(dlg.fields["SHA-1"].get()) == 40, what="計算 SHA-1"),
            d.click("關閉", within=dlg, wait=False))]
        left.menu("內容")
        d.settle()
        left.go_up()
        d.settle()
        d.press(left.btn_refresh)
        if left.place != nas:
            raise GuiError("回上一層失敗")
        d.press(left.btn_back)
        d.press(left.btn_fwd)
        for col in ("size", "date", "#0", "#0"):
            left.sort_by(col)
        left.crumb.edit()
        left.crumb._var.set(browse_dir)
        left.crumb._commit()
        d.settle()
        if left.place != browse_dir:
            raise GuiError(f"位址列輸入路徑沒有作用：{left.place}")
        d.strings["新資料夾"] = ["新資料夾A"]
        left.new_folder()
        d.settle()
        d.select(left, "新資料夾A")
        d.strings["重新命名"] = ["改名後"]
        left.menu("重新命名")
        d.settle()
        if not os.path.isdir(os.path.join(browse_dir, "改名後")):
            raise GuiError("本機新增資料夾或改名失敗")
        d.select(left, "改名後")
        left.menu("刪除")
        d.settle()
        if os.path.exists(os.path.join(browse_dir, "改名後")) or "改名後" in d.names(left):
            raise GuiError("移到資源回收筒失敗")
        n = len(d.opened)
        left.menu("在檔案總管開啟")
        left.menu("複製路徑")
        if len(d.opened) != n + 1 or left.clipboard_get() != browse_dir:
            raise GuiError("在檔案總管開啟或複製路徑失敗")
        d.strings["新資料夾"] = ["bad:name?"]
        d.expect("名稱不能使用")
        left.new_folder()
        d.settle()
        for btn in (left.btn_loc, left.btn_view, left.btn_marks, left.btn_quota):
            d.press(btn, what="面板選單")
        d.menu(left.menu_items())
        d.pick(left.view_items(), "顯示「大小」")
        d.pick(left.view_items(), "顯示「大小」")
        left.tree_var.set(not left.tree_var.get())
        left._toggle_tree()
        left.tree_var.set(not left.tree_var.get())
        left._toggle_tree()
        ex.hidden_var.set(True)
        ex.hidden_changed()
        d.settle()
        ex.hidden_var.set(False)
        ex.hidden_changed()
        d.settle()

    d.step("瀏覽：本機面板（開啟、內容、上一層、排序、位址列、新增、改名、刪除、選單）", browse_local)

    def bookmarks():
        left, _right = d.panels()
        d.press(left.btn_star)
        marks = w.config.settings.bookmarks
        if len(marks) != 1 or marks[0].get("path") != browse_dir:
            raise GuiError(f"加入書籤失敗：{marks}")
        left.go(nas)
        d.settle()
        d.pick(left.bookmark_items(), marks[0]["name"])
        if left.place != browse_dir:
            raise GuiError("開啟書籤失敗")

        def script(dlg):
            dlg.tree.selection_set("0")
            d.strings["重新命名書籤"] = ["我的書籤"]
            d.click("重新命名…", within=dlg, wait=False)
            if w.config.settings.bookmarks[0]["name"] != "我的書籤":
                raise GuiError("書籤改名失敗")
            dlg.tree.selection_set("0")
            d.click("刪除書籤", within=dlg, wait=False)
            d.click("關閉", within=dlg, wait=False)

        d.scripts["BookmarksDialog"] = [script]
        d.pick(w.tool_items(), "編輯書籤…")
        if w.config.settings.bookmarks:
            raise GuiError("刪除書籤失敗")

    d.step("瀏覽：書籤（加入、開啟、改名、刪除）", bookmarks)

    # ------------------------------------------------------------ 瀏覽：115 面板與傳輸
    def browse_cloud():
        left, right = d.panels()
        pair = ex.pairs[0]
        src = ex.cloud_source("default")
        ex.show_location(src, text=CLOUD)
        d.settle(what="開啟 115 資料夾")
        if right.source.path_text(right.place) != CLOUD or "文件" not in d.names(right):
            raise GuiError(f"115 面板沒有開啟 {CLOUD}：{right.source.path_text(right.place)}")
        d.select(right, "文件")
        right.open_selected()
        d.settle(what="進入 115 子資料夾")
        if right.source.path_text(right.place) != CLOUD + "/文件":
            raise GuiError("開啟 115 資料夾沒有進入")
        right.go_up()
        d.settle()
        right.go_text("/")
        d.settle()
        d.strings["新資料夾"] = ["GUI瀏覽"]
        right.new_folder()
        d.settle()
        d.select(right, "GUI瀏覽")
        right.open_selected()
        d.settle()
        if right.source.path_text(right.place) != "/GUI瀏覽":
            raise GuiError(f"115 新資料夾失敗：{right.source.path_text(right.place)}")

        d.select(left, "上傳我.txt", "子資料夾")
        d.press(pair.btn_right, what="傳到右邊")
        if not {"上傳我.txt", "子資料夾"} <= d.names(right):
            raise GuiError(f"上傳後 115 面板沒有檔案：{d.names(right)}")
        ex.compare(left)
        d.settle(what="比較兩邊")
        if left.compare.get("上傳我.txt".casefold()) != "same":
            raise GuiError(f"比較結果不對：{left.compare}")

        d.select(right, "上傳我.txt")
        d.scripts["PropertiesDialog"] = [lambda dlg: d.click("關閉", within=dlg, wait=False)]
        right.menu("內容")
        d.strings["重新命名"] = ["已改名.txt"]
        right.menu("重新命名")
        d.settle()
        d.select(right, "已改名.txt")
        d.press(pair.btn_left, what="傳到左邊")
        if not os.path.exists(os.path.join(browse_dir, "已改名.txt")):
            raise GuiError("下載失敗")
        d.select(right, "子資料夾")
        d.scripts["CloudFolderDialog"] = [lambda c: (c._new_folder(), d.click("確定", within=c, wait=False))]
        d.strings["新資料夾"] = ["移動目的"]
        right.menu("移動到…")
        d.settle()
        if "子資料夾" in d.names(right) or "移動目的" not in d.names(right):
            raise GuiError(f"移動後的內容不對：{d.names(right)}")
        # 複製／貼上：把本機檔案貼到 115（只秒傳：115 上已有相同內容，所以不會實際上傳）
        ex.instant_var.set(True)
        d.select(left, "已改名.txt")
        left.menu("複製")
        d.select(right, "移動目的")
        right.open_selected()
        d.settle()
        right.menu("貼上")
        d.settle()
        ex.instant_var.set(False)
        if "已改名.txt" not in d.names(right):
            raise GuiError(f"貼上（秒傳）失敗：{d.names(right)}")
        right.go_up()
        d.settle()
        d.select(right, "已改名.txt")
        right.menu("刪除")
        d.settle()
        if "已改名.txt" in d.names(right):
            raise GuiError("115 刪除失敗")
        n = len(d.opened)
        right.menu("在 115 瀏覽器開啟")
        right.menu("複製路徑")
        if len(d.opened) != n + 1 or right.clipboard_get() != "/GUI瀏覽":
            raise GuiError("在 115 瀏覽器開啟或複製路徑失敗")
        for btn in (right.btn_loc, right.btn_view, right.btn_marks, right.btn_quota, right.btn_refresh):
            d.press(btn, what="面板選單")
        d.menu(right.menu_items())
        d.pick(right.view_items(), "顯示「SHA-1」（115）")
        d.pick(right.view_items(), "顯示「SHA-1」（115）")
        d.pick(right.view_items(), "略過")
        if w.config.settings.manual_conflict != "skip":
            raise GuiError("同名處理方式選擇失敗")
        d.pick(right.view_items(), "保留兩者（新檔加上時間）")

    d.step("瀏覽：115 面板（新資料夾、上傳、比較、改名、下載、移動、貼上秒傳、刪除）", browse_cloud)

    def tabs_and_views():
        pair = ex.pairs[0]
        left, right = d.panels()
        g = pair.right
        n = len(g.panels)
        d.press(g.tabs.plus, what="新增分頁")
        chooser = g.panels[-1]
        if len(g.panels) != n + 1 or isinstance(chooser, FilePanel):
            raise GuiError("新增分頁沒有顯示「選擇要連線的位置」")
        chooser.choose(chooser.tree.get_children()[0])  # 本機
        d.settle()
        new = g.active
        if new is None or new.source.kind != "local":
            raise GuiError("新分頁沒有連到本機")
        new.go(browse_dir)
        d.settle()
        # 本機 → 本機 複製
        d.select(left, "上傳我.txt")
        left.go(nas)
        d.settle()
        d.select(left, "只在115.txt")
        ex.transfer(left, new)
        d.settle()
        if not os.path.exists(os.path.join(browse_dir, "只在115.txt")):
            raise GuiError("本機之間複製失敗")
        g.select(n - 1)
        g.close_tab(n)
        if len(g.panels) != n or g.active is not right:
            raise GuiError("關閉分頁失敗")
        pair.link_var.set(True)
        left.go(nas)
        ex.show_location(ex.cloud_source("default"), text=CLOUD)
        d.settle()
        d.select(left, "文件")
        left.open_selected()
        d.settle()
        if right.source.path_text(right.place) != CLOUD + "/文件":
            raise GuiError(f"連動瀏覽沒有作用：{right.source.path_text(right.place)}")
        pair.link_var.set(False)
        d.scripts["AddMappingDialog"] = [lambda dlg: (
            None if (dlg.local.get(), dlg.cloud.get()) == (os.path.join(nas, "文件"), CLOUD + "/文件")
            else d.errors.append(f"建立同步帶入的路徑不對：{dlg.local.get()} {dlg.cloud.get()}"),
            d.click("取消", within=dlg, wait=False))]
        ex.sync_folders(pair)
        d.settle()
        d.page("explore")
        for view in ("one", "four", "two"):
            d.pick(w.view_items(), {"one": "單一面板", "two": "兩個面板", "four": "四個面板"}[view])
            if ex.view != view or len(ex.groups()) != {"one": 1, "two": 2, "four": 4}[view]:
                raise GuiError(f"檢視切換失敗：{view}")
        d.pick(w.view_items(), "顯示工作清單")
        if w.tasks_visible():
            raise GuiError("隱藏工作清單失敗")
        d.pick(w.view_items(), "顯示工作清單")
        if not w.tasks_visible():
            raise GuiError("顯示工作清單失敗")

    d.step("瀏覽：分頁、本機互傳、連動瀏覽、建立同步、單一／兩個／四個面板", tabs_and_views)

    def task_list():
        d.page("explore")
        ta = w.tasks
        ta.refresh(force=True)
        gl = ta.grid_list
        if not ta.count or not len(gl):
            raise GuiError("工作清單沒有任何紀錄")
        ops = {str(r["cells"]["op"][1]) for r in gl.rows}
        if not any(o.startswith("同步") for o in ops) or not any("上傳" in o for o in ops):
            raise GuiError(f"工作清單應有同步與上傳的工作：{ops}")
        d.press(ta.btn_pause, what="全部暫停")
        if not ta.mgr.paused.is_set():
            raise GuiError("暫停失敗")
        d.press(ta.btn_pause, what="全部繼續")
        if ta.mgr.paused.is_set():
            raise GuiError("繼續失敗")
        d.menu(ta.menu_items())
        first = gl.ids()[0]
        gl.expand(first, False)
        gl.expand(first, True)
        gl.selection_set([first])
        ta.cancel_selected()
        ta.clear()
        d.settle()
        if ta.count:
            raise GuiError("清除已完成的工作失敗")

    d.step("工作清單：暫停／繼續、展開、取消、清除", task_list)

    # ------------------------------------------------------------ 搜尋
    def search():
        pg = d.page("search")
        pg.query.set("報告")
        d.press(pg.btn_search)
        where = {r["where"] for r in pg.results}
        if "NAS" not in where or len(where) < 2:
            raise GuiError(f"搜尋結果應同時有 NAS 與 115：{pg.results}")
        for key, cb in pg.filters.items():
            for label in cb.cget("values"):
                cb.set(label)
                cb.event_generate("<<ComboboxSelected>>")
                d.settle(what=f"搜尋篩選 {key}")
            cb.set(cb.cget("values")[0])
        pg._apply()
        d.menu(pg.menu_items())
        for iid, r in list(pg.shown.items()):
            pg.tree.selection_set(iid)
            pg.open_item()
            pg.copy_name()
            d.page("search")
            pg.tree.selection_set(iid)
            d.press(pg.btn_view, what="檢視資料夾")
            panel = d.panels()[0 if r["account_id"] is None else 1]
            if w.pages.current != "explore" or r["name"] not in {it.name for it in panel.selected()}:
                raise GuiError(f"檢視資料夾沒有開到檔案所在位置：{r['name']} {panel.place}")
        pg = d.page("search")
        for target in pg.target.cget("values"):
            pg.target.set(target)
            pg.query.set("旅行")
            d.press(pg.btn_search)
        if not pg.results or any(r["where"] != "NAS" for r in pg.results):
            raise GuiError(f"只搜尋 NAS 索引時的結果不對：{pg.results}")
        if w.config.settings.ui.get("searches", [])[:2] != ["旅行", "報告"]:
            raise GuiError("搜尋紀錄沒有保存")

    d.step("搜尋：115 與 NAS、日期／類型／大小篩選、檢視資料夾", search)

    # ------------------------------------------------------------ 重複檔案
    def duplicates():
        d.pick(w.tool_items(), "重複檔案")
        pg = w.dupes_page
        d.press(pg.btn_rescan)
        if not pg.tree.get_children():
            raise GuiError("沒有找到重複檔案（NAS 與 115 都各有一組）")
        d.press(pg.btn_delete)  # 沒選取：只提示
        cloud_items = [i for i, it in pg.items.items()
                       if it["where"] == "115" and it["path"].startswith(f"{CLOUD}/文件/備份")]
        if len(cloud_items) < 2:
            raise GuiError(f"115 上的重複檔案沒有列出：{[it['path'] for it in pg.items.values()]}")
        pg.tree.selection_set(cloud_items[0])
        d.press(pg.btn_open)
        pg.tree.selection_set(cloud_items[-1])
        d.press(pg.btn_delete)
        left = [n for n in d.cloud_files("文件") if n.startswith("備份")]
        if len(left) != 1:
            raise GuiError(f"刪除 115 副本失敗：{left}")
        nas_items = [i for i, it in pg.items.items() if it["where"] == "NAS"]
        if nas_items:
            pg.tree.selection_set(nas_items[0])
            d.press(pg.btn_open)
        d.press(pg.btn_refresh)

    d.step("重複檔案：分析、刪除 115 副本、開啟位置", duplicates)

    def history():
        d.pick(w.tool_items(), "紀錄與同步歷史")
        pg = w.log_page
        d.press(pg.btn_refresh)
        if not pg.tree.get_children() or "秒傳" not in str(pg.stats.cget("text")):
            raise GuiError("同步歷史是空的")
        d.press(pg.btn_folder)
        d.press(pg.btn_clear)
        if pg.text.get("1.0", "end").strip():
            raise GuiError("清除畫面上的紀錄失敗")

    d.step("紀錄：同步歷史與秒傳統計", history)

    # ------------------------------------------------------------ 選項
    def options():
        def script(dlg):
            for iid in dlg.cats.get_children():
                dlg.cats.selection_set(iid)
                dlg.update()
                if dlg.current is not dlg.frames[iid]:
                    raise GuiError("選項分類沒有切換")
            dlg.show_category("一般")
            d.files["askopenfilename"] = [os.path.join(d.workdir, "115chrome.exe")]
            d.click("瀏覽…", within=dlg, wait=False)
            if not dlg.vars["browser_path"].get().endswith("115chrome.exe"):
                raise GuiError("選擇 115 瀏覽器失敗")
            dlg.vars["browser_path"].set("")
            old = dlg.vars["api_min_interval"].get()
            dlg.vars["api_min_interval"].set("abc")
            d.click("確定", within=dlg, wait=False)
            if not dlg.winfo_exists():
                raise GuiError("格式錯誤時不應關閉選項視窗")
            dlg.vars["api_min_interval"].set(old)
            dlg.vars["upload_limit_kb"].set("500")
            dlg.vars["show_hidden"].set(True)
            d.click("確定", within=dlg, wait=False)

        d.scripts["OptionsDialog"] = [script, lambda dlg: (
            dlg.vars["upload_limit_kb"].set("0"), dlg.vars["show_hidden"].set(False),
            d.click("確定", within=dlg, wait=False)), lambda dlg: d.click("取消", within=dlg, wait=False)]
        d.expect("設定格式錯誤")
        d.press(w.btn_options, what="選項")
        s = w.config.settings
        if s.upload_limit_kb != 500 or not s.show_hidden or not ex.hidden_var.get():
            raise GuiError("選項沒有儲存")
        if str(w.statusbar.cget("text")) != "設定已儲存":
            raise GuiError(f"儲存設定失敗：{w.statusbar.cget('text')}")
        d.press(w.btn_options, what="選項")
        if s.upload_limit_kb != 0 or s.show_hidden:
            raise GuiError("選項沒有還原")
        d.press(w.btn_options, what="選項")  # 取消：不變更

    d.step("選項：每個分類、選擇 115 瀏覽器、儲存（含格式錯誤提示）、取消", options)

    # ------------------------------------------------------------ 排程
    def schedules():
        pg = d.page("scheduler")
        n = len(w.config.settings.schedules)

        def script(dlg):
            dlg.name.set("每晚比對")
            dlg.time.set("25:99")
            d.click("確定", within=dlg, wait=False)
            if not dlg.winfo_exists():
                raise GuiError("時間格式錯誤時不應關閉視窗")
            dlg.time.set("03:15")
            dlg.action.set("只比對")
            d.click("所有已核對的同步", within=dlg, wait=False)
            dlg.map_vars[d.mapping.id].set(True)
            d.click("確定", within=dlg, wait=False)

        d.scripts["ScheduleDialog"] = [script]
        d.expect("時間格式錯誤")
        d.press(pg.btn_new)
        if len(w.config.settings.schedules) != n + 1:
            raise GuiError("新增排程失敗")
        x = w.config.settings.schedules[-1]
        if (x["name"], x["time"], x["action"], x["mapping_ids"]) != ("每晚比對", "03:15", "scan", [d.mapping.id]):
            raise GuiError(f"排程內容不對：{x}")
        pg.tree.selection_set(x["id"])
        d.menu(pg.menu_items())
        d.press(pg.btn_report)  # 還沒執行過：只提示
        d.press(pg.btn_toggle)
        if x.get("enabled", True):
            raise GuiError("停用排程失敗")
        d.press(pg.btn_toggle)
        d.press(pg.btn_run)
        if not x.get("last_run") or "✔" not in pg.tree.set(x["id"], "result"):
            raise GuiError(f"立即執行後沒有記錄結果：{x}")
        d.press(pg.btn_report)
        d.scripts["ScheduleDialog"] = [lambda dlg: (dlg.time.set("04:30"), d.click("確定", within=dlg, wait=False))]
        d.press(pg.btn_edit)
        if x["time"] != "04:30":
            raise GuiError("編輯排程失敗")
        sp = d.page("sync")
        sp.select(d.mapping.id)
        d.scripts["ScheduleDialog"] = [lambda dlg: d.click("取消", within=dlg, wait=False)]
        d.click("新增排程…", within=sp.detail)
        pg = d.page("scheduler")
        pg.tree.selection_set(x["id"])
        d.press(pg.btn_delete)
        if len(w.config.settings.schedules) != n:
            raise GuiError("刪除排程失敗")

    d.step("排程：新增（含格式錯誤提示）、停用、立即執行、報告、編輯、刪除", schedules)

    def finish():
        d.page("explore")
        w.save_ui()
        w._save_ui_now()

    d.step("回到瀏覽頁並保存介面狀態", finish)
