"""NAS115Sync 圖形介面（Tkinter）：版面與操作方式比照 Air Explorer。

最上面是主工具列（瀏覽、帳號、搜尋、同步、排程、選項、檢視、待核對、工具、說明），按下去切換頁面；
「瀏覽」頁是左右兩個檔案面板，下方是工作清單；最底下的狀態列顯示目前工作、總進度與上下傳速度。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from .. import __version__
from ..app import App, RedactSecrets, open_in_115_browser, resource_path, single_instance
from ..check115 import run_check
from ..config import app_home
from ..engine import Cancelled
from ..manual import export_plans_csv
from ..scheduler import Scheduler, describe
from ..watcher import RealtimeSync
from . import dialogs, pages, theme
from .panels import ExplorePage
from .tasks import TaskArea
from .theme import px
from .util import QueueLogHandler, Tooltip, UiQueue, enable_dpi_awareness, human_size, open_local, popup_below
from .widgets import PageHost, tool_button

log = logging.getLogger(__name__)

README_URL = "https://github.com/lionsmanejellyfish302-HERO/Claude-AI-#readme"
TOOL_PAGES = ("savedata", "dupes", "log")  # 從「工具」選單開啟的頁面


class MainWindow:
    def __init__(self, root: tk.Tk, app: App, start_realtime: bool = False):
        self.root, self.app = root, app
        self.engine, self.db, self.config = app.engine, app.db, app.config
        self.ui = UiQueue(root)
        self.rt = RealtimeSync(self.engine)
        self.scheduler = Scheduler(self.engine)
        self.busy = False
        self.current_batch = 0
        self._last_progress = 0.0
        self._job_progress: tuple[int, int] | None = None
        self._reviews_dirty = False
        self._save_after = None
        self._tasks_user_sized = False
        self._closed = False
        s = self.config.settings

        root.title(f"NAS115Sync {__version__}" + ("（示範模式）" if app.demo else ""))
        theme.build(root, s.appearance)
        self._place_window()

        # ---- 主工具列
        self.toolbar = ttk.Frame(root)
        self.toolbar.pack(fill="x")
        self.nav: dict[str, ttk.Button] = {}
        for name, text in (("explore", "瀏覽"), ("accounts", "帳號"), ("search", "搜尋"), ("sync", "同步"),
                           ("scheduler", "排程")):
            self.nav[name] = self._main_button(name, text, lambda n=name: self.show_page(n))
        theme.line(self.toolbar, vertical=True).pack(side="left", fill="y", padx=px(3), pady=px(6))
        self.btn_options = self._main_button("options", "選項", self.show_options)
        self.btn_view = self._main_button("view", "檢視 ▾", lambda: popup_below(self.btn_view, self.view_items()))
        self.nav["reviews"] = self._main_button("reviews", "待核對", lambda: self.show_page("reviews"))
        self.btn_tools = self._main_button("tools", "工具 ▾", lambda: popup_below(self.btn_tools, self.tool_items()))
        self.btn_help = self._main_button("help", "▾", lambda: popup_below(self.btn_help, self.help_items()))
        theme.line(root).pack(fill="x")

        # ---- 狀態列（先放底部）
        self._build_statusbar()
        theme.line(root).pack(side="bottom", fill="x")

        # ---- 頁面
        self.pages = PageHost(root)
        self.pages.pack(fill="both", expand=True)
        self.split = ttk.PanedWindow(self.pages, orient="vertical")
        self.explore = ExplorePage(self.split, self)
        self.tasks = TaskArea(self.split, self)
        self.split.add(self.explore, weight=1)
        self.split.add(self.tasks, weight=0)
        self.split.bind("<ButtonRelease-1>", lambda e: setattr(self, "_tasks_user_sized", True), add="+")
        self.pages.add("explore", self.split)
        self.accounts_page = pages.AccountsPage(self.pages, self)
        self.search_page = pages.SearchPage(self.pages, self)
        self.sync_page = pages.SyncPage(self.pages, self)
        self.scheduler_page = pages.SchedulerPage(self.pages, self)
        self.reviews_page = pages.ReviewsPage(self.pages, self)
        self.savedata_page = pages.SavedataPage(self.pages, self)
        self.dupes_page = pages.DuplicatesPage(self.pages, self)
        self.log_page = pages.LogPage(self.pages, self)
        for name, page in (("accounts", self.accounts_page), ("search", self.search_page), ("sync", self.sync_page),
                           ("scheduler", self.scheduler_page), ("reviews", self.reviews_page),
                           ("savedata", self.savedata_page), ("dupes", self.dupes_page), ("log", self.log_page)):
            self.pages.add(name, page)
        self.sync_window: dialogs.SyncWindow | None = None
        theme.apply_titlebar(root)

        self._log_handler = QueueLogHandler(self.ui, self.log_page.append)
        self._log_handler.addFilter(RedactSecrets())
        logging.getLogger().addHandler(self._log_handler)
        self.engine.listeners.append(lambda ev, data: self.ui.call(self._on_engine_event, ev, data))
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<F5>", lambda e: self._refresh_current(), add="+")

        self.show_page(s.ui.get("page") if s.ui.get("page") in self.nav else "explore")
        self.refresh_all()
        self._tick()
        self._tick_fast()
        self.root.after(200, self._fit_tasks)
        self.scheduler.start()
        if start_realtime:
            root.after(1500, self.toggle_realtime)

    # ================================================================ 視窗與工具列
    def _place_window(self) -> None:
        root = self.root
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight() - px(56)  # 扣掉工作列
        geo = str(self.config.settings.ui.get("geometry") or "")
        try:
            w, h = (int(x) for x in geo.split("x"))
        except ValueError:
            w, h = px(1240), px(760)
        w, h = max(px(640), min(w, sw - px(16))), max(px(420), min(h, sh - px(16)))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(px(900), w), min(px(560), h))

    def _main_button(self, icon: str, text: str, command) -> ttk.Button:
        b = tool_button(self.toolbar, icon, text, command, style="Main.Tool.TButton", size=theme.TOOL)
        b.pack(side="left", padx=(px(2), 0), pady=px(2))
        return b

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root, style="Chrome.TFrame")
        bar.pack(side="bottom", fill="x")
        self.rt_btn = tool_button(bar, "dot_off", "即時同步：關", self.toggle_realtime, style="Chrome.Tool.TButton",
                                  hint="開始或停止即時同步（NAS 與 115 有變動時自動同步）")
        self.rt_btn.pack(side="left", padx=(px(2), px(4)))
        self.up_label = ttk.Label(bar, text="0 KB/s", image=theme.icon("upload"), compound="left",
                                  style="Chrome.TLabel", width=11)
        self.up_label.pack(side="right", padx=(0, px(6)))
        self.down_label = ttk.Label(bar, text="0 KB/s", image=theme.icon("download"), compound="left",
                                    style="Chrome.TLabel", width=11)
        self.down_label.pack(side="right")
        self.cancel_btn = tool_button(bar, "stop", "取消", self.cancel, style="Chrome.Tool.TButton",
                                      hint="取消目前的工作")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=px(130))
        self.progress.pack(side="right", padx=px(6), pady=px(3))
        self.total_label = ttk.Label(bar, text="", style="Chrome.TLabel")
        self.total_label.pack(side="right", padx=(px(6), 0))
        self.statusbar = ttk.Label(bar, text="就緒", style="Chrome.TLabel", anchor="w")
        self.statusbar.pack(side="left", fill="x", expand=True)
        self._rt_tip = Tooltip(self.rt_btn, "")

    def show_page(self, name: str) -> None:
        self.pages.show(name)
        current = "tools" if name in TOOL_PAGES else name
        for key, b in self.nav.items():
            if key == "reviews":
                continue
            b.configure(style=("On." if key == current else "") + "Main.Tool.TButton")
        self.btn_tools.configure(style=("On." if current == "tools" else "") + "Main.Tool.TButton")
        self._reviews_button()
        page = self.pages.pages[name]
        if hasattr(page, "on_show"):
            page.on_show()
        if name in self.nav:
            self.config.settings.ui["page"] = name
            self.save_ui()

    def _reviews_button(self) -> None:
        n = self.db.count_pending()
        on = self.pages.current == "reviews"
        style = "On.Main.Tool.TButton" if on else "Special.Main.Tool.TButton" if n else "Main.Tool.TButton"
        self.nav["reviews"].configure(text=f"待核對 ({n})" if n else "待核對", style=style)

    def _refresh_current(self) -> None:
        page = self.pages.pages.get(self.pages.current or "")
        if self.pages.current == "explore":
            p = self.explore.active
            if p is not None:
                p.refresh()
        elif hasattr(page, "on_show"):
            page.on_show()

    # ---------------------------------------------------------------- 選單
    def view_items(self) -> list:
        view = self.explore.view

        def set_view(v: str) -> None:
            self.show_page("explore")
            self.explore.set_view(v)

        def appearance(mode: str) -> None:
            self.config.settings.appearance = mode
            self.config.save()
            messagebox.showinfo("外觀", "外觀設定會在重新啟動程式後生效。", parent=self.root)

        cur = self.config.settings.appearance
        return [{"label": "單一面板", "check": view == "one", "command": lambda: set_view("one")},
                {"label": "兩個面板", "check": view == "two", "command": lambda: set_view("two")},
                {"label": "四個面板", "check": view == "four", "command": lambda: set_view("four")},
                None,
                {"label": "顯示工作清單", "check": self.tasks_visible(), "command": self.toggle_tasks},
                None,
                {"label": "外觀", "icon": "view", "submenu": [
                    {"label": label, "check": cur == mode, "command": lambda m=mode: appearance(m)}
                    for mode, label in (("auto", "跟隨 Windows"), ("light", "淺色"), ("dark", "深色"))]}]

    def tool_items(self) -> list:
        return [{"label": "遊戲存檔", "icon": "gamepad", "command": lambda: self.show_page("savedata")},
                {"label": "重複檔案", "icon": "duplicates", "command": lambda: self.show_page("dupes")},
                {"label": "紀錄與同步歷史", "icon": "log", "command": lambda: self.show_page("log")},
                None,
                {"label": "115 連線測試…", "icon": "connect", "command": self.check_115},
                {"label": "匯出比對報告（CSV）…", "icon": "export", "command": self.export_csv},
                None,
                {"label": "編輯書籤…", "icon": "star_on", "command": self.edit_bookmarks},
                {"label": "開啟設定資料夾", "icon": "folder", "command": lambda: open_local(str(app_home()))}]

    def help_items(self) -> list:
        return [{"label": "使用說明", "icon": "help", "command": lambda: webbrowser.open(README_URL)},
                {"label": "指令列說明", "icon": "log", "command": self.show_cli_help},
                None,
                {"label": "關於 NAS115Sync", "icon": "info", "command": self.show_about}]

    def show_about(self) -> None:
        messagebox.showinfo(
            "關於 NAS115Sync",
            f"NAS115Sync {__version__}\n\nNAS ⇄ 115 網盤的比對、同步與檔案管理。\n"
            "上傳一律先嘗試 115 秒傳；版面與操作方式比照 Air Explorer。\n\n"
            f"設定與紀錄：{app_home()}", parent=self.root)

    def show_cli_help(self) -> None:
        messagebox.showinfo(
            "指令列說明",
            "同一個資料夾裡的 NAS115Sync-CLI.exe 可以用在排程或批次檔：\n\n"
            "  NAS115Sync-CLI.exe scan          比對（不傳輸）\n"
            "  NAS115Sync-CLI.exe sync --yes    比對並同步\n"
            "  NAS115Sync-CLI.exe watch         即時同步\n"
            "  NAS115Sync-CLI.exe upload <本機路徑> <115 資料夾>\n"
            "  NAS115Sync-CLI.exe download <115 路徑> <本機資料夾>\n"
            "  NAS115Sync-CLI.exe --help        所有指令", parent=self.root)

    # ---------------------------------------------------------------- 工作清單的高度
    def tasks_visible(self) -> bool:
        return str(self.tasks) in self.split.panes()

    def toggle_tasks(self) -> None:
        if self.tasks_visible():
            self.split.forget(self.tasks)
        else:
            self.split.add(self.tasks, weight=0)
            self._tasks_user_sized = False
            self.root.after(50, self._fit_tasks)

    def _fit_tasks(self) -> None:
        """沒有工作時只留標題列；有工作時自動拉高（使用者自己拖過分隔線後就不再調整）。"""
        if self._closed or not self.tasks_visible() or self._tasks_user_sized:
            return
        total = self.split.winfo_height()
        if total <= 1:
            return
        gl = self.tasks.grid_list
        want = gl.head_h + px(30) if not self.tasks.count else min(int(total * 0.36), gl.head_h + px(34)
                                                                   + gl.row_h * min(8, len(gl) + 1))
        try:
            self.split.sashpos(0, max(px(120), total - want))
        except tk.TclError:
            pass

    # ================================================================ 背景工作
    def run_bg(self, title: str, fn, on_done=None, batch: tuple | None = None, mapping_ids=None) -> bool:
        """在背景執行一個工作（一次一個）。batch=(種類, 名稱, 來源, 目的) 時會出現在工作清單。"""
        if self.busy:
            messagebox.showinfo("請稍候", "目前已有工作進行中", parent=self.root)
            return False
        self.busy = True
        self.engine.cancel_event.clear()
        self.set_status(f"{title}…")
        self.cancel_btn.pack(side="right", before=self.progress)
        bid = 0
        if batch:
            bid = self.engine.transfers.begin_batch(batch[0], batch[1], batch[2], batch[3], mapping_ids)
            self.tasks.refresh(force=True)
            self._fit_tasks()
        self.current_batch = bid

        def target() -> None:
            res, done, status, outcome = None, None, f"{title}失敗", "failed"
            try:
                res = fn()
                done, status, outcome = on_done, f"{title}完成", "done"
            except Cancelled:
                status, outcome = f"{title}已取消", "cancelled"
            except Exception as e:  # noqa: BLE001
                log.exception("%s失敗", title)
                status = f"{title}失敗：{e}"
                self.ui.call(messagebox.showerror, f"{title}失敗", str(e))
            finally:
                self.engine.cancel_event.clear()  # 取消只針對這個工作，不影響即時同步
                if bid:
                    self.engine.transfers.end_batch(bid, outcome)
                self.ui.call(self._finish, status, done, res)

        threading.Thread(target=target, daemon=True).start()
        return True

    def _finish(self, status: str, on_done, res) -> None:
        # 先結束「工作中」狀態，on_done 才能接著開始下一個工作（例如比對完重新整理）
        self.busy = False
        self.current_batch = 0
        self._job_progress = None
        self.cancel_btn.pack_forget()
        self.set_status(status)
        self.refresh_all()
        if on_done:
            on_done(res)

    def cancel(self) -> None:
        self.engine.cancel_event.set()
        self.set_status("取消中…")

    def set_status(self, text: str) -> None:
        self.statusbar.configure(text=text.replace("\n", " "))

    def progress_cb(self, stage: str, done: int, total: int, text: str) -> None:
        now = time.monotonic()
        if now - self._last_progress < 0.1 and done != total:
            return
        self._last_progress = now
        self.ui.call(self._show_progress, stage, done, total, text)

    def _show_progress(self, stage: str, done: int, total: int, text: str) -> None:
        if not self.busy:
            return
        self._job_progress = (done, total)
        if total:
            pct = done * 100 // max(1, total)
            info = f"{human_size(done)} / {human_size(total)}" if stage.startswith("計算") else f"{done} / {total}"
            self.set_status(f"{stage} {pct}%（{info}）　{text}")
        else:
            self.set_status(f"{stage}：{done}　{text}")
        if self.sync_window is not None and self.sync_window.winfo_exists():
            self.sync_window.show_progress(stage, done, total, text)

    def _on_engine_event(self, ev: str, data: dict) -> None:
        if ev == "uploaded":
            self.set_status(f"{'秒傳' if data['instant'] else '上傳'}完成：{data['rel_path']}")
        elif ev == "downloaded":
            self.set_status(f"{'從本機複製' if data['copied'] else '下載'}完成：{data['rel_path']}")
        elif ev == "schedule_done":
            self.set_status(f"排程完成：{describe(data['schedule'])}")
            self.scheduler_page.refresh()
        elif ev == "reviews_changed":
            self._reviews_dirty = True
        elif ev == "realtime":
            self._update_realtime()
        elif ev == "savedata_dirty":
            self.set_status(f"偵測到存檔變動，等待遊戲結束／靜置後上傳：{data['group']}")
        elif ev == "savedata_synced":
            self.set_status(f"存檔已同步到 115：{data['group']}")
            self.savedata_page.refresh()
        elif ev == "failed":
            self.set_status(f"傳輸失敗：{data['rel_path']}：{data['error']}")
        if ev in ("uploaded", "downloaded", "failed", "deleted", "deleted_local") and self.sync_window is not None \
                and self.sync_window.winfo_exists():
            self.sync_window.on_event(ev, data)

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            if self._reviews_dirty:
                self._reviews_dirty = False
                self.reviews_page.refresh()
            self._reviews_button()
            self._update_realtime()
        finally:
            self.root.after(3000, self._tick)

    def _tick_fast(self) -> None:
        if self._closed:
            return
        try:
            before = self.tasks.count
            self.tasks.refresh()
            if (before == 0) != (self.tasks.count == 0):
                self._fit_tasks()
            done, total, down, up = self.tasks.totals
            if self._job_progress is not None and self._job_progress[1]:
                done, total = self._job_progress
                self.total_label.configure(text="")
            else:
                self.total_label.configure(text=f"總計：{human_size(done)} / {human_size(total)}" if total else "")
            self.progress.configure(maximum=max(1, total), value=done if total else 0)
            self.down_label.configure(text=f"{human_size(down)}/s" if down else "0 KB/s")
            self.up_label.configure(text=f"{human_size(up)}/s" if up else "0 KB/s")
        finally:
            self.root.after(700, self._tick_fast)

    def refresh_all(self) -> None:
        self.sync_page.refresh()
        self.reviews_page.refresh()
        self.savedata_page.refresh()
        self.scheduler_page.refresh()
        self._reviews_button()
        self._update_realtime()

    def save_ui(self) -> None:
        """稍後把介面狀態存檔（連續變動只存一次）。"""
        if self._save_after is None and not self._closed:
            self._save_after = self.root.after(1500, self._save_ui_now)

    def _save_ui_now(self) -> None:
        self._save_after = None
        try:
            self.config.save()
        except OSError:
            log.debug("無法保存介面狀態", exc_info=True)

    # ================================================================ 帳號相關
    def cloud(self, account_id: str):
        return self.engine.client(account_id)

    def accounts_changed(self) -> None:
        self.explore.accounts_changed()
        self.accounts_page.refresh()
        self.search_page.refresh_accounts()
        self.sync_page.refresh()

    def check_115(self, account_id: str | None = None) -> None:
        aid = account_id or self.accounts_page.selected_id()
        cloud = self.cloud(aid)
        if not self.app.demo and not cloud.is_logged_in():
            messagebox.showwarning("尚未登入", "請先到「帳號」頁用 115 App 掃碼登入", parent=self.root)
            return
        if not messagebox.askyesno(
                "115 連線測試", "會在 115 建立一個「NAS115Sync連線測試」資料夾，實際測試上傳、秒傳、下載、改名，"
                                "最後把整個測試資料夾刪除（進 115 回收站）。不會動到其他檔案。繼續？", parent=self.root):
            return

        def work():
            return run_check(cloud, lambda name, ok, text: self.ui.call(
                self.set_status, f"115 連線測試：{name} {'✔' if ok else '✘'}"))

        def done(results) -> None:
            text = "\n".join(f"{'✔' if ok else '✘'} {name}：{detail}" for name, ok, detail in results)
            if all(ok for _n, ok, _d in results):
                messagebox.showinfo("115 連線測試通過", text, parent=self.root)
            else:
                messagebox.showwarning("115 連線測試有項目失敗", text, parent=self.root)

        self.run_bg("115 連線測試", work, done)

    # ================================================================ 同步
    def ready_to_sync(self, mappings=None) -> bool:
        ms = mappings if mappings is not None else self.config.active_mappings()
        if not ms:
            messagebox.showwarning("沒有已核對的同步", "請到「同步」頁建立同步，並按「核對確認」由本人核對路徑", parent=self.root)
            self.show_page("sync")
            return False
        if not self.app.demo:
            missing = {m.account_id for m in ms if not self.cloud(m.account_id).is_logged_in()}
            if missing:
                names = "、".join(a.label for a in self.config.accounts if a.id in missing)
                messagebox.showwarning("尚未登入", f"請先到「帳號」頁登入 115 帳號：{names}", parent=self.root)
                return False
        return True

    def scan(self, mappings=None, on_done=None) -> None:
        """比對（不傳輸）。mappings=None 表示所有已核對的同步。"""
        if not self.ready_to_sync(mappings):
            return

        def done(plans) -> None:
            self.sync_page.show_plans(plans)
            if on_done:
                on_done(plans)
                return
            n_up = sum(len(p.uploads) for p in plans)
            n_down = sum(len(p.downloads) for p in plans)
            msg = f"比對完成：可自動上傳 {n_up} 個、下載 {n_down} 個檔案，待本人核對 {self.db.count_pending()} 項。"
            warns = [w for p in plans for w in p.warnings]
            if warns:
                msg += "\n\n警告：\n" + "\n".join(warns)
            messagebox.showinfo("比對完成", msg, parent=self.root)

        self.run_bg("比對", lambda: self.engine.analyze(mappings, progress=self.progress_cb), done)

    def execute(self, plans=None, on_done=None) -> None:
        """執行比對結果中可以自動處理的項目（上傳先秒傳）。"""
        plans = plans if plans is not None else self.engine.last_plans
        if not plans:
            messagebox.showinfo("尚未比對", "請先按「比較」", parent=self.root)
            return
        n = sum(len(p.uploads) + len(p.downloads) + len(p.deletes) + len(p.local_deletes) + len(p.mkdirs)
                + len(p.local_mkdirs) for p in plans)
        if n and not messagebox.askyesno("執行同步", f"將執行 {n} 項自動同步（上傳時優先嘗試秒傳）。繼續？", parent=self.root):
            return

        def done(stats) -> None:
            self.engine.last_plans = []
            self.sync_page.forget_plans([p.mapping_id for p in plans])
            if on_done:
                on_done(stats)
            messagebox.showinfo("同步完成",
                                f"上傳 {stats['uploaded']} 個（其中秒傳 {stats['instant']} 個）\n"
                                f"下載 {stats['downloaded']} 個（其中從本機複製 {stats['copied']} 個）\n"
                                f"失敗 {stats['failed']} 個（已列入待核對）\n"
                                f"刪除 115 {stats['deleted']} 個／NAS {stats['local_deleted']} 個（可復原）\n"
                                f"建立資料夾 {stats['mkdirs']} 個", parent=self.root)

        ids = [p.mapping_id for p in plans]
        names = "、".join(m.title for m in (self.config.get_mapping(i) for i in ids) if m)
        self.run_bg("同步", lambda: self.engine.execute(plans, progress=self.progress_cb), done,
                    batch=("sync", "同步", names, ""), mapping_ids=ids)

    def export_csv(self) -> None:
        plans = self.engine.last_plans
        if not plans:
            messagebox.showinfo("尚未比對", "請先到「同步」頁按「全部比較」", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="匯出比對報告", defaultextension=".csv",
                                            initialfile=time.strftime("比對報告_%Y%m%d-%H%M.csv"),
                                            filetypes=[("CSV（Excel 可開）", "*.csv")])
        if path:
            n = export_plans_csv(self.engine, plans, path)
            self.set_status(f"已匯出 {n} 筆到 {path}")

    def toggle_realtime(self) -> None:
        if self.rt.running:
            self.set_status("停止即時同步中…")
            threading.Thread(target=lambda: (self.rt.stop(), self.ui.call(self._update_realtime)),
                             daemon=True).start()
            return
        if not self.ready_to_sync():
            return
        try:
            self.rt.start()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("無法啟動即時同步", str(e), parent=self.root)
        self._update_realtime()

    def _update_realtime(self) -> None:
        on = self.rt.running
        pending = self.rt.pending_count() if on else 0
        self.rt_btn.configure(text="即時同步：開" + (f"（待處理 {pending}）" if pending else "") if on else "即時同步：關",
                              image=theme.icon_pair("dot_on" if on else "dot_off"))
        self._rt_tip.text = "按一下停止即時同步" if on else "按一下開始即時同步（NAS 與 115 有變動時自動同步）"
        self.sync_page.update_realtime(on)

    def new_sync(self, local_path: str = "", account_id: str = "default", cloud_path: str = "") -> None:
        self.show_page("sync")
        self.sync_page.add(local_path, account_id, cloud_path)

    def open_sync_window(self, mapping) -> None:
        if self.sync_window is not None and self.sync_window.winfo_exists():
            if self.sync_window.mapping.id == mapping.id:
                self.sync_window.lift()
                return
            self.sync_window.destroy()
        self.sync_window = dialogs.SyncWindow(self, mapping)

    # ================================================================ 對話框
    def show_options(self) -> None:
        if dialogs.OptionsDialog(self).modal():
            self.set_status("設定已儲存")

    def edit_bookmarks(self) -> None:
        dialogs.BookmarksDialog(self).modal()
        self.explore.each_panel(lambda p: p._update_star())

    def show_properties(self, panel, item) -> None:
        dialogs.PropertiesDialog(self, panel, item).modal()

    def pick_cloud_folder(self, account_id: str, title: str = "選擇 115 資料夾", start: str = "/") -> str | None:
        return dialogs.CloudFolderDialog(self, account_id, title, start).modal()

    def open_cloud_path(self, account_id: str, cid: str) -> None:
        open_in_115_browser(self.cloud(account_id).web_url(cid), self.config.settings.browser_path)

    # ================================================================ 關閉
    def _on_close(self) -> None:
        if (self.rt.running or self.busy) and not messagebox.askyesno(
                "結束程式", "即時同步或工作仍在進行，結束後將停止同步。確定結束？", parent=self.root):
            return
        self._closed = True
        try:
            if self.root.state() == "normal":
                self.config.settings.ui["geometry"] = f"{self.root.winfo_width()}x{self.root.winfo_height()}"
            self.config.save()
        except (tk.TclError, OSError):
            pass
        self.engine.cancel_event.set()
        self.scheduler.stop()
        if self.rt.running:
            self.rt.stop()
        logging.getLogger().removeHandler(self._log_handler)
        self.root.destroy()


def apply_window_icon(root: tk.Tk) -> None:
    icon = resource_path("assets/app.ico")
    if sys.platform == "win32" and os.path.exists(icon):
        try:
            root.iconbitmap(default=icon)
        except tk.TclError:
            pass


def setup_fonts(root: tk.Tk) -> None:
    """（相容舊介面的呼叫）字型由 theme.build 設定。"""


def run_gui(app: App, start_realtime: bool = False, minimized: bool = False) -> None:
    enable_dpi_awareness()
    if not single_instance("NAS115Sync_demo" if app.demo else "NAS115Sync"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("NAS115Sync", "NAS115Sync 已在執行中（請查看工作列）。")
        root.destroy()
        return
    root = tk.Tk()
    apply_window_icon(root)
    MainWindow(root, app, start_realtime=start_realtime)
    if minimized:
        root.iconify()
    root.mainloop()
