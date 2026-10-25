"""NAS115Sync 圖形介面（Tkinter）：Apple 設計美學（側邊欄、圓角卡片、大標題、留白），比例依黃金分割。"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import __version__, autostart
from ..app import App, RedactSecrets, make_client, open_in_115_browser, resource_path, single_instance
from ..check115 import run_check
from ..compare import DECISIONS, KIND_LABELS, SAVEDATA_GROUP, available_decisions
from ..config import DIRECTION_LABELS, Settings
from ..engine import Cancelled
from ..manual import export_plans_csv
from ..paths import local_join, rel_parent
from ..scheduler import WEEKDAYS, Scheduler, describe, new_schedule
from ..watcher import RealtimeSync
from . import theme
from .browser import BrowserTab
from .dialogs import AddMappingDialog, ConfirmMappingDialog, QrLoginDialog, SuggestDialog
from .sidebar import Sidebar
from .tabs import DuplicatesTab, TransfersTab
from .theme import MAJOR, MINOR, XL, XS, XXL, XXS, L, M, S, px
from .util import (AutoScrollbar, Flow, QueueLogHandler, UiQueue, enable_dpi_awareness, fmt_time, human_size,
                   open_local, scrolled_text, set_text, setup_fonts, tree_with_scroll, wrap_label)

log = logging.getLogger(__name__)

GROUP_STATUS = {"pending": "待核對", "approved": "自動同步（Steam 雲存檔式）", "normal": "一般檔案", "exclude": "不同步"}
DANGEROUS = {"delete_cloud", "delete_all", "delete_extra", "upload_replace", "download_replace", "delete_local"}

SETTING_FIELDS = [
    ("auto_update_linked", "已核對連結的檔案：NAS 修改後自動上傳新版並刪除 115 舊版", "bool"),
    ("deletion_policy", "一邊刪除檔案時（各對應可另外設定）", "choice",
     [("review", "交給本人核對"), ("auto", "自動同步刪除（115 進回收站／NAS 移到可復原資料夾）"), ("ignore", "不處理")]),
    ("review_same_name_elsewhere", "新檔案與 115 其他資料夾同檔名時要本人核對", "bool"),
    ("mass_delete_guard_min", "大量刪除保護：一次至少幾個檔案", "int"),
    ("mass_delete_guard_ratio", "大量刪除保護：占已同步檔案比例（0~1）", "float"),
    ("savedata_dir_names", "存檔資料夾名稱（逗號分隔）", "list"),
    ("savedata_extensions", "存檔副檔名（逗號分隔）", "list"),
    ("savedata_quiet_seconds", "存檔最後一次變動後靜置幾秒才上傳", "int"),
    ("savedata_keep_versions", "115 保留存檔舊版份數（0 = 上傳後刪除舊版）", "int"),
    ("savedata_mirror_deletes", "NAS 刪除存檔時同步刪除 115 上的存檔", "bool"),
    ("stable_seconds", "一般檔案停止變動幾秒後才上傳", "float"),
    ("rescan_interval_minutes", "定期完整比對間隔（分鐘，補抓網路磁碟漏掉的事件）", "int"),
    ("use_polling_observer", "改用輪詢監看（NAS 不回報變動事件時勾選）", "bool"),
    ("polling_interval_seconds", "輪詢間隔（秒）", "int"),
    ("instant_first", "先對全部檔案嘗試秒傳，再實際上傳其餘檔案", "bool"),
    ("verify_uploads", "實際上傳後向 115 讀回 SHA-1 驗證", "bool"),
    ("keep_versions", "一般檔案：115 舊版保留份數（0 = 上傳新版後刪除舊版）", "int"),
    ("local_trash_dir", "NAS 端被覆蓋／刪除的檔案先移到此資料夾（在各對應根目錄下）", "str"),
    ("local_trash_days", "NAS 可復原資料夾保留天數", "int"),
    ("savedata_cloud_poll_seconds", "雙向/下載模式：多久檢查一次 115 上的新存檔（秒）", "int"),
    ("upload_limit_kb", "上傳限速 KB/s（0 = 不限）", "int"),
    ("download_limit_kb", "下載限速 KB/s（0 = 不限）", "int"),
    ("proxy", "代理伺服器（例如 http://127.0.0.1:7890，空白 = 不使用）", "str"),
    ("upload_workers", "同時上傳數", "int"),
    ("hash_workers", "同時計算 MD5/SHA-1 數", "int"),
    ("api_min_interval", "115 API 最小呼叫間隔（秒，避免被限流）", "float"),
    ("cloud_poll_seconds", "即時同步：多久檢查一次 115 端（115 電腦版、App）的變動（秒）", "int"),
    ("part_size_mb", "大檔分片大小（MB）", "int"),
    ("browser_path", "115 瀏覽器執行檔（空白 = 自動尋找）", "str"),
    ("ignore_patterns", "忽略的檔名規則（每行一個，支援 * ?）", "lines"),
    ("appearance", "外觀（重新啟動程式後生效）", "choice",
     [("auto", "跟隨 Windows"), ("light", "淺色"), ("dark", "深色")]),
]
# 設定頁分組（像 macOS 系統設定的分組卡片）
SETTING_GROUPS = [
    ("同步與核對", ["auto_update_linked", "deletion_policy", "review_same_name_elsewhere", "mass_delete_guard_min",
                "mass_delete_guard_ratio", "instant_first", "verify_uploads", "keep_versions"]),
    ("即時同步", ["stable_seconds", "cloud_poll_seconds", "rescan_interval_minutes", "use_polling_observer",
              "polling_interval_seconds"]),
    ("遊戲存檔", ["savedata_dir_names", "savedata_extensions", "savedata_quiet_seconds", "savedata_keep_versions",
              "savedata_mirror_deletes", "savedata_cloud_poll_seconds"]),
    ("NAS 端", ["local_trash_dir", "local_trash_days", "ignore_patterns"]),
    ("傳輸與網路", ["upload_limit_kb", "download_limit_kb", "upload_workers", "hash_workers", "part_size_mb",
               "api_min_interval", "proxy", "browser_path"]),
    ("外觀", ["appearance"]),
]


class MainWindow:
    def __init__(self, root: tk.Tk, app: App, start_realtime: bool = False):
        self.root, self.app = root, app
        self.engine, self.db, self.config = app.engine, app.db, app.config
        self.ui = UiQueue(root)
        self.rt = RealtimeSync(self.engine)
        self.scheduler = Scheduler(self.engine)
        self.busy = False
        self._last_progress = 0.0
        self._reviews_dirty = False
        self.review_items: dict[str, object] = {}
        self.group_items: dict[str, tuple] = {}

        root.title(f"NAS115Sync {__version__}" + ("（示範模式）" if app.demo else ""))
        theme.build(root, getattr(self.config.settings, "appearance", "auto"))
        # 1294×800（長寬比 φ），螢幕較小（例如 1366×768 筆電）時縮小到放得下，並置中
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight() - px(XXL)  # 扣掉工作列
        w, h = min(px(theme.WINDOW[0]), sw - px(L)), min(px(theme.WINDOW[1]), sh - px(L))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(px(theme.MIN_WINDOW[0]), w), min(px(theme.MIN_WINDOW[1]), h))

        shell = ttk.Frame(root, style="Page.TFrame")
        shell.pack(fill="both", expand=True)
        self.sidebar = Sidebar(shell, self._nav, subtitle="NAS ⇄ 115 網盤" + ("・示範模式" if app.demo else ""))
        self.sidebar.pack(side="left", fill="y")
        main = ttk.Frame(shell, style="Page.TFrame")
        main.pack(side="left", fill="both", expand=True)
        self.nb = ttk.Notebook(main, style="Hidden.TNotebook")
        self.nb.pack(fill="both", expand=True)
        self._build_dashboard()
        self.tab_browser = self._new_tab("瀏覽", "左邊 NAS、右邊 115：上傳、下載、比較、整理檔案", "folder")
        self.browser = BrowserTab(self, self.tab_browser.body)
        self.tab_transfers = self._new_tab("傳輸", "每個檔案的進度與速度；可暫停、取消、限速", "transfer")
        self.transfers_tab = TransfersTab(self, self.tab_transfers.body)
        self._build_mappings()
        self._build_reviews()
        self._build_savedata()
        self.tab_dupes = self._new_tab("重複檔案", "依 SHA-1 找出 NAS 與 115 上內容相同的檔案", "copy")
        self.dupes_tab = DuplicatesTab(self, self.tab_dupes.body)
        self._build_log()
        self._build_settings()
        self.statusbar = ttk.Label(main, text="就緒", anchor="w", style="Status.TLabel",
                                   padding=(px(XL), px(XS), px(XL), px(S)))
        self.statusbar.pack(fill="x")
        self.sidebar.select(self.nb.select())
        theme.apply_titlebar(root)

        self._log_handler = QueueLogHandler(self.ui, self._append_log)
        self._log_handler.addFilter(RedactSecrets())
        logging.getLogger().addHandler(self._log_handler)
        self.engine.listeners.append(lambda ev, data: self.ui.call(self._on_engine_event, ev, data))
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.refresh_all()
        self._refresh_account()
        self.browser.refresh_accounts()
        active = self.config.active_mappings()
        self.browser.set_initial_local(active[0].local_path if active else "")
        self._tick()
        self._tick_fast()
        self.scheduler.start()
        if start_realtime:
            root.after(1500, self.toggle_realtime)

    def _new_tab(self, title: str, subtitle: str, icon: str, card: bool = True) -> ttk.Frame:
        """一個頁面：大標題＋說明，內容放在 page.body（白色圓角卡片）。"""
        page = ttk.Frame(self.nb, style="Page.TFrame", padding=(px(XL), px(L), px(XL), 0))
        page.header = ttk.Frame(page, style="Page.TFrame")  # type: ignore[attr-defined]
        page.header.pack(fill="x", pady=(0, px(M)))
        ttk.Label(page.header, text=title, style="PageTitle.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(page.header, text=subtitle, style="PageSubtitle.TLabel").pack(anchor="w", pady=(px(XXS), 0))
        if card:
            page.body = theme.card(page)  # type: ignore[attr-defined]
            page.body.pack(fill="both", expand=True)
        self.nb.add(page, text=title)
        self.sidebar.add(str(page), title, icon)
        return page

    def _nav(self, key: str) -> None:
        self.nb.select(key)

    # 提供給其他分頁使用
    def set_status(self, text: str) -> None:
        self._set_status(text)

    def show_transfers(self) -> None:
        self.nb.select(self.tab_transfers)

    def ready_to_sync(self) -> bool:
        return self._ready_to_sync()

    # ================================================================ 背景工作
    def run_bg(self, title: str, fn, on_done=None) -> None:
        if self.busy:
            messagebox.showinfo("請稍候", "目前已有工作進行中", parent=self.root)
            return
        self.busy = True
        self.engine.cancel_event.clear()
        self._set_status(f"{title}…")
        self.cancel_btn.configure(state="normal")

        def target():
            res, done, status = None, None, f"{title}失敗"
            try:
                res = fn()
                done, status = on_done, f"{title}完成"
            except Cancelled:
                status = f"{title}已取消"
            except Exception as e:  # noqa: BLE001
                log.exception("%s失敗", title)
                status = f"{title}失敗：{e}"
                self.ui.call(messagebox.showerror, f"{title}失敗", str(e))
            finally:
                self.engine.cancel_event.clear()  # 取消只針對這個工作，不影響即時同步
                self.ui.call(self._finish, status, done, res)

        threading.Thread(target=target, daemon=True).start()

    def _finish(self, status: str, on_done, res) -> None:
        # 先結束「工作中」狀態，on_done 才能接著開始下一個工作（例如比對完重新整理）
        self._idle()
        self._set_status(status)
        if on_done:
            on_done(res)

    def _idle(self) -> None:
        self.busy = False
        self.cancel_btn.configure(state="disabled")
        self.progress.configure(value=0)
        self.refresh_all()

    def cancel(self) -> None:
        self.engine.cancel_event.set()
        self._set_status("取消中…")

    def _set_status(self, text: str) -> None:
        self.statusbar.configure(text=text)

    def progress_cb(self, stage: str, done: int, total: int, text: str) -> None:
        now = time.monotonic()
        if now - self._last_progress < 0.1 and done != total:
            return
        self._last_progress = now
        self.ui.call(self._show_progress, stage, done, total, text)

    def _show_progress(self, stage, done, total, text) -> None:
        if total:
            self.progress.configure(mode="determinate", maximum=total, value=done)
            pct = done * 100 // max(1, total)
            size_info = f"（{human_size(done)} / {human_size(total)}）" if stage.startswith("計算") else \
                f"（{done}/{total}）"
            self.stage_label.configure(text=f"{stage} {pct}% {size_info}")
        else:
            self.progress.configure(mode="determinate", maximum=100, value=(done % 100))
            self.stage_label.configure(text=f"{stage}：{done}")
        self.file_label.configure(text=text)

    def _on_engine_event(self, ev: str, data: dict) -> None:
        if ev == "file_progress":
            now = time.monotonic()
            if now - self._last_progress >= 0.2 or data["done"] == data["total"]:
                self._last_progress = now
                pct = data["done"] * 100 // max(1, data["total"])
                self.file_label.configure(text=f"上傳中 {data['rel_path']}  {pct}%  "
                                               f"{human_size(data['done'])}/{human_size(data['total'])}")
        elif ev == "uploaded":
            how = "秒傳" if data["instant"] else "上傳"
            self._set_status(f"{how}完成：{data['rel_path']}")
        elif ev == "downloaded":
            self._set_status(f"{'從 NAS 複製' if data['copied'] else '下載'}完成：{data['rel_path']}")
        elif ev == "schedule_done":
            self._set_status(f"排程完成：{describe(data['schedule'])}")
        elif ev == "reviews_changed":
            self._reviews_dirty = True
        elif ev == "plans":
            self._show_plans(data["plans"])
        elif ev == "realtime":
            self._update_rt_button()
        elif ev == "savedata_dirty":
            self._set_status(f"偵測到存檔變動，等待遊戲結束／靜置後上傳：{data['group']}")
        elif ev == "savedata_synced":
            self._set_status(f"存檔已同步到 115：{data['group']}")
            self._refresh_savedata()
        elif ev == "failed":
            self._set_status(f"傳輸失敗：{data['rel_path']}：{data['error']}")

    def _tick(self) -> None:
        try:
            if self._reviews_dirty:
                self._reviews_dirty = False
                self._refresh_reviews()
            self._refresh_stats()
        finally:
            self.root.after(3000, self._tick)

    def _tick_fast(self) -> None:
        try:
            if self.nb.select() == str(self.tab_transfers):
                self.transfers_tab.refresh()
        finally:
            self.root.after(1000, self._tick_fast)

    def refresh_all(self) -> None:
        self._refresh_mappings()
        self._refresh_reviews()
        self._refresh_savedata()
        self._refresh_stats()
        self._update_rt_button()

    # ================================================================ 總覽
    def _build_dashboard(self) -> None:
        page = self.tab_dashboard = self._new_tab("總覽", "NAS 與 115 網盤的比對與即時同步", "home", card=False)
        grid = ttk.Frame(page, style="Page.TFrame")
        grid.pack(fill="both", expand=True)
        # 黃金分割：左欄 : 右欄 = 0.382 : 0.618
        grid.columnconfigure(0, weight=round(MINOR * 1000), uniform="golden")
        grid.columnconfigure(1, weight=round(MAJOR * 1000), uniform="golden")
        grid.rowconfigure(1, weight=1)
        gap = px(M)

        acct = theme.card(grid)
        acct.grid(row=0, column=0, sticky="nsew", padx=(0, gap), pady=(0, gap))
        ttk.Label(acct, text="115 帳號", style="Section.TLabel").pack(anchor="w")
        row = ttk.Frame(acct)
        row.pack(fill="x", pady=(px(S), 0))
        self.acct_combo = ttk.Combobox(row, state="readonly", width=14, style="Popup.TCombobox")
        self.acct_combo.pack(side="left", fill="x", expand=True)
        self.acct_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_account())
        ttk.Button(row, text=theme.icon("add"), style="Icon.TButton", command=self.add_account).pack(
            side="left", padx=(px(S), 0))
        ttk.Button(row, text=theme.icon("remove"), style="Icon.TButton", command=self.remove_account).pack(
            side="left", padx=(px(XS), 0))
        self.acct_label = ttk.Label(acct, text="檢查中…", style="Secondary.TLabel")
        self.acct_label.pack(anchor="w", pady=(px(S), px(S)))
        bar = Flow(acct)
        bar.pack(fill="x")
        self.btn_qr = ttk.Button(bar, text="115 App 掃碼登入", style="Accent.TButton", command=self.login_qr)
        self.btn_check = ttk.Button(bar, text="115 連線測試", command=self.check_115)
        self.btn_logout = ttk.Button(bar, text="登出", command=self.logout)
        bar.add(self.btn_qr, self.btn_check, self.btn_logout)
        if self.app.demo:
            for b in (self.btn_logout, self.btn_qr):
                b.configure(state="disabled")
        self._fill_accounts()

        act = theme.card(grid)
        act.grid(row=0, column=1, sticky="nsew", pady=(0, gap))
        ttk.Label(act, text="同步", style="Section.TLabel").pack(anchor="w")
        wrap_label(act, "掃描比對：讀取 NAS 與 115、計算 MD5/SHA-1 並三方比對（不傳輸）。"
                        "執行同步：處理可自動完成的項目（上傳秒傳優先），需要判斷的交給「待核對」。").pack(
            fill="x", pady=(px(XXS), px(M)))
        row = Flow(act)
        row.pack(fill="x")
        self.btn_scan = ttk.Button(row, text="掃描比對", style="Large.TButton", command=self.scan)
        self.btn_sync = ttk.Button(row, text="執行同步", style="Large.Accent.TButton", command=self.execute)
        self.rt_btn = ttk.Button(row, text="開始即時同步", style="Large.TButton", command=self.toggle_realtime)
        self.cancel_btn = ttk.Button(row, text="取消目前工作", command=self.cancel, state="disabled",
                                     style="Link.TButton")
        self.btn_csv = ttk.Button(row, text="匯出比對報告（CSV）", style="Link.TButton", command=self.export_csv)
        row.add(self.btn_scan, self.btn_sync, self.rt_btn, self.cancel_btn, self.btn_csv)
        self.progress = ttk.Progressbar(act, mode="determinate")
        self.progress.pack(fill="x", pady=(px(M), px(XS)))
        self.stage_label = ttk.Label(act, text="", style="Secondary.TLabel")
        self.stage_label.pack(anchor="w")
        self.file_label = ttk.Label(act, text="", style="Caption.TLabel")
        self.file_label.pack(anchor="w")

        stats = theme.card(grid)
        stats.grid(row=1, column=0, sticky="nsew", padx=(0, gap))
        head = ttk.Frame(stats)
        head.pack(fill="x")
        ttk.Label(head, text="狀態", style="Section.TLabel").pack(side="left")
        ttk.Button(head, text="前往待核對 →", style="Link.TButton",
                   command=lambda: self.nb.select(self.tab_reviews)).pack(side="right")
        # 先放底部的統計文字：視窗較矮時被壓縮的是上方的數字區，而不是這行
        self.stats_label = ttk.Label(stats, text="", style="Caption.TLabel", justify="left")
        self.stats_label.pack(anchor="w", side="bottom")
        tiles = ttk.Frame(stats)
        tiles.pack(fill="both", expand=True, pady=(px(M), 0))
        tiles.columnconfigure((0, 1), weight=1, uniform="tile")
        self.metrics: dict[str, tuple[ttk.Label, ttk.Label]] = {}
        for i, (key, caption) in enumerate((("mappings", "路徑對應已核對"), ("reviews", "待核對"),
                                            ("instant", "秒傳次數"), ("realtime", "即時同步"))):
            tile = ttk.Frame(tiles)
            tile.grid(row=i // 2, column=i % 2, sticky="nw", pady=(0, px(L)))
            value = ttk.Label(tile, text="—", style="Metric.TLabel")
            value.pack(anchor="w")
            cap = ttk.Label(tile, text=caption, style="Caption.TLabel")
            cap.pack(anchor="w")
            self.metrics[key] = (value, cap)

        plan = theme.card(grid)
        plan.grid(row=1, column=1, sticky="nsew")
        ttk.Label(plan, text="最近一次比對結果", style="Section.TLabel").pack(anchor="w", pady=(0, px(S)))
        self.plan_tree = tree_with_scroll(plan, [
            ("map", "對應（NAS ⇄ 115）", 260), ("mode", "方向", 70), ("up", "待上傳", 60), ("down", "待下載", 60),
            ("bytes", "傳輸量", 80), ("same", "已相同", 60), ("rev", "待核對", 60), ("only", "僅一邊有", 70),
            ("del", "刪除", 50), ("warn", "警告", 220)], height=6)
        self.plan_tree.frame.pack(fill="both", expand=True)

    def _fill_accounts(self) -> None:
        labels = [a.label for a in self.config.accounts]
        cur = self.acct_combo.get()
        self.acct_combo.configure(values=labels)
        self.acct_combo.set(cur if cur in labels else labels[0])

    @property
    def account_id(self) -> str:
        labels = [a.label for a in self.config.accounts]
        cur = self.acct_combo.get()
        return self.config.accounts[labels.index(cur)].id if cur in labels else "default"

    @property
    def cur_cloud(self):
        return self.engine.client(self.account_id)

    def add_account(self) -> None:
        label = simpledialog.askstring("新增 115 帳號", "帳號名稱（自訂，例如「家人帳號」）：", parent=self.root)
        if not label:
            return
        a = self.config.add_account(label.strip())
        self.engine.clouds[a.id] = make_client(self.config, a.id, self.app.demo)
        self._fill_accounts()
        self.acct_combo.set(a.label)
        self.browser.refresh_accounts()
        self._refresh_account()

    def remove_account(self) -> None:
        aid = self.account_id
        if aid == "default":
            messagebox.showinfo("無法刪除", "預設帳號不能刪除", parent=self.root)
            return
        if not messagebox.askyesno("刪除帳號", f"刪除帳號「{self.acct_combo.get()}」？", parent=self.root):
            return
        try:
            self.config.remove_account(aid)
        except ValueError as e:
            messagebox.showerror("無法刪除", str(e), parent=self.root)
            return
        self.engine.clouds.pop(aid, None)
        self._fill_accounts()
        self.browser.refresh_accounts()
        self._refresh_account()

    def _refresh_account(self) -> None:
        if self.app.demo:
            self.acct_label.configure(text="示範模式（模擬 115，不會連線）", style="Secondary.TLabel")
            return
        cloud = self.cur_cloud
        if not cloud.is_logged_in():
            self.acct_label.configure(text="尚未登入 115", style="Danger.TLabel")
            return

        def work():
            try:
                name = cloud.user_label()
                self.ui.call(lambda: self.acct_label.configure(text=f"已登入：{name}", style="Success.TLabel"))
            except Exception as e:  # noqa: BLE001
                msg = f"登入失效：{e}"
                self.ui.call(lambda: self.acct_label.configure(text=msg, style="Danger.TLabel"))

        threading.Thread(target=work, daemon=True).start()

    def login_qr(self) -> None:
        if QrLoginDialog(self.root, self.cur_cloud, self.ui).modal():
            messagebox.showinfo("登入成功", "已登入 115。建議按「115 連線測試」確認上傳、秒傳、下載都正常。",
                                parent=self.root)
        self._refresh_account()

    def check_115(self) -> None:
        cloud = self.cur_cloud
        if not self.app.demo and not cloud.is_logged_in():
            messagebox.showwarning("尚未登入", "請先按「115 App 掃碼登入」", parent=self.root)
            return
        if not messagebox.askyesno(
                "115 連線測試", "會在 115 建立一個「NAS115Sync連線測試」資料夾，實際測試上傳、秒傳、下載、改名，"
                                "最後把整個測試資料夾刪除（進 115 回收站）。不會動到其他檔案。繼續？", parent=self.root):
            return

        def work():
            return run_check(cloud, lambda name, ok, text: self.ui.call(
                self._set_status, f"115 連線測試：{name} {'✔' if ok else '✘'}"))

        def done(results):
            text = "\n".join(f"{'✔' if ok else '✘'} {name}：{detail}" for name, ok, detail in results)
            if all(ok for _n, ok, _d in results):
                messagebox.showinfo("115 連線測試通過", text, parent=self.root)
            else:
                messagebox.showwarning("115 連線測試有項目失敗", text, parent=self.root)

        self.run_bg("115 連線測試", work, done)

    def logout(self) -> None:
        if messagebox.askyesno("登出", "確定要登出 115？（即時同步會停止；本機保存的登入資訊會刪除）\n"
                                     "也可以在 115 App 的「登入裝置管理」移除此裝置。", parent=self.root):
            if self.rt.running:
                self.rt.stop()
            self.cur_cloud.logout()
            self._refresh_account()

    def _refresh_stats(self) -> None:
        ms = self.config.mappings
        st = self.db.upload_stats()
        n = self.db.count_pending()
        running = self.rt.running

        def metric(key, value, caption=None, style="Metric.TLabel"):
            v, c = self.metrics[key]
            v.configure(text=value, style=style)
            if caption is not None:
                c.configure(text=caption)

        metric("mappings", f"{sum(m.confirmed for m in ms)} / {len(ms)}")
        metric("reviews", str(n), "待核對" if n else "待核對（全部處理完畢）")
        metric("instant", str(st.get("instant_n", 0)), f"秒傳次數・省下 {human_size(st.get('instant_bytes', 0))}")
        metric("realtime", "執行中" if running else "未啟動",
               f"即時同步・待處理 {self.rt.pending_count()} 項" if running else "即時同步")
        self.stats_label.configure(
            text=f"實際上傳 {st.get('real_n', 0)} 次（{human_size(st.get('real_bytes', 0))}）・"
                 f"下載 {st.get('down_n', 0)} 次（其中從 NAS 複製 {st.get('copy_n', 0)} 次）")
        self.nb.tab(self.tab_reviews, text=f"待核對（{n}）" if n else "待核對")
        self.sidebar.set_badge(str(self.tab_reviews), str(n) if n else "")

    def _show_plans(self, plans) -> None:
        self.plan_tree.delete(*self.plan_tree.get_children())
        for p in plans:
            m = self.config.get_mapping(p.mapping_id)
            s = p.summary()
            label = f"{m.local_path} ⇄ {m.cloud_path}" if m else p.mapping_id
            mode = DIRECTION_LABELS.get(m.direction, "") if m else ""
            self.plan_tree.insert("", "end", values=(
                label, mode.split("（")[0], s["uploads"], s["downloads"],
                human_size(s["upload_bytes"] + s["download_bytes"]), s["identical"], s["reviews"],
                s["cloud_only"] + s["local_only"], s["deletes"] + s["local_deletes"], "；".join(p.warnings)))

    def export_csv(self) -> None:
        plans = self.engine.last_plans
        if not plans:
            messagebox.showinfo("尚未比對", "請先按「① 掃描比對」", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="匯出比對報告", defaultextension=".csv",
                                            initialfile=time.strftime("比對報告_%Y%m%d-%H%M.csv"),
                                            filetypes=[("CSV（Excel 可開）", "*.csv")])
        if path:
            n = export_plans_csv(self.engine, plans, path)
            self._set_status(f"已匯出 {n} 筆到 {path}")

    def scan(self) -> None:
        if not self._ready_to_sync():
            return
        self.run_bg("掃描比對", lambda: self.engine.analyze(progress=self.progress_cb),
                    lambda plans: self._after_scan(plans))

    def _after_scan(self, plans) -> None:
        self._show_plans(plans)
        n_up = sum(len(p.uploads) for p in plans)
        n_down = sum(len(p.downloads) for p in plans)
        msg = (f"比對完成：可自動上傳 {n_up} 個、下載 {n_down} 個檔案，"
               f"待本人核對 {self.db.count_pending()} 項。")
        warns = [w for p in plans for w in p.warnings]
        if warns:
            msg += "\n\n警告：\n" + "\n".join(warns)
        messagebox.showinfo("比對完成", msg, parent=self.root)

    def execute(self) -> None:
        plans = self.engine.last_plans
        if not plans:
            messagebox.showinfo("尚未比對", "請先按「① 掃描比對」", parent=self.root)
            return
        n = sum(len(p.uploads) + len(p.downloads) + len(p.deletes) + len(p.local_deletes) + len(p.mkdirs)
                + len(p.local_mkdirs) for p in plans)
        if n and not messagebox.askyesno("執行同步", f"將執行 {n} 項自動同步（上傳時優先嘗試秒傳）。繼續？",
                                         parent=self.root):
            return

        def done(stats):
            self.engine.last_plans = []
            messagebox.showinfo("同步完成",
                                f"上傳 {stats['uploaded']} 個（其中秒傳 {stats['instant']} 個）\n"
                                f"下載 {stats['downloaded']} 個（其中從 NAS 複製 {stats['copied']} 個）\n"
                                f"失敗 {stats['failed']} 個（已列入待核對）\n"
                                f"刪除 115 {stats['deleted']} 個／NAS {stats['local_deleted']} 個（可復原）\n"
                                f"建立資料夾 {stats['mkdirs']} 個", parent=self.root)

        self.run_bg("同步", lambda: self.engine.execute(plans, progress=self.progress_cb), done)

    def toggle_realtime(self) -> None:
        if self.rt.running:
            self._set_status("停止即時同步中…")
            threading.Thread(target=lambda: (self.rt.stop(), self.ui.call(self._update_rt_button)),
                             daemon=True).start()
            return
        if not self._ready_to_sync():
            return
        try:
            self.rt.start()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("無法啟動即時同步", str(e), parent=self.root)
        self._update_rt_button()

    def _update_rt_button(self) -> None:
        on = self.rt.running
        self.rt_btn.configure(text="停止即時同步" if on else "開始即時同步")
        self.sidebar.set_status("即時同步中" if on else "即時同步未啟動", on)

    def _ready_to_sync(self) -> bool:
        ms = self.config.active_mappings()
        if not ms:
            messagebox.showwarning("沒有已核對的路徑", "請到「路徑對應」新增對應並按「核對確認」", parent=self.root)
            self.nb.select(self.tab_mappings)
            return False
        if not self.app.demo:
            missing = {m.account_id for m in ms if not self.engine.client(m.account_id).is_logged_in()}
            if missing:
                names = "、".join(a.label for a in self.config.accounts if a.id in missing)
                messagebox.showwarning("尚未登入", f"請先登入 115 帳號：{names}", parent=self.root)
                return False
        return True

    # ================================================================ 路徑對應
    def _build_mappings(self) -> None:
        page = self.tab_mappings = self._new_tab(
            "路徑對應", "只有「已核對」的對應才會比對與同步；新增或自動建議的對應都必須由本人按「核對確認」", "link")
        f = page.body
        bar = Flow(f)
        bar.pack(fill="x", pady=(0, px(M)))
        buttons = [ttk.Button(bar, text="新增…", style="Accent.TButton", command=self.add_mapping),
                   ttk.Button(bar, text="自動建議（NAS 所有資料夾）…", command=self.suggest_mappings),
                   ttk.Button(bar, text="核對確認…", command=self.confirm_mapping),
                   ttk.Button(bar, text="編輯選項…", command=self.edit_mapping),
                   ttk.Button(bar, text="取消核對", command=self.unconfirm_mapping),
                   ttk.Button(bar, text="啟用/停用", command=self.toggle_mapping),
                   ttk.Button(bar, text="開啟 NAS 資料夾", command=self.open_mapping_local),
                   ttk.Button(bar, text="在 115 瀏覽器開啟", command=self.open_mapping_cloud),
                   ttk.Button(bar, text="刪除", style="Destructive.TButton", command=self.remove_mapping)]
        bar.add(*buttons)
        self.map_tree = tree_with_scroll(f, [("state", "狀態", 80), ("local", "NAS 資料夾", 300),
                                             ("cloud", "115 資料夾", 230), ("mode", "同步方向", 140),
                                             ("acct", "帳號", 90), ("type", "類型", 70), ("filter", "篩選", 120),
                                             ("at", "核對時間", 140)], height=16)
        self.map_tree.frame.pack(fill="both", expand=True)
        self.map_tree.tag_configure("pending", foreground=theme.P.warn_text)
        self.map_tree.tag_configure("off", foreground=theme.P.text3)
        self.map_tree.bind("<Double-1>", lambda e: self.confirm_mapping())

    def _refresh_mappings(self) -> None:
        sel = set(self.map_tree.selection())
        self.map_tree.delete(*self.map_tree.get_children())
        for m in self.config.mappings:
            state = ("已核對" if m.confirmed else "待核對") + ("" if m.enabled else "・停用")
            acct = next((a.label for a in self.config.accounts if a.id == m.account_id), m.account_id)
            filt = []
            if m.include_patterns:
                filt.append("只含 " + " ".join(m.include_patterns))
            if m.exclude_patterns:
                filt.append("排除 " + " ".join(m.exclude_patterns))
            if m.min_size or m.max_size:
                filt.append(f"大小 {human_size(m.min_size)}~{human_size(m.max_size) if m.max_size else '不限'}")
            if m.skip_hidden:
                filt.append("略過隱藏檔")
            self.map_tree.insert("", "end", iid=m.id, values=(state, m.local_path, m.cloud_path,
                                                              DIRECTION_LABELS.get(m.direction, m.direction), acct,
                                                              "遊戲存檔" if m.savedata else "一般",
                                                              "；".join(filt), m.confirmed_at),
                                 tags=("off",) if not m.enabled else () if m.confirmed else ("pending",))
        self.map_tree.selection_set([i for i in sel if self.map_tree.exists(i)])

    def _selected_mappings(self):
        return [self.config.get_mapping(i) for i in self.map_tree.selection() if self.config.get_mapping(i)]

    def add_mapping(self) -> None:
        r = AddMappingDialog(self.root, self.config.accounts).modal()
        if r:
            m = self.config.add_mapping(r.pop("local_path"), r.pop("cloud_path"), **r)
            self._refresh_mappings()
            self.map_tree.selection_set(m.id)
            if messagebox.askyesno("核對路徑", "要現在核對這個路徑對應嗎？", parent=self.root):
                self.confirm_mapping()

    def suggest_mappings(self) -> None:
        if not self.app.demo and not self.cur_cloud.is_logged_in():
            messagebox.showwarning("尚未登入", "請先登入 115（總覽頁選擇的帳號）", parent=self.root)
            return
        aid = self.account_id
        items = SuggestDialog(self.root, self.cur_cloud, self.ui).modal()
        if items:
            existing = {(m.local_path, m.cloud_path) for m in self.config.mappings}
            for s in items:
                if (s.local_path, s.cloud_path) not in existing:
                    self.config.add_mapping(s.local_path, s.cloud_path, account_id=aid)
            self._refresh_mappings()
            messagebox.showinfo("已加入", f"已加入 {len(items)} 個「待核對」對應，請逐一選取並按「核對確認」。",
                                parent=self.root)

    def confirm_mapping(self) -> None:
        ms = self._selected_mappings()
        if len(ms) != 1:
            messagebox.showinfo("請選取", "請選取一個對應來核對", parent=self.root)
            return
        m = ms[0]
        cloud = self.engine.cloud_for(m)
        r = ConfirmMappingDialog(self.root, m, cloud, self.config.mappings, self.ui,
                                 self.config.settings.browser_path).modal()
        if not r:
            return

        def work():
            cid = r["cid"] if not r["create"] else cloud.ensure_path(m.cloud_path)
            self.config.confirm_mapping(m.id, cid)

        self.run_bg("確認路徑對應", work, lambda _: self._refresh_mappings())

    def edit_mapping(self) -> None:
        ms = self._selected_mappings()
        if len(ms) != 1:
            messagebox.showinfo("請選取", "請選取一個對應", parent=self.root)
            return
        m = ms[0]
        r = AddMappingDialog(self.root, self.config.accounts, m).modal()
        if not r:
            return
        moved = (r["local_path"] != m.local_path or r["cloud_path"] != m.cloud_path
                 or r["account_id"] != m.account_id)
        for k, v in r.items():
            setattr(m, k, v)
        if moved:  # 路徑或帳號變了：必須重新核對
            m.confirmed, m.confirmed_at, m.cloud_cid = False, "", ""
            self.db.forget_cloud_dirs(m.id)
        self.config.save()
        self._refresh_mappings()
        if moved:
            messagebox.showinfo("需要重新核對", "路徑或帳號已變更，請重新「核對確認」。", parent=self.root)

    def unconfirm_mapping(self) -> None:
        for m in self._selected_mappings():
            self.config.unconfirm_mapping(m.id)
        self._refresh_mappings()

    def toggle_mapping(self) -> None:
        for m in self._selected_mappings():
            m.enabled = not m.enabled
        self.config.save()
        self._refresh_mappings()

    def remove_mapping(self) -> None:
        ms = self._selected_mappings()
        if ms and messagebox.askyesno("刪除對應", f"刪除 {len(ms)} 個對應？（不會刪除任何檔案）", parent=self.root):
            for m in ms:
                self.config.remove_mapping(m.id)
            self._refresh_mappings()

    def open_mapping_local(self) -> None:
        for m in self._selected_mappings()[:1]:
            open_local(m.local_path)

    def open_mapping_cloud(self) -> None:
        for m in self._selected_mappings()[:1]:
            def work(m=m):
                c = self.engine.cloud_for(m)
                cid = m.cloud_cid or c.resolve_path(m.cloud_path)
                if cid is None:
                    raise ValueError(f"115 上找不到 {m.cloud_path}")
                open_in_115_browser(c.web_url(cid), self.config.settings.browser_path)
            self.run_bg("開啟 115", work)

    # ================================================================ 待核對
    def _build_reviews(self) -> None:
        page = self.tab_reviews = self._new_tab(
            "待核對", "同名、相同 MD5/SHA-1、遊戲存檔、衝突與刪除——需要你判斷的項目都在這裡", "flag")
        f = page.body
        top = Flow(f)
        top.pack(fill="x", pady=(0, px(M)))
        self.kind_filter = ttk.Combobox(top, state="readonly", width=26, style="Popup.TCombobox",
                                        values=["全部"] + [KIND_LABELS[k] for k in DECISIONS])
        self.kind_filter.set("全部")
        self.kind_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_reviews())
        top.add(self.kind_filter, ttk.Button(top, text="重新整理", command=self._refresh_reviews),
                ttk.Button(top, text="全選此類型", command=self._select_all_reviews),
                ttk.Label(top, text="可多選同類型項目一次處理", style="Caption.TLabel"))

        pane = ttk.PanedWindow(f, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        self.rev_tree = tree_with_scroll(left, [("id", "#", 50), ("kind", "類型", 130), ("path", "路徑", 260)],
                                         height=20)
        self.rev_tree.frame.pack(fill="both", expand=True)
        self.rev_tree.bind("<<TreeviewSelect>>", lambda e: self._show_review())
        pane.add(left, weight=round(MINOR * 1000))
        right = ttk.Frame(pane)
        self.rev_detail = scrolled_text(right, height=20, wrap="char", state="disabled", font="NSBody")
        self.rev_detail.frame.pack(fill="both", expand=True)
        self.proc_frame = ttk.Frame(right)
        ttk.Label(self.proc_frame, text="遊戲執行檔（選填，逗號分隔，執行中不上傳）").pack(anchor="w")
        self.proc_var = tk.StringVar()
        ttk.Entry(self.proc_frame, textvariable=self.proc_var).pack(fill="x", pady=(px(XS), 0))
        self.decision_bar = ttk.Frame(right)
        self.decision_bar.pack(fill="x", pady=(px(M), 0))
        nav = Flow(right)
        nav.pack(fill="x", pady=(px(S), 0))
        nav.add(ttk.Button(nav, text="開啟 NAS 位置", style="Link.TButton", command=self._open_review_local),
                ttk.Button(nav, text="在 115 瀏覽器開啟", style="Link.TButton", command=self._open_review_cloud))
        pane.add(right, weight=round(MAJOR * 1000))
        self._golden_sash(pane)

    def _golden_sash(self, pane: ttk.PanedWindow) -> None:
        """第一次顯示時把分隔線放在黃金分割點（左 0.382、右 0.618）。"""
        def place(event=None):
            if pane.winfo_width() > 1:
                pane.sashpos(0, round(pane.winfo_width() * MINOR))
                pane.unbind("<Map>")
        pane.bind("<Map>", place)

    def _refresh_reviews(self) -> None:
        label = self.kind_filter.get()
        kind = next((k for k, v in KIND_LABELS.items() if v == label), None)
        sel = set(self.rev_tree.selection())
        self.rev_tree.delete(*self.rev_tree.get_children())
        self.review_items.clear()
        for rv in self.db.list_reviews("pending", kind=kind):
            iid = str(rv.id)
            self.review_items[iid] = rv
            m = self.config.get_mapping(rv.mapping_id)
            path = rv.rel_path or "（整個對應）"
            if m:
                path = f"{path}    [{m.cloud_path}]"
            self.rev_tree.insert("", "end", iid=iid, values=(rv.id, KIND_LABELS.get(rv.kind, rv.kind), path))
        keep = [i for i in sel if self.rev_tree.exists(i)]
        if keep:
            self.rev_tree.selection_set(keep)
        else:
            self._show_review()
        self._refresh_stats()

    def _select_all_reviews(self) -> None:
        sel = self._selected_reviews()
        kind = sel[0].kind if sel else None
        self.rev_tree.selection_set([i for i, rv in self.review_items.items() if kind is None or rv.kind == kind])

    def _selected_reviews(self):
        return [self.review_items[i] for i in self.rev_tree.selection() if i in self.review_items]

    def _show_review(self) -> None:
        for w in self.decision_bar.winfo_children():
            w.destroy()
        self.proc_frame.pack_forget()
        sel = self._selected_reviews()
        if not sel:
            set_text(self.rev_detail, "請在左側選取一個待核對項目。")
            return
        show_review_text(self.rev_detail,
                         review_text(sel[0]) + (f"\n\n（共選取 {len(sel)} 項）" if len(sel) > 1 else ""))
        kinds = {rv.kind for rv in sel}
        if len(kinds) != 1:
            ttk.Label(self.decision_bar, text="選取了不同類型的項目，請只選同一類型以一次處理。",
                      style="Warning.TLabel").pack(anchor="w")
            return
        kind = kinds.pop()
        opts = available_decisions(kind, sel[0].details)
        if len(sel) > 1:  # 多選時只顯示每一項都可用的決定
            common = set.intersection(*[{d for d, _ in available_decisions(rv.kind, rv.details)} for rv in sel])
            opts = [o for o in opts if o[0] in common]
        if kind == SAVEDATA_GROUP:
            self.proc_frame.pack(fill="x", pady=(px(M), 0), before=self.decision_bar)
        for i, (decision, label) in enumerate(opts):
            style = "Destructive.TButton" if decision in DANGEROUS else "Accent.TButton" if i == 0 else "TButton"
            ttk.Button(self.decision_bar, text=label, style=style,
                       command=lambda d=decision, lb=label: self._decide(d, lb)).grid(
                row=i // 2, column=i % 2, sticky="we", padx=(0, px(S)), pady=(0, px(S)))
        for c in range(2):
            self.decision_bar.columnconfigure(c, weight=1, uniform="decision")

    def _decide(self, decision: str, label: str) -> None:
        sel = self._selected_reviews()
        if not sel:
            return
        if decision in DANGEROUS and not messagebox.askyesno(
                "請確認", f"確定對 {len(sel)} 個項目執行「{label}」？\n（115 刪除的檔案會進入 115 回收站）",
                parent=self.root):
            return
        procs = [p.strip() for p in self.proc_var.get().split(",") if p.strip()]
        extra = {"process_names": procs} if procs else {}
        ids = [rv.id for rv in sel]

        def work():
            ok, errors = 0, []
            for i, rid in enumerate(ids, 1):
                if self.engine.cancel_event.is_set():
                    raise Cancelled("已取消")
                try:
                    self.engine.apply_review(rid, decision, extra)
                    ok += 1
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    errors.append(f"#{rid}：{e}")
                self.progress_cb("處理核對項目", i, len(ids), str(rid))
            return ok, errors

        def done(res):
            ok, errors = res
            msg = f"完成 {ok} 項"
            if errors:
                msg += f"，失敗 {len(errors)} 項：\n" + "\n".join(errors[:15])
                messagebox.showwarning("處理結果", msg, parent=self.root)
            else:
                self._set_status(msg)

        self.run_bg(f"「{label}」", work, done)

    def _open_review_local(self) -> None:
        sel = self._selected_reviews()
        if sel:
            d = sel[0].details
            path = (d.get("local") or {}).get("path") or d.get("local_dir") or d.get("local_root")
            if path:
                open_local(path)

    def _open_review_cloud(self) -> None:
        sel = self._selected_reviews()
        if not sel:
            return
        rv = sel[0]
        m = self.config.get_mapping(rv.mapping_id)
        if not m:
            return
        cloud = rv.details.get("cloud") or []
        rel_dir = rv.rel_path if rv.kind == SAVEDATA_GROUP else rel_parent(rv.rel_path)
        cid = (cloud[0].get("pid") if cloud else None) or self.db.cloud_dir(m.id, rel_dir) or m.cloud_cid
        if cid:
            open_in_115_browser(self.engine.cloud_for(m).web_url(cid), self.config.settings.browser_path)

    # ================================================================ 遊戲存檔
    def _build_savedata(self) -> None:
        page = self.tab_savedata = self._new_tab(
            "遊戲存檔", "像 Steam 雲存檔：遊戲寫完存檔、程序結束後自動上傳到 115，並刪除 115 上的舊版", "game")
        f = page.body
        bar = Flow(f)
        bar.pack(fill="x", pady=(0, px(M)))
        bar.add(ttk.Button(bar, text="核可自動同步", style="Accent.TButton",
                           command=lambda: self._group_action("approve")),
                ttk.Button(bar, text="立即同步此存檔", command=self._sync_group_now),
                ttk.Button(bar, text="設定遊戲程序…", command=self._set_group_procs),
                ttk.Button(bar, text="當一般檔案", command=lambda: self._group_action("normal")),
                ttk.Button(bar, text="不同步", command=lambda: self._group_action("exclude")),
                ttk.Button(bar, text="開啟 NAS 位置", style="Link.TButton", command=self._open_group_local))
        self.save_tree = tree_with_scroll(f, [("map", "對應", 220), ("dir", "存檔資料夾", 300), ("state", "狀態", 190),
                                              ("proc", "遊戲程序", 160), ("last", "上次上傳", 150)], height=16)
        self.save_tree.frame.pack(fill="both", expand=True)

    def _refresh_savedata(self) -> None:
        self.save_tree.delete(*self.save_tree.get_children())
        self.group_items = {}
        for g in self.db.list_groups():
            m = self.config.get_mapping(g.mapping_id)
            if not m:
                continue
            iid = f"{g.mapping_id}|{g.rel_key}"
            self.group_items[iid] = (m, g)
            self.save_tree.insert("", "end", iid=iid, values=(
                m.cloud_path, g.rel_dir or "（對應根目錄）", GROUP_STATUS.get(g.status, g.status),
                ", ".join(g.process_names), fmt_time(g.last_upload)))

    def _selected_groups(self):
        return [self.group_items[i] for i in self.save_tree.selection() if i in self.group_items]

    def _group_action(self, decision: str) -> None:
        groups = self._selected_groups()
        if not groups:
            return

        def work():
            for m, g in groups:
                self.engine.change_savedata_group(m, g.rel_dir, decision)

        self.run_bg("變更存檔設定", work, lambda _: self._refresh_savedata())

    def _set_group_procs(self) -> None:
        groups = self._selected_groups()
        if not groups:
            return
        cur = ", ".join(groups[0][1].process_names)
        s = simpledialog.askstring("遊戲程序", "遊戲執行檔名稱（逗號分隔，例如 eldenring.exe）。\n"
                                             "這些程序執行中時不會上傳存檔，等遊戲結束才上傳：",
                                   initialvalue=cur, parent=self.root)
        if s is None:
            return
        for m, g in groups:
            self.db.upsert_group(m.id, g.rel_dir, process_names=[p for p in s.split(",")])
        self._refresh_savedata()

    def _sync_group_now(self) -> None:
        groups = [(m, g) for m, g in self._selected_groups() if g.status == "approved"]
        if not groups:
            messagebox.showinfo("無法同步", "請選取「自動同步」狀態的存檔資料夾", parent=self.root)
            return
        self.run_bg("同步存檔", lambda: [self.engine.sync_savedata_group(m, g.rel_dir) for m, g in groups],
                    lambda _: self._refresh_savedata())

    def _open_group_local(self) -> None:
        for m, g in self._selected_groups()[:1]:
            open_local(local_join(m.local_path, g.rel_dir))

    # ================================================================ 紀錄
    def _build_log(self) -> None:
        page = self.tab_log = self._new_tab("紀錄", "執行紀錄與每一次上傳、下載、刪除的同步歷史", "history")
        f = page.body
        pane = ttk.PanedWindow(f, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        self.log_text = scrolled_text(top, height=12, state="disabled", font="NSMono")
        self.log_text.frame.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground=theme.P.warn_text)
        self.log_text.tag_configure("error", foreground=theme.P.danger_text)
        pane.add(top, weight=1)
        bottom = ttk.Frame(pane)
        bar = ttk.Frame(bottom)
        bar.pack(fill="x", pady=(px(S), px(S)))
        ttk.Label(bar, text="同步歷史", style="Section.TLabel").pack(side="left")
        ttk.Button(bar, text="重新整理", command=self._refresh_history).pack(side="right")
        self.hist_tree = tree_with_scroll(bottom, [("ts", "時間", 150), ("action", "動作", 120), ("path", "路徑", 380),
                                                   ("how", "方式", 90), ("size", "大小", 90), ("ok", "結果", 60),
                                                   ("detail", "說明", 250)], height=10)
        self.hist_tree.frame.pack(fill="both", expand=True)
        self.hist_tree.tag_configure("fail", foreground=theme.P.danger_text)
        pane.add(bottom, weight=1)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._on_tab())

    def _on_tab(self) -> None:
        cur = self.nb.select()
        self.sidebar.select(cur)
        if cur == str(self.tab_log):
            self._refresh_history()
        elif cur == str(self.tab_savedata):
            self._refresh_savedata()
        elif cur == str(self.tab_transfers):
            self.transfers_tab.refresh()

    def _append_log(self, line: str, level: int) -> None:
        t = self.log_text
        t.configure(state="normal")
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        t.insert("end", line + "\n", tag)
        if int(t.index("end-1c").split(".")[0]) > 3000:
            t.delete("1.0", "500.0")
        t.see("end")
        t.configure(state="disabled")

    def _refresh_history(self) -> None:
        self.hist_tree.delete(*self.hist_tree.get_children())
        for h in self.db.recent_history(500):
            how = ("秒傳" if h["instant"] else "實際上傳") if str(h["action"]).startswith("upload") else ""
            self.hist_tree.insert("", "end", values=(fmt_time(h["ts"]), h["action"], h["rel_path"], how,
                                                     human_size(h["size"]) if h["size"] else "",
                                                     "成功" if h["ok"] else "失敗", h["detail"] or ""),
                                  tags=() if h["ok"] else ("fail",))

    # ================================================================ 設定
    def _build_settings(self) -> None:
        page = self.tab_settings = self._new_tab("設定", "同步規則、即時同步、遊戲存檔、傳輸與外觀", "settings",
                                                 card=False)
        p = theme.P
        canvas = tk.Canvas(page, highlightthickness=0, background=p.bg, borderwidth=0)
        sb = AutoScrollbar(page, orient="vertical", command=canvas.yview, style="Page.Vertical.TScrollbar")
        f = ttk.Frame(canvas, style="Page.TFrame")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
                        if self.nb.select() == str(self.tab_settings) else None)

        s = self.config.settings
        specs = {spec[0]: spec for spec in SETTING_FIELDS}
        self.settings_vars: dict[str, object] = {}
        for title, names in SETTING_GROUPS:
            ttk.Label(f, text=title, style="PageSection.TLabel").pack(anchor="w", pady=(px(M), px(XS)),
                                                                      padx=(px(S), 0))
            box = theme.card(f, padding=M)
            box.pack(fill="x", padx=(0, px(M)))
            # 黃金分割：說明欄 : 設定欄 = 1 : φ
            box.columnconfigure(0, weight=round(MINOR * 1000), uniform="set")
            box.columnconfigure(1, weight=round(MAJOR * 1000), uniform="set")
            for row, name in enumerate(names):
                spec = specs[name]
                label, typ = spec[1], spec[2]
                val = getattr(s, name)
                pad = (px(XS), px(XS))
                if row:
                    theme.separator(box).grid(row=2 * row - 1, column=0, columnspan=2, sticky="we", pady=px(XXS))
                r = 2 * row
                if typ == "bool":
                    var = tk.BooleanVar(value=bool(val))
                    ttk.Checkbutton(box, text=label, variable=var).grid(row=r, column=0, columnspan=2, sticky="w",
                                                                        pady=pad)
                else:
                    wrap_label(box, label, style="TLabel").grid(row=r, column=0, sticky="we", pady=pad,
                                                                 padx=(0, px(M)))
                    if typ == "lines":
                        var = tk.Text(box, height=6, width=40, font="NSMono", background=p.bg,
                                      padx=px(S), pady=px(S))
                        var.insert("1.0", "\n".join(val))
                        var.grid(row=r, column=1, sticky="we", pady=pad)
                    elif typ == "choice":
                        choices = spec[3]
                        var = tk.StringVar(value=dict(choices).get(val, val))
                        ttk.Combobox(box, textvariable=var, values=[c[1] for c in choices], state="readonly",
                                     style="Popup.TCombobox").grid(row=r, column=1, sticky="we", pady=pad)
                    else:
                        var = tk.StringVar(value=", ".join(val) if typ == "list" else str(val))
                        ttk.Entry(box, textvariable=var, width=40 if typ in ("str", "list") else 10).grid(
                            row=r, column=1, sticky="we" if typ in ("str", "list") else "w", pady=pad)
                self.settings_vars[name] = var

        ttk.Label(f, text="Windows", style="PageSection.TLabel").pack(anchor="w", pady=(px(M), px(XS)),
                                                                      padx=(px(S), 0))
        box = theme.card(f, padding=M)
        box.pack(fill="x", padx=(0, px(M)))
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(box, text="Windows 開機時自動啟動並開始即時同步", variable=self.autostart_var,
                        state="normal" if autostart.supported() else "disabled").pack(anchor="w")
        bar = Flow(f, style="Page.TFrame")
        bar.pack(fill="x", pady=(px(L), px(XS)), padx=(px(S), 0))
        bar.add(ttk.Button(bar, text="儲存設定", style="Large.Accent.TButton", command=self.save_settings),
                ttk.Button(bar, text="選擇 115 瀏覽器…", style="Large.TButton", command=self._pick_browser))
        wrap_label(f, "API 間隔、分片大小、代理與外觀在重新啟動程式後生效；即時同步相關設定在重新開始即時同步後生效。",
                   style="Status.TLabel").pack(fill="x", padx=(px(S), px(M)))
        self._build_schedules(f)

    # ================================================================ 排程
    def _build_schedules(self, parent) -> None:
        ttk.Label(parent, text="排程（在指定時間自動完整比對／同步；需本程式在執行中）", style="PageSection.TLabel").pack(
            anchor="w", pady=(px(L), px(XS)), padx=(px(S), 0))
        box = theme.card(parent, padding=M)
        box.pack(fill="x", padx=(0, px(M)), pady=(0, px(XL)))
        self.sched_tree = tree_with_scroll(box, [("desc", "排程", 520)], height=4, selectmode="browse")
        self.sched_tree.frame.pack(fill="x")
        bar = Flow(box)
        bar.pack(fill="x", pady=(px(M), 0))
        self.sched_time = tk.StringVar(value="03:00")
        items = [ttk.Label(bar, text="時間"), ttk.Entry(bar, textvariable=self.sched_time, width=6)]
        self.sched_days = []
        for d in WEEKDAYS:
            v = tk.BooleanVar(value=True)
            items.append(ttk.Checkbutton(bar, text=d, variable=v))
            self.sched_days.append(v)
        self.sched_action = ttk.Combobox(bar, state="readonly", width=14, values=["完整比對＋同步", "只比對"],
                                         style="Popup.TCombobox")
        self.sched_action.set("完整比對＋同步")
        items += [self.sched_action, ttk.Button(bar, text="新增", style="Accent.TButton", command=self._add_schedule),
                  ttk.Button(bar, text="啟用/停用", command=self._toggle_schedule),
                  ttk.Button(bar, text="立即執行", command=self._run_schedule_now),
                  ttk.Button(bar, text="刪除", style="Destructive.TButton", command=self._del_schedule)]
        bar.add(*items)
        self._refresh_schedules()

    def _refresh_schedules(self) -> None:
        self.sched_tree.delete(*self.sched_tree.get_children())
        for x in self.config.settings.schedules:
            self.sched_tree.insert("", "end", iid=x["id"], values=(describe(x),))

    def _add_schedule(self) -> None:
        t = self.sched_time.get().strip()
        try:
            time.strptime(t, "%H:%M")
        except ValueError:
            messagebox.showerror("時間格式錯誤", "請輸入 HH:MM，例如 03:00", parent=self.root)
            return
        days = [i for i, v in enumerate(self.sched_days) if v.get()]
        if not days:
            return
        action = "sync" if self.sched_action.get().startswith("完整比對＋") else "scan"
        self.config.settings.schedules.append(new_schedule(time.strftime("%H:%M", time.strptime(t, "%H:%M")),
                                                           days, action))
        self.config.save()
        self._refresh_schedules()

    def _selected_schedule(self):
        sel = self.sched_tree.selection()
        return next((x for x in self.config.settings.schedules if sel and x["id"] == sel[0]), None)

    def _del_schedule(self) -> None:
        x = self._selected_schedule()
        if x:
            self.config.settings.schedules.remove(x)
            self.config.save()
            self._refresh_schedules()

    def _toggle_schedule(self) -> None:
        x = self._selected_schedule()
        if x:
            x["enabled"] = not x.get("enabled", True)
            self.config.save()
            self._refresh_schedules()

    def _run_schedule_now(self) -> None:
        x = self._selected_schedule()
        if x and self._ready_to_sync():
            self.run_bg("排程工作", lambda: self.scheduler.run_schedule(x))

    def _pick_browser(self) -> None:
        p = filedialog.askopenfilename(parent=self.root, title="選擇 115 瀏覽器（115chrome.exe）",
                                       filetypes=[("程式", "*.exe"), ("全部", "*.*")])
        if p:
            self.settings_vars["browser_path"].set(p)

    def save_settings(self) -> None:
        s = self.config.settings
        defaults = Settings()
        try:
            for spec in SETTING_FIELDS:
                name, typ = spec[0], spec[2]
                var = self.settings_vars[name]
                if typ == "bool":
                    val = bool(var.get())
                elif typ == "lines":
                    val = [x.strip() for x in var.get("1.0", "end").splitlines() if x.strip()]
                elif typ == "list":
                    val = [x.strip() for x in var.get().split(",") if x.strip()]
                elif typ == "choice":
                    val = next((k for k, v in spec[3] if v == var.get()), getattr(defaults, name))
                elif typ == "int":
                    val = int(var.get())
                elif typ == "float":
                    val = float(var.get())
                else:
                    val = var.get().strip()
                setattr(s, name, val)
        except ValueError as e:
            messagebox.showerror("設定格式錯誤", f"請檢查數字欄位：{e}", parent=self.root)
            return
        self.config.save()
        self.engine.transfers.set_limits(s.upload_limit_kb, s.download_limit_kb)
        if autostart.supported():
            try:
                autostart.set_enabled(self.autostart_var.get())
            except OSError as e:
                messagebox.showwarning("開機自動啟動", f"設定失敗：{e}", parent=self.root)
        self._set_status("設定已儲存")

    # ================================================================ 關閉
    def _on_close(self) -> None:
        if (self.rt.running or self.busy) and not messagebox.askyesno(
                "結束程式", "即時同步或工作仍在進行，結束後將停止同步。確定結束？", parent=self.root):
            return
        self.engine.cancel_event.set()
        self.scheduler.stop()
        if self.rt.running:
            self.rt.stop()
        logging.getLogger().removeHandler(self._log_handler)
        self.root.destroy()


def review_text(rv) -> str:
    d = rv.details
    out = [f"{KIND_LABELS.get(rv.kind, rv.kind)}　#{rv.id}", d.get("message", ""), ""]
    out.append(f"NAS 根目錄：{d.get('local_root', '')}")
    out.append(f"115 根目錄：{d.get('cloud_root', '')}")
    out.append(f"同步方向：{DIRECTION_LABELS.get(d.get('direction', 'upload'), '')}")
    loc = d.get("local") or {}
    if loc:
        out += ["", "■ NAS 檔案",
                f"  路徑：{loc.get('path', '')}",
                f"  大小：{human_size(loc.get('size'))}（{loc.get('size') or 0:,} bytes）"]
        if loc.get("mtime"):
            out.append(f"  修改時間：{fmt_time(loc['mtime'])}")
        if loc.get("md5"):
            out.append(f"  MD5：{loc['md5']}")
        if loc.get("sha1"):
            out.append(f"  SHA-1：{loc['sha1']}")
    for c in d.get("cloud") or []:
        same = ""
        if loc.get("sha1") and c.get("sha1"):
            same = "（與 NAS 相同）" if c["sha1"] == loc["sha1"] else "（與 NAS 不同）"
        out += ["", "■ 115 檔案",
                f"  路徑：{c.get('path', '')}",
                f"  大小：{human_size(c.get('size'))}（{c.get('size') or 0:,} bytes）",
                f"  SHA-1：{c.get('sha1', '')} {same}",
                f"  上傳時間：{fmt_time(c.get('mtime'))}",
                f"  檔案 ID：{c.get('fid', '')}"]
    matches = d.get("matches") or []
    if matches:
        out += ["", f"■ 內容相同的其他檔案（{len(matches)}）"]
        out += [f"  [{mt.get('where')}] 相同 {mt.get('hash')}：{mt.get('path') or mt.get('rel_path')}"
                for mt in matches[:50]]
    if d.get("move_candidate"):
        out += ["", f"→ 推測是從「{d['move_candidate']['from_rel']}」搬移或改名而來（原位置的 NAS 檔已不存在），"
                    "可選擇在 115 直接搬移，不必重新上傳。"]
    if rv.kind == SAVEDATA_GROUP:
        out += ["", "■ 存檔資料夾",
                f"  NAS：{d.get('local_dir', '')}",
                f"  115：{d.get('cloud_dir', '')}",
                f"  檔案數：{d.get('file_count', 0)}，總大小：{human_size(d.get('total_size'))}", "  檔案："]
        out += [f"    {p}" for p in d.get("samples", [])]
        out += ["", "核可後：遊戲寫完存檔（靜置後、遊戲程序結束後）會自動上傳新版並刪除 115 舊版，"
                    "效果類似 Steam 雲存檔。"]
    if d.get("paths"):
        out += ["", f"■ 不見的檔案（{d.get('count', len(d['paths']))}）"]
        out += [f"  {p}" for p in d["paths"][:100]]
        if len(d["paths"]) > 100:
            out.append(f"  …其餘 {len(d['paths']) - 100} 個")
    out += ["", "說明：SHA-1 相同即內容相同。115 只提供 SHA-1；MD5 由本程式在 NAS 端計算並記錄。"]
    return "\n".join(out)


def show_review_text(widget: tk.Text, content: str) -> None:
    """排版待核對說明：第一行是標題、「■」開頭是粗體小節標題、MD5／SHA-1 用等寬字型、補充說明用次要文字色。"""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.tag_configure("title", font="NSHeadline", spacing3=px(XS))
    widget.tag_configure("section", font="NSBodyBold", spacing1=px(S), spacing3=px(XXS))
    widget.tag_configure("mono", font="NSMono")
    widget.tag_configure("note", foreground=theme.P.text2)
    for i, line in enumerate(content.split("\n")):
        tags: tuple[str, ...] = ()
        if i == 0:
            tags = ("title",)
        elif line.startswith("■ "):
            line, tags = line[2:], ("section",)
        elif line.startswith(("說明：", "→ ", "核可後：", "（共選取")):
            tags = ("note",)
        start = widget.index("end-1c")
        widget.insert("end", line + "\n", tags)
        for m in re.finditer(r"\b[0-9A-Fa-f]{32}(?:[0-9A-Fa-f]{8})?\b", line):
            widget.tag_add("mono", f"{start}+{m.start()}c", f"{start}+{m.end()}c")
    widget.configure(state="disabled")


def apply_window_icon(root: tk.Tk) -> None:
    icon = resource_path("assets/app.ico")
    if sys.platform == "win32" and os.path.exists(icon):
        try:
            root.iconbitmap(default=icon)
        except tk.TclError:
            pass


def run_gui(app: App, start_realtime: bool = False, minimized: bool = False) -> None:
    enable_dpi_awareness()
    if not single_instance("NAS115Sync_demo" if app.demo else "NAS115Sync"):
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("NAS115Sync", "NAS115Sync 已在執行中（請查看工作列）。")
        root.destroy()
        return
    root = tk.Tk()
    setup_fonts(root)
    apply_window_icon(root)
    MainWindow(root, app, start_realtime=start_realtime)
    if minimized:
        root.iconify()
    root.mainloop()
