"""對話框與「同步」視窗（比照 Air Explorer 的 Windows 對話框：淺灰底、右下角「確定／取消」）。"""
from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import autostart
from ..compare import KIND_LABELS
from ..config import DELETE_LABELS, DIRECTION_LABELS, DOWNLOAD, TWO_WAY, UPLOAD, Settings
from ..mapping_check import inspect_mapping, suggest_mappings
from ..paths import cloud_join, cloud_norm, local_join, norm_key, unsafe_name
from ..scheduler import WEEKDAYS, describe_when, new_schedule, next_run
from . import icons, theme
from .gridlist import Column, GridList
from .theme import px
from .util import (AutoScrollbar, fmt_date, fmt_time, human_size, make_tree, open_local, popup, popup_below,
                   scrolled_text, set_text, wrap_label)
from .widgets import Toggle, tool_button

DIRECTION_ICON = {UPLOAD: "to_right", DOWNLOAD: "to_left", TWO_WAY: "compare"}
DIRECTION_RULES = {
    UPLOAD: ("把 NAS 的內容備份到 115（上傳時先嘗試秒傳）",
             [("只有 NAS 有的檔案", "上傳到 115"), ("只有 115 有的檔案", "不處理（列為「僅 115 有」）"),
              ("NAS 的檔案改過", "上傳新版，115 舊版依設定刪除或保留"), ("115 的檔案被改過", "交給本人核對")]),
    DOWNLOAD: ("把 115 的內容下載到 NAS（NAS 上已有相同內容時直接複製）",
               [("只有 115 有的檔案", "下載到 NAS"), ("只有 NAS 有的檔案", "不處理（列為「僅 NAS 有」）"),
                ("115 的檔案改過", "下載新版，NAS 舊版移到可復原資料夾"), ("NAS 的檔案被改過", "交給本人核對")]),
    TWO_WAY: ("兩邊互相補齊，像 OneDrive 一樣保持一致",
              [("只有 NAS 有的檔案", "上傳到 115"), ("只有 115 有的檔案", "下載到 NAS"),
               ("只有一邊改過", "把新版同步到另一邊"), ("兩邊都改過", "交給本人核對")]),
}


class _Dialog(tk.Toplevel):
    def __init__(self, parent, title: str, padding: int = 12):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.configure(background=theme.P.chrome)
        self.result = None
        self.body = ttk.Frame(self, style="Chrome.TFrame", padding=px(padding))
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())
        theme.apply_titlebar(self)

    def buttons(self, *specs, parent=None) -> ttk.Frame:
        """右下角的按鈕列。specs：(文字, 動作, 是否為預設按鈕)。"""
        bar = ttk.Frame(parent or self.body, style="Chrome.TFrame")
        for text, command, *default in reversed(specs):
            ttk.Button(bar, text=text, command=command, style="Accent.TButton" if default and default[0] else "TButton").pack(
                side="right", padx=(px(6), 0))
        return bar

    def center(self, w: int, h: int) -> None:
        """置中於主視窗；大小至少要容納內容（字型與縮放比例不同時，內容需要的空間也不同）。"""
        self.update_idletasks()
        w = min(max(w, self.winfo_reqwidth()), self.winfo_screenwidth() - px(20))
        h = min(max(h, self.winfo_reqheight()), self.winfo_screenheight() - px(60))
        p = self.master
        x = p.winfo_rootx() + max(0, (p.winfo_width() - w) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - h) // 3)
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    def modal(self):
        self.grab_set()
        self.wait_window()
        return self.result

    def destroy(self):
        super().destroy()
        # 立即在主執行緒釋放 Tk 變數：否則之後可能由背景執行緒的垃圾回收釋放，而 Tk 不允許跨執行緒呼叫
        for name, value in list(vars(self).items()):
            if isinstance(value, tk.Variable):
                delattr(self, name)
            elif isinstance(value, dict) and value and all(isinstance(v, tk.Variable) for v in value.values()):
                value.clear()


def _label(parent, text: str, style: str = "Chrome.TLabel", **kw) -> ttk.Label:
    return ttk.Label(parent, text=text, style=style, **kw)


# =========================================================================== 同步設定（新增／編輯）
class AddMappingDialog(_Dialog):
    """建立或編輯一組同步：NAS 資料夾、115 資料夾、同步方式、刪除處理、篩選。"""

    def __init__(self, win, mapping=None, local_path: str = "", account_id: str = "default", cloud_path: str = ""):
        super().__init__(win.root, "同步 - 選項" if mapping else "建立新的同步")
        self.win, self.accounts = win, win.config.accounts
        m = mapping
        b = self.body
        b.columnconfigure(1, weight=1)

        _label(b, "名稱").grid(row=0, column=0, sticky="w", pady=px(3))
        self.name = tk.StringVar(value=m.name if m else "")
        ttk.Entry(b, textvariable=self.name).grid(row=0, column=1, sticky="we", pady=px(3))
        _label(b, "（空白時用 NAS 資料夾名稱）", "Chrome.Secondary.TLabel").grid(row=0, column=2, sticky="w", padx=px(6))

        _label(b, "NAS 資料夾", image=theme.icon("computer"), compound="left").grid(row=1, column=0, sticky="w", pady=px(3))
        self.local = tk.StringVar(value=m.local_path if m else local_path)
        ttk.Entry(b, textvariable=self.local).grid(row=1, column=1, sticky="we", pady=px(3))
        ttk.Button(b, text="瀏覽…", command=self._browse).grid(row=1, column=2, sticky="w", padx=px(6))

        _label(b, "115 帳號", image=theme.icon("cloud"), compound="left").grid(row=2, column=0, sticky="w", pady=px(3))
        self.account = ttk.Combobox(b, state="readonly", values=[a.label for a in self.accounts], width=24)
        cur = next((a.label for a in self.accounts if a.id == (m.account_id if m else account_id)), self.accounts[0].label)
        self.account.set(cur)
        self.account.grid(row=2, column=1, sticky="w", pady=px(3))

        _label(b, "115 資料夾").grid(row=3, column=0, sticky="w", pady=px(3), padx=(px(21), px(8)))
        self.cloud = tk.StringVar(value=m.cloud_path if m else cloud_path)
        ttk.Entry(b, textvariable=self.cloud).grid(row=3, column=1, sticky="we", pady=px(3))
        ttk.Button(b, text="瀏覽…", command=self._browse_cloud).grid(row=3, column=2, sticky="w", padx=px(6))

        # 同步方式：左邊三個模式按鈕，右邊是這個模式的規則（與 Air Explorer 的「Synchronize - Options」相同的排法）
        mode = ttk.Frame(b, style="Chrome.TFrame")
        mode.grid(row=4, column=0, columnspan=3, sticky="we", pady=(px(10), px(4)))
        mode.columnconfigure(1, weight=1)
        self.direction = tk.StringVar(value=DIRECTION_LABELS[m.direction if m else UPLOAD])
        self._mode_buttons: dict[str, ttk.Button] = {}
        col = ttk.Frame(mode, style="Chrome.TFrame")
        col.grid(row=0, column=0, sticky="n")
        for key in (UPLOAD, DOWNLOAD, TWO_WAY):
            btn = ttk.Button(col, text=DIRECTION_LABELS[key], style="Wide.TButton", width=24,
                             command=lambda k=key: self.direction.set(DIRECTION_LABELS[k]))
            btn.pack(fill="x", pady=(0, px(6)))
            self._mode_buttons[key] = btn
        self.rules = ttk.Frame(mode, padding=px(10))
        self.rules.grid(row=0, column=1, sticky="nsew", padx=(px(10), 0))
        self.rules.columnconfigure(1, weight=1)
        self.direction.trace_add("write", lambda *_: self._show_mode())

        opts = ttk.Frame(b, style="Chrome.TFrame")
        opts.grid(row=5, column=0, columnspan=3, sticky="we", pady=px(4))
        _label(opts, "一邊刪除檔案時").pack(side="left")
        self.delete_mode = ttk.Combobox(opts, state="readonly", width=24, values=list(DELETE_LABELS.values()))
        self.delete_mode.set(DELETE_LABELS[m.delete_mode if m else ""])
        self.delete_mode.pack(side="left", padx=(px(6), px(16)))
        self.savedata = tk.BooleanVar(value=bool(m and m.savedata))
        ttk.Checkbutton(opts, text="整個資料夾都是遊戲存檔（每個子資料夾為一個遊戲）", variable=self.savedata,
                        style="Chrome.TCheckbutton").pack(side="left")

        hash_row = ttk.Frame(b, style="Chrome.TFrame")
        hash_row.grid(row=6, column=0, columnspan=3, sticky="we")
        always = ttk.Checkbutton(hash_row, text="以檔案內容比對（MD5／SHA-1），內容相同就不重複傳輸；上傳一律先嘗試秒傳",
                                 style="Chrome.TCheckbutton")
        always.state(["selected", "disabled"])
        always.pack(side="left")

        flt = ttk.LabelFrame(b, text="篩選（選填）", style="Chrome.TLabelframe")
        flt.grid(row=7, column=0, columnspan=3, sticky="we", pady=(px(8), 0))
        flt.columnconfigure(1, weight=1)
        _label(flt, "只同步符合的檔案（例如 *.jpg *.mp4）").grid(row=0, column=0, sticky="w", pady=px(2))
        self.include = tk.StringVar(value=" ".join(m.include_patterns) if m else "")
        ttk.Entry(flt, textvariable=self.include).grid(row=0, column=1, sticky="we", padx=(px(8), 0), pady=px(2))
        _label(flt, "排除符合的檔案（例如 *.iso cache Temp/*）").grid(row=1, column=0, sticky="w", pady=px(2))
        self.exclude = tk.StringVar(value=" ".join(m.exclude_patterns) if m else "")
        ttk.Entry(flt, textvariable=self.exclude).grid(row=1, column=1, sticky="we", padx=(px(8), 0), pady=px(2))
        _label(flt, "檔案大小 MB（最小～最大，0 = 不限）").grid(row=2, column=0, sticky="w", pady=px(2))
        sz = ttk.Frame(flt, style="Chrome.TFrame")
        sz.grid(row=2, column=1, sticky="w", padx=(px(8), 0), pady=px(2))
        self.min_mb = tk.StringVar(value=str((m.min_size if m else 0) / 1048576).rstrip("0").rstrip(".") or "0")
        self.max_mb = tk.StringVar(value=str((m.max_size if m else 0) / 1048576).rstrip("0").rstrip(".") or "0")
        ttk.Entry(sz, textvariable=self.min_mb, width=9).pack(side="left")
        _label(sz, " ～ ").pack(side="left")
        ttk.Entry(sz, textvariable=self.max_mb, width=9).pack(side="left")
        self.skip_hidden = tk.BooleanVar(value=bool(m and m.skip_hidden))
        ttk.Checkbutton(flt, text="略過隱藏檔／系統檔", variable=self.skip_hidden, style="Chrome.TCheckbutton").grid(
            row=3, column=0, sticky="w", pady=px(2))

        _label(b, "建立或變更路徑後狀態為「待核對」，要按「核對確認」由本人確認兩邊路徑後才會同步。",
               "Chrome.Warning.TLabel").grid(row=8, column=0, columnspan=3, sticky="w", pady=(px(10), 0))
        self.buttons(("確定", self._ok, True), ("取消", self.destroy)).grid(row=9, column=0, columnspan=3, sticky="e",
                                                                        pady=(px(12), 0))
        self._show_mode()
        self.center(px(760), px(520))

    def _show_mode(self) -> None:
        key = next((k for k, v in DIRECTION_LABELS.items() if v == self.direction.get()), UPLOAD)
        for k, btn in self._mode_buttons.items():
            btn.configure(style="Accent.TButton" if k == key else "TButton")
            btn.state(["pressed"] if k == key else ["!pressed"])
        for w in self.rules.winfo_children():
            w.destroy()
        title, rules = DIRECTION_RULES[key]
        ttk.Label(self.rules, text=title, style="Bold.TLabel").grid(row=0, column=0, columnspan=2, sticky="w",
                                                                    pady=(0, px(6)))
        for i, (case, action) in enumerate(rules, 1):
            ttk.Label(self.rules, text=case, style="Secondary.TLabel").grid(row=i, column=0, sticky="e", pady=px(2))
            ttk.Label(self.rules, text=action).grid(row=i, column=1, sticky="w", padx=(px(10), 0), pady=px(2))

    def _browse(self) -> None:
        d = filedialog.askdirectory(parent=self, title="選擇 NAS 資料夾")
        if d:
            self.local.set(d.replace("/", "\\") if "\\" in d or ":" in d else d)

    def _account_id(self) -> str:
        labels = [a.label for a in self.accounts]
        return self.accounts[labels.index(self.account.get())].id

    def _browse_cloud(self) -> None:
        path = CloudFolderDialog(self.win, self._account_id(), "選擇 115 資料夾", self.cloud.get() or "/", parent=self).modal()
        if path:
            self.cloud.set(path)

    def _ok(self) -> None:
        if not self.local.get().strip() or not self.cloud.get().strip():
            messagebox.showwarning("缺少資料", "請填寫 NAS 資料夾與 115 資料夾", parent=self)
            return
        try:
            min_size = int(float(self.min_mb.get() or 0) * 1048576)
            max_size = int(float(self.max_mb.get() or 0) * 1048576)
        except ValueError:
            messagebox.showwarning("格式錯誤", "檔案大小請填數字（MB）", parent=self)
            return
        inv_dir = {v: k for k, v in DIRECTION_LABELS.items()}
        inv_del = {v: k for k, v in DELETE_LABELS.items()}
        self.result = {
            "name": self.name.get().strip(),
            "local_path": self.local.get().strip(), "cloud_path": cloud_norm(self.cloud.get().strip()),
            "savedata": self.savedata.get(), "direction": inv_dir[self.direction.get()],
            "delete_mode": inv_del[self.delete_mode.get()], "account_id": self._account_id(),
            "include_patterns": self.include.get().split(), "exclude_patterns": self.exclude.get().split(),
            "min_size": min_size, "max_size": max_size, "skip_hidden": self.skip_hidden.get(),
        }
        self.destroy()


class SuggestDialog(_Dialog):
    """把 NAS 根目錄下所有資料夾依名稱配對到 115，勾選後加入為「待核對」。"""

    def __init__(self, parent, cloud, ui):
        super().__init__(parent, "自動建議同步（NAS 所有資料夾）")
        self.cloud, self.ui = cloud, ui
        b = self.body
        row = ttk.Frame(b, style="Chrome.TFrame")
        row.pack(fill="x")
        _label(row, "NAS 根目錄").pack(side="left")
        self.nas_root = tk.StringVar()
        ttk.Entry(row, textvariable=self.nas_root).pack(side="left", fill="x", expand=True, padx=px(6))
        ttk.Button(row, text="瀏覽…", command=self._browse).pack(side="left")
        row2 = ttk.Frame(b, style="Chrome.TFrame")
        row2.pack(fill="x", pady=px(6))
        _label(row2, "115 根目錄").pack(side="left")
        self.cloud_root = tk.StringVar(value="/NAS備份")
        ttk.Entry(row2, textvariable=self.cloud_root).pack(side="left", fill="x", expand=True, padx=px(6))
        self.btn = ttk.Button(row2, text="產生建議", command=self._suggest)
        self.btn.pack(side="left")
        self.tree = make_tree(b, [("local", "NAS 資料夾", 320), ("cloud", "115 資料夾", 280), ("state", "115 狀態", 110)],
                              height=12)
        self.tree.frame.pack(fill="both", expand=True)
        self.status = _label(b, "選取要加入的項目（可多選），加入後仍需逐一「核對確認」。")
        self.status.pack(anchor="w", pady=px(6))
        bar = self.buttons(("全選", lambda: self.tree.selection_set(self.tree.get_children())),
                           ("加入選取項目", self._ok, True), ("取消", self.destroy))
        bar.pack(fill="x")
        self.items: dict = {}
        self.center(px(800), px(500))

    def _browse(self) -> None:
        d = filedialog.askdirectory(parent=self, title="選擇 NAS 根目錄")
        if d:
            self.nas_root.set(d)

    def _suggest(self) -> None:
        nas, root = self.nas_root.get().strip(), cloud_norm(self.cloud_root.get().strip() or "/")
        if not nas:
            return
        self.btn.configure(state="disabled")
        self.status.configure(text="讀取中…")

        def work() -> None:
            try:
                res = suggest_mappings(nas, root, self.cloud)
                self.ui.call(self._show, res, None)
            except Exception as e:  # noqa: BLE001
                self.ui.call(self._show, [], e)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, res, err) -> None:
        if not self.winfo_exists():
            return
        self.btn.configure(state="normal")
        if err:
            self.status.configure(text=f"失敗：{err}")
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for s in res:
            iid = self.tree.insert("", "end", values=(s.local_path, s.cloud_path,
                                                      "已有同名資料夾" if s.cloud_exists else "將新建"))
            self.items[iid] = s
        self.status.configure(text=f"找到 {len(res)} 個資料夾。選取要加入的項目，加入後仍需逐一「核對確認」。")

    def _ok(self) -> None:
        self.result = [self.items[i] for i in self.tree.selection() if i in self.items]
        self.destroy()


class ConfirmMappingDialog(_Dialog):
    """本人核對：同時列出 NAS 與 115 兩邊的內容，勾選「我已核對」後才可確認。"""

    def __init__(self, parent, mapping, cloud, mappings, ui, browser_path: str = ""):
        super().__init__(parent, "核對同步的路徑")
        self.m, self.cloud, self.ui, self.browser_path = mapping, cloud, ui, browser_path
        self.info = None
        b = self.body
        _label(b, "請核對 NAS 與 115 兩邊的路徑與內容是否為同一份資料", "Chrome.Bold.TLabel").pack(anchor="w")
        self.text = scrolled_text(b, height=20, wrap="char")
        self.text.frame.pack(fill="both", expand=True, pady=px(6))
        set_text(self.text, "讀取 NAS 與 115 內容中…")
        self.create = tk.BooleanVar(value=False)
        self.create_holder = ttk.Frame(b, style="Chrome.TFrame")  # 115 上沒有資料夾時才顯示「建立」選項
        self.create_holder.pack(fill="x")
        self.checked = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="我已本人核對：NAS 與 115 路徑對應正確", variable=self.checked, command=self._update,
                        style="Chrome.TCheckbutton").pack(anchor="w")
        bar = ttk.Frame(b, style="Chrome.TFrame")
        bar.pack(fill="x", pady=(px(8), 0))
        ttk.Button(bar, text="開啟 NAS 資料夾", command=lambda: open_local(self.m.local_path)).pack(side="left")
        self.open115 = ttk.Button(bar, text="在 115 瀏覽器開啟", command=self._open115, state="disabled")
        self.open115.pack(side="left", padx=px(6))
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right", padx=(px(6), 0))
        self.ok = ttk.Button(bar, text="確認對應", style="Accent.TButton", command=self._ok, state="disabled")
        self.ok.pack(side="right")
        self.center(px(780), px(620))
        threading.Thread(target=self._load, args=(mappings,), daemon=True).start()

    def _load(self, mappings) -> None:
        try:
            info = inspect_mapping(self.m, self.cloud, mappings)
            self.ui.call(self._loaded, info, None)
        except Exception as e:  # noqa: BLE001
            self.ui.call(self._loaded, None, e)

    def _loaded(self, info, err) -> None:
        if not self.winfo_exists():
            return
        if err:
            set_text(self.text, f"讀取失敗：{err}")
            return
        self.info = info
        set_text(self.text, "\n".join(info.lines(self.m)))
        if info.cloud_cid is not None:
            self.open115.configure(state="normal")
        elif not info.cloud_error:
            ttk.Checkbutton(self.create_holder, text=f"115 上沒有此資料夾，建立 {self.m.cloud_path}",
                            variable=self.create, command=self._update, style="Chrome.TCheckbutton").pack(anchor="w")
        self._update()

    def _update(self) -> None:
        ok = (self.info is not None and self.info.local_ok and self.checked.get()
              and (self.info.cloud_cid is not None or (not self.info.cloud_error and self.create.get())))
        self.ok.configure(state="normal" if ok else "disabled")

    def _open115(self) -> None:
        from ..app import open_in_115_browser

        if self.info and self.info.cloud_cid is not None:
            open_in_115_browser(self.cloud.web_url(self.info.cloud_cid), self.browser_path)

    def _ok(self) -> None:
        self.result = {"cid": self.info.cloud_cid, "create": self.info.cloud_cid is None}
        self.destroy()


def _round_poly(x1: int, y1: int, x2: int, y2: int, r: int) -> list[int]:
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class QrLoginDialog(_Dialog):
    """與 115 電腦版相同的登入方式：115 App 掃 QR 碼。可選擇使用哪一個「登入裝置欄位」。"""

    def __init__(self, parent, cloud, ui):
        from ..cloud.web115 import DEFAULT_DEVICE, LOGIN_DEVICES

        super().__init__(parent, "115 掃碼登入")
        self.cloud, self.ui = cloud, ui
        self._closed = threading.Event()
        self._devices = LOGIN_DEVICES
        b = self.body
        wrap_label(b, "用手機 115 App 掃描 QR 碼並確認（與 115 電腦版是同一個帳號、同一個網盤）。",
                   style="Chrome.TLabel").pack(fill="x")
        row = ttk.Frame(b, style="Chrome.TFrame")
        row.pack(fill="x", pady=(px(10), 0))
        _label(row, "登入裝置欄位").pack(side="left", padx=(0, px(8)))
        self.device = ttk.Combobox(row, state="readonly", width=20, values=list(LOGIN_DEVICES.values()))
        self.device.set(LOGIN_DEVICES.get(getattr(cloud, "device", DEFAULT_DEVICE), LOGIN_DEVICES[DEFAULT_DEVICE]))
        self.device.pack(side="left", fill="x", expand=True)
        wrap_label(b, "115 每種裝置只允許一個登入：請選你「沒有在用」的裝置，否則該裝置上的 115 會被登出。\n"
                      "預設的「支付寶小程序」不會影響 115 電腦版與手機 App。",
                   style="Chrome.Warning.TLabel").pack(fill="x", pady=(px(6), 0))
        bar = ttk.Frame(b, style="Chrome.TFrame")
        bar.pack(side="bottom", fill="x", pady=(px(12), 0))
        ttk.Button(bar, text="取消", command=self.close).pack(side="right", padx=(px(6), 0))
        self.start_btn = ttk.Button(bar, text="顯示 QR 碼", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="right")
        self.qr_size = px(233)
        p = theme.P
        self.canvas = tk.Canvas(b, width=self.qr_size, height=self.qr_size, bg=p.chrome, highlightthickness=0)
        self.canvas.pack(pady=(px(14), px(6)))
        q, r = self.qr_size - 2, px(10)
        self.canvas.create_polygon(_round_poly(1, 1, q, q, r), fill="", outline=p.text3, dash=(4, 4), smooth=True)
        self.canvas.create_text(self.qr_size // 2, self.qr_size // 2, text="QR 碼會顯示在這裡", fill=p.text3, font="UISmall")
        self.status = _label(b, "選好裝置欄位後按「顯示 QR 碼」", "Chrome.Secondary.TLabel")
        self.status.pack()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.center(px(440), px(540))

    def start(self) -> None:
        device = next((k for k, v in self._devices.items() if v == self.device.get()), None)
        self.start_btn.configure(state="disabled")
        self.device.configure(state="disabled")
        self.status.configure(text="取得 QR 碼中…")
        threading.Thread(target=self._run, args=(device,), daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        self.destroy()

    def _draw(self, text: str) -> None:
        if not self.winfo_exists():
            return
        self.canvas.delete("all")
        self.canvas.configure(bg="white")  # QR 碼一律白底黑點，深色模式下手機才掃得到
        try:
            import qrcode
        except ImportError:
            self.canvas.create_text(self.qr_size // 2, self.qr_size // 2, text=text, width=self.qr_size - px(8))
            return
        qr = qrcode.QRCode(border=2)
        qr.add_data(text)
        qr.make(fit=True)
        mat = qr.get_matrix()
        scale = max(2, self.qr_size // len(mat))
        size = scale * len(mat)
        self.canvas.configure(width=size, height=size)
        for y, row in enumerate(mat):
            for x, v in enumerate(row):
                if v:
                    self.canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale,
                                                 fill="black", outline="black")

    def _set(self, msg: str) -> None:
        if self.winfo_exists():
            self.status.configure(text=msg)

    def _run(self, device) -> None:
        from ..cloud.web115 import QR_CONFIRMED, QR_SCANNED

        try:
            login = self.cloud.start_qr_login(device)
            self.ui.call(self._draw, login.qrcode)
            self.ui.call(self._set, "等待掃描…")
            while not self._closed.is_set():
                st = self.cloud.poll_qr_status(login)
                if st == QR_CONFIRMED:
                    break
                if st == QR_SCANNED:
                    self.ui.call(self._set, "已掃描，請在手機上確認…")
                elif st < 0:
                    self.ui.call(self._set, "QR 碼已過期或取消，請關閉後重試")
                    return
            if self._closed.is_set():
                return
            self.cloud.finish_qr_login(login)
            self.result = True
            self.ui.call(self._done)
        except Exception as e:  # noqa: BLE001
            self.ui.call(self._set, f"登入失敗：{e}")

    def _done(self) -> None:
        if self.winfo_exists():
            self.destroy()


# =========================================================================== 選項
SETTING_FIELDS: dict[str, tuple] = {
    "appearance": ("外觀（重新啟動程式後生效）", "choice", [("auto", "跟隨 Windows"), ("light", "淺色"), ("dark", "深色")]),
    "manual_conflict": ("「瀏覽」頁傳輸遇到同名但內容不同的檔案", "choice",
                        [("keep_both", "保留兩者（新檔加上時間）"), ("overwrite", "覆蓋（舊檔可復原）"), ("skip", "略過")]),
    "show_hidden": ("「瀏覽」頁顯示隱藏檔", "bool"),
    "browser_path": ("115 瀏覽器執行檔（空白 = 自動尋找）", "str"),
    "auto_update_linked": ("已核對連結的檔案：NAS 修改後自動上傳新版並刪除 115 舊版", "bool"),
    "deletion_policy": ("一邊刪除檔案時（各同步可另外設定）", "choice",
                        [("review", "交給本人核對"), ("auto", "自動同步刪除（115 進回收站／NAS 移到可復原資料夾）"),
                         ("ignore", "不處理")]),
    "review_same_name_elsewhere": ("新檔案與 115 其他資料夾同檔名時要本人核對", "bool"),
    "mass_delete_guard_min": ("大量刪除保護：一次至少幾個檔案", "int"),
    "mass_delete_guard_ratio": ("大量刪除保護：占已同步檔案比例（0~1）", "float"),
    "instant_first": ("先對全部檔案嘗試秒傳，再實際上傳其餘檔案", "bool"),
    "verify_uploads": ("實際上傳後向 115 讀回 SHA-1 驗證", "bool"),
    "keep_versions": ("一般檔案：115 舊版保留份數（0 = 上傳新版後刪除舊版）", "int"),
    "stable_seconds": ("一般檔案停止變動幾秒後才上傳", "float"),
    "cloud_poll_seconds": ("多久檢查一次 115 端（115 電腦版、App）的變動（秒）", "int"),
    "rescan_interval_minutes": ("定期完整比對間隔（分鐘，補抓網路磁碟漏掉的事件）", "int"),
    "use_polling_observer": ("改用輪詢監看（NAS 不回報變動事件時勾選）", "bool"),
    "polling_interval_seconds": ("輪詢間隔（秒）", "int"),
    "savedata_dir_names": ("存檔資料夾名稱（逗號分隔）", "list"),
    "savedata_extensions": ("存檔副檔名（逗號分隔）", "list"),
    "savedata_quiet_seconds": ("存檔最後一次變動後靜置幾秒才上傳", "int"),
    "savedata_keep_versions": ("115 保留存檔舊版份數（0 = 上傳後刪除舊版）", "int"),
    "savedata_mirror_deletes": ("NAS 刪除存檔時同步刪除 115 上的存檔", "bool"),
    "savedata_cloud_poll_seconds": ("雙向／下載模式：多久檢查一次 115 上的新存檔（秒）", "int"),
    "upload_workers": ("同時上傳數", "int"),
    "hash_workers": ("同時計算 MD5／SHA-1 數", "int"),
    "upload_limit_kb": ("上傳限速 KB/s（0 = 不限）", "int"),
    "download_limit_kb": ("下載限速 KB/s（0 = 不限）", "int"),
    "part_size_mb": ("大檔分片大小（MB，重新啟動後生效）", "int"),
    "proxy": ("代理伺服器（例如 http://127.0.0.1:7890，空白 = 不使用；重新啟動後生效）", "str"),
    "api_min_interval": ("115 介面最小呼叫間隔（秒，避免被限流；重新啟動後生效）", "float"),
    "local_trash_dir": ("NAS 端被覆蓋／刪除的檔案先移到此資料夾（在各同步的 NAS 資料夾下）", "str"),
    "local_trash_days": ("NAS 可復原資料夾保留天數", "int"),
    "ignore_patterns": ("忽略的檔名規則（每行一個，支援 * ?）", "lines"),
}
OPTION_PAGES = [
    ("一般", "options", ["appearance", "_autostart", "manual_conflict", "show_hidden", "browser_path"]),
    ("同步與核對", "sync", ["auto_update_linked", "deletion_policy", "review_same_name_elsewhere", "mass_delete_guard_min",
                       "mass_delete_guard_ratio", "instant_first", "verify_uploads", "keep_versions"]),
    ("即時同步", "clock", ["stable_seconds", "cloud_poll_seconds", "rescan_interval_minutes", "use_polling_observer",
                       "polling_interval_seconds"]),
    ("遊戲存檔", "gamepad", ["savedata_dir_names", "savedata_extensions", "savedata_quiet_seconds",
                         "savedata_keep_versions", "savedata_mirror_deletes", "savedata_cloud_poll_seconds"]),
    ("傳輸", "upload", ["upload_workers", "hash_workers", "upload_limit_kb", "download_limit_kb", "part_size_mb"]),
    ("網路", "proxy", ["proxy", "api_min_interval"]),
    ("NAS 端", "nas", ["local_trash_dir", "local_trash_days", "ignore_patterns"]),
]


class OptionsDialog(_Dialog):
    """選項：左邊是分類，右邊是該分類的設定（與 Air Explorer 的 Options 相同的排法）。"""

    def __init__(self, win):
        super().__init__(win.root, "選項", padding=0)
        self.win, self.config = win, win.config
        s = self.config.settings
        self.vars: dict[str, object] = {}
        main = ttk.Frame(self.body, style="Chrome.TFrame")
        main.pack(fill="both", expand=True)
        self.cats = ttk.Treeview(main, show="tree", selectmode="browse", height=len(OPTION_PAGES))
        self.cats.column("#0", width=px(150), stretch=False)
        self.cats.pack(side="left", fill="y", padx=(px(8), 0), pady=px(8))
        holder = ttk.Frame(main, style="Chrome.TFrame", padding=(px(14), px(10)))
        holder.pack(side="left", fill="both", expand=True)
        self.frames: dict[str, ttk.Frame] = {}
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        for title, icon, names in OPTION_PAGES:
            iid = self.cats.insert("", "end", text=" " + title, image=theme.icon(icon))
            f = ttk.Frame(holder, style="Chrome.TFrame")
            f.columnconfigure(1, weight=1)
            self.frames[iid] = f
            for r, name in enumerate(names):
                self._field(f, r, name, s)
        self.cats.bind("<<TreeviewSelect>>", lambda e: self._show())
        self.current: ttk.Frame | None = None
        bottom = ttk.Frame(self.body, style="Chrome.TFrame", padding=(px(12), px(8)))
        bottom.pack(side="bottom", fill="x")
        theme.line(self.body).pack(side="bottom", fill="x")
        self.buttons(("確定", self._ok, True), ("取消", self.destroy), parent=bottom).pack(fill="x")
        self.cats.selection_set(self.cats.get_children()[0])
        self.center(px(820), px(520))

    def _field(self, f: ttk.Frame, r: int, name: str, s: Settings) -> None:
        pad = (px(4), px(4))
        if name == "_autostart":
            ttk.Checkbutton(f, text="Windows 開機時自動啟動並開始即時同步", variable=self.autostart_var,
                            style="Chrome.TCheckbutton",
                            state="normal" if autostart.supported() else "disabled").grid(
                row=r, column=0, columnspan=3, sticky="w", pady=pad)
            return
        spec = SETTING_FIELDS[name]
        label, typ = spec[0], spec[1]
        val = getattr(s, name)
        if typ == "bool":
            var: object = tk.BooleanVar(value=bool(val))
            ttk.Checkbutton(f, text=label, variable=var, style="Chrome.TCheckbutton").grid(
                row=r, column=0, columnspan=3, sticky="w", pady=pad)
        else:
            ttk.Label(f, text=label, style="Chrome.TLabel", wraplength=px(300), justify="left").grid(
                row=r, column=0, sticky="w", pady=pad, padx=(0, px(10)))
            if typ == "lines":
                var = tk.Text(f, height=9, width=30, font="UIMono")
                var.insert("1.0", "\n".join(val))
                var.grid(row=r, column=1, columnspan=2, sticky="we", pady=pad)
            elif typ == "choice":
                choices = spec[2]
                var = tk.StringVar(value=dict(choices).get(val, val))
                ttk.Combobox(f, textvariable=var, values=[c[1] for c in choices], state="readonly").grid(
                    row=r, column=1, columnspan=2, sticky="we", pady=pad)
            else:
                var = tk.StringVar(value=", ".join(val) if typ == "list" else str(val))
                wide = typ in ("str", "list")
                ttk.Entry(f, textvariable=var, width=30 if wide else 10).grid(
                    row=r, column=1, sticky="we" if wide else "w", pady=pad)
                if name == "browser_path":
                    ttk.Button(f, text="瀏覽…", command=self._pick_browser).grid(row=r, column=2, padx=(px(6), 0))
        self.vars[name] = var

    def _show(self) -> None:
        sel = self.cats.selection()
        if not sel:
            return
        if self.current is not None:
            self.current.pack_forget()
        self.current = self.frames[sel[0]]
        self.current.pack(fill="both", expand=True)

    def show_category(self, title: str) -> None:
        for iid in self.cats.get_children():
            if self.cats.item(iid, "text").strip() == title:
                self.cats.selection_set(iid)

    def _pick_browser(self) -> None:
        p = filedialog.askopenfilename(parent=self, title="選擇 115 瀏覽器（115chrome.exe）",
                                       filetypes=[("程式", "*.exe"), ("全部", "*.*")])
        if p:
            self.vars["browser_path"].set(p)

    def _ok(self) -> None:
        s = self.config.settings
        defaults = Settings()
        values = {}
        try:
            for name, var in self.vars.items():
                spec = SETTING_FIELDS[name]
                typ = spec[1]
                if typ == "bool":
                    val = bool(var.get())
                elif typ == "lines":
                    val = [x.strip() for x in var.get("1.0", "end").splitlines() if x.strip()]
                elif typ == "list":
                    val = [x.strip() for x in var.get().split(",") if x.strip()]
                elif typ == "choice":
                    val = next((k for k, v in spec[2] if v == var.get()), getattr(defaults, name))
                elif typ == "int":
                    val = int(var.get())
                elif typ == "float":
                    val = float(var.get())
                else:
                    val = var.get().strip()
                values[name] = val
        except ValueError as e:
            messagebox.showerror("設定格式錯誤", f"請檢查數字欄位：{e}", parent=self)
            return
        for name, val in values.items():
            setattr(s, name, val)
        self.config.save()
        self.win.engine.transfers.set_limits(s.upload_limit_kb, s.download_limit_kb)
        self.win.explore.hidden_var.set(bool(s.show_hidden))
        if autostart.supported():
            try:
                autostart.set_enabled(self.autostart_var.get())
            except OSError as e:
                messagebox.showwarning("開機自動啟動", f"設定失敗：{e}", parent=self)
        self.result = True
        self.destroy()


# =========================================================================== 書籤、內容、115 資料夾選擇
class BookmarksDialog(_Dialog):
    def __init__(self, win):
        super().__init__(win.root, "編輯書籤")
        self.win = win
        self.tree = make_tree(self.body, [("name", "名稱", 260), ("where", "位置", 110), ("path", "路徑", 320)], height=10,
                              selectmode="browse")
        self.tree.frame.pack(fill="both", expand=True)
        bar = ttk.Frame(self.body, style="Chrome.TFrame")
        bar.pack(fill="x", pady=(px(8), 0))
        ttk.Button(bar, text="重新命名…", command=self.rename).pack(side="left")
        ttk.Button(bar, text="刪除書籤", command=self.delete).pack(side="left", padx=px(6))
        ttk.Button(bar, text="關閉", command=self.destroy, style="Accent.TButton").pack(side="right")
        self.fill()
        self.center(px(740), px(380))

    @property
    def marks(self) -> list:
        return self.win.config.settings.bookmarks

    def fill(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, b in enumerate(self.marks):
            acct = self.win.config.get_account(b.get("account_id", "")) if b.get("kind") == "cloud" else None
            where = "本機" if b.get("kind") == "local" else (acct.label if acct else "115")
            self.tree.insert("", "end", iid=str(i), values=(b.get("name", ""), where, b.get("path", "")))

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.marks[int(sel[0])] if sel else None

    def rename(self) -> None:
        b = self._selected()
        if b is None:
            return
        name = simpledialog.askstring("重新命名書籤", "書籤名稱：", initialvalue=b.get("name", ""), parent=self)
        if name and name.strip():
            b["name"] = name.strip()
            self.win.config.save()
            self.fill()

    def delete(self) -> None:
        b = self._selected()
        if b is not None:
            self.marks.remove(b)
            self.win.config.save()
            self.fill()


class PropertiesDialog(_Dialog):
    """檔案或資料夾的內容；本機檔案會計算 MD5 與 SHA-1（方便與 115 上的 SHA-1 比對）。"""

    def __init__(self, win, panel, item):
        super().__init__(win.root, f"{item.name} - 內容")
        self.win = win
        b = self.body
        b.columnconfigure(1, weight=1)
        local = panel.source.kind == "local"
        head = ttk.Frame(b, style="Chrome.TFrame")
        head.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, px(8)))
        _label(head, "", image=theme.icon(item.icon, 32) if item.icon else theme.icon(
            "folder" if item.is_dir else icons.file_icon_name(item.name), 32)).pack(side="left", padx=(0, px(10)))
        _label(head, item.name, "Chrome.Big.TLabel").pack(side="left")
        base = panel.source.path_text(panel.place)
        location = item.key if local else (base.rstrip("/") + "/" + item.name)
        rows = [("類型", "資料夾" if item.is_dir else "檔案"),
                ("位置", ("本機" if local else panel.source.title + "（115 網盤）")),
                ("路徑", location)]
        if not item.is_dir:
            rows.append(("大小", f"{human_size(item.size)}（{item.size:,} 位元組）"))
        rows.append(("修改日期" if local else "上傳時間", fmt_time(item.mtime)))
        self.fields: dict[str, tk.StringVar] = {}
        if not item.is_dir:
            rows += [("MD5", "計算中…" if local else "（115 不提供 MD5）"), ("SHA-1", "計算中…" if local else item.sha1)]
        if not local:
            rows.append(("115 檔案 ID", item.key))
        for r, (label, value) in enumerate(rows, 1):
            _label(b, label, "Chrome.Secondary.TLabel").grid(row=r, column=0, sticky="nw", pady=px(3), padx=(0, px(12)))
            var = tk.StringVar(value=value)
            e = ttk.Entry(b, textvariable=var, state="readonly", font="UIMono" if label in ("MD5", "SHA-1") else "UI")
            e.grid(row=r, column=1, sticky="we", pady=px(3))
            self.fields[label] = var
        self.buttons(("關閉", self.destroy, True)).grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e",
                                                     pady=(px(12), 0))
        self.center(px(560), px(300))
        if local and not item.is_dir:
            threading.Thread(target=self._hash, args=(item.key,), daemon=True).start()

    def _hash(self, path: str) -> None:
        try:
            h, _ = self.win.engine.hash_cached(path)
            md5, sha1 = h.md5, h.sha1
        except Exception as e:  # noqa: BLE001
            md5 = sha1 = f"無法讀取：{e}"
        self.win.ui.call(self._show_hash, md5, sha1)

    def _show_hash(self, md5: str, sha1: str) -> None:
        if self.winfo_exists() and self.fields:
            self.fields["MD5"].set(md5)
            self.fields["SHA-1"].set(sha1)


class CloudFolderDialog(_Dialog):
    """選擇 115 上的資料夾（可直接輸入路徑，不存在的資料夾會在使用時建立）。結果是路徑文字。"""

    def __init__(self, win, account_id: str, title: str = "選擇 115 資料夾", start: str = "/", parent=None):
        super().__init__(parent or win.root, title)
        self.win, self.client = win, win.cloud(account_id)
        b = self.body
        acct = win.config.get_account(account_id)
        _label(b, f"{acct.label if acct else '115'}（115 網盤）", image=theme.icon("cloud"), compound="left").pack(anchor="w")
        frame = ttk.Frame(b)
        frame.pack(fill="both", expand=True, pady=px(6))
        self.tree = ttk.Treeview(frame, show="tree", selectmode="browse", height=12)
        sb = AutoScrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row = ttk.Frame(b, style="Chrome.TFrame")
        row.pack(fill="x")
        _label(row, "資料夾").pack(side="left")
        self.path = tk.StringVar(value=cloud_norm(start or "/"))
        ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True, padx=px(6))
        ttk.Button(row, text="新資料夾…", command=self._new_folder).pack(side="left")
        self.status = _label(b, "", "Chrome.Secondary.TLabel")
        self.status.pack(anchor="w", pady=(px(4), 0))
        self.buttons(("確定", self._ok, True), ("取消", self.destroy)).pack(fill="x", pady=(px(8), 0))
        self.nodes: dict[str, tuple[str, str]] = {}  # iid -> (cid, 路徑)
        root = self.tree.insert("", "end", text=" /", image=theme.icon("cloud"), open=True)
        self.nodes[root] = (self.client.root_cid, "/")
        self.tree.bind("<<TreeviewOpen>>", lambda e: self._load(self.tree.focus()))
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Double-Button-1>", lambda e: None)
        self.center(px(480), px(460))
        self._load(root)

    def _select(self, _event=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0] in self.nodes:
            self.path.set(self.nodes[sel[0]][1])

    def _load(self, iid: str) -> None:
        if iid not in self.nodes:
            return
        cid, path = self.nodes[iid]
        self.status.configure(text="讀取中…")

        def work() -> None:
            try:
                dirs = [e for e in self.client.list_dir(cid) if e.is_dir]
                self.win.ui.call(self._fill, iid, path, dirs, None)
            except Exception as e:  # noqa: BLE001
                self.win.ui.call(self._fill, iid, path, [], e)

        threading.Thread(target=work, daemon=True).start()

    def _fill(self, iid: str, path: str, dirs, err) -> None:
        if not self.winfo_exists() or not self.tree.exists(iid):
            return
        self.status.configure(text=f"讀取失敗：{err}" if err else "")
        for kid in self.tree.get_children(iid):
            self.nodes.pop(kid, None)
            self.tree.delete(kid)
        for e in sorted(dirs, key=lambda d: d.name.casefold()):
            kid = self.tree.insert(iid, "end", text=" " + e.name, image=theme.icon("folder"))
            self.nodes[kid] = (e.fid, cloud_join(path, e.name))
            self.tree.insert(kid, "end", iid=kid + "_x", text="")
        self.tree.item(iid, open=True)

    def _new_folder(self) -> None:
        name = simpledialog.askstring("新資料夾", "115 資料夾名稱：", parent=self)
        name = (name or "").strip()
        if not name:
            return
        if why := unsafe_name(name):
            messagebox.showerror("名稱不能使用", why, parent=self)
            return
        self.path.set(cloud_join(self.path.get() or "/", name))

    def _ok(self) -> None:
        self.result = cloud_norm(self.path.get().strip() or "/")
        self.destroy()


# =========================================================================== 排程工作
class ScheduleDialog(_Dialog):
    """排程工作（比照 Air Explorer 的「Scheduled Task」：設定／狀態 兩個分頁）。"""

    def __init__(self, win, schedule: dict | None = None, mapping_ids=None):
        super().__init__(win.root, "排程工作")
        self.win = win
        s = schedule or {}
        nb = ttk.Notebook(self.body)
        nb.pack(fill="both", expand=True)
        conf = ttk.Frame(nb, padding=px(12))
        nb.add(conf, text="設定")
        conf.columnconfigure(1, weight=1)
        ttk.Label(conf, text="名稱").grid(row=0, column=0, sticky="w", pady=px(4))
        self.name = tk.StringVar(value=s.get("name", ""))
        ttk.Entry(conf, textvariable=self.name).grid(row=0, column=1, sticky="we", pady=px(4), padx=(px(8), 0))
        ttk.Label(conf, text="操作").grid(row=1, column=0, sticky="w", pady=px(4))
        self.action = ttk.Combobox(conf, state="readonly", values=["完整比對＋同步", "只比對"], width=18)
        self.action.set("只比對" if s.get("action") == "scan" else "完整比對＋同步")
        self.action.grid(row=1, column=1, sticky="w", pady=px(4), padx=(px(8), 0))

        scope = ttk.LabelFrame(conf, text="範圍")
        scope.grid(row=2, column=0, columnspan=2, sticky="we", pady=px(6))
        chosen = set(s.get("mapping_ids") or mapping_ids or [])
        self.all_var = tk.BooleanVar(value=not chosen)
        ttk.Checkbutton(scope, text="所有已核對的同步", variable=self.all_var, command=self._scope).pack(anchor="w")
        self.map_vars: dict[str, tk.BooleanVar] = {}
        self._map_checks = []
        for m in win.config.mappings:
            v = tk.BooleanVar(value=m.id in chosen)
            c = ttk.Checkbutton(scope, text=f"{m.title}（{m.local_path} ⇄ {m.cloud_path}）", variable=v)
            c.pack(anchor="w", padx=(px(18), 0))
            self.map_vars[m.id] = v
            self._map_checks.append(c)

        when = ttk.LabelFrame(conf, text="排程")
        when.grid(row=3, column=0, columnspan=2, sticky="we", pady=px(6))
        row = ttk.Frame(when)
        row.pack(fill="x")
        ttk.Label(row, text="時間（HH:MM）").pack(side="left")
        self.time = tk.StringVar(value=s.get("time", "03:00"))
        ttk.Entry(row, textvariable=self.time, width=7).pack(side="left", padx=px(8))
        days = ttk.Frame(when)
        days.pack(fill="x", pady=(px(6), 0))
        ttk.Label(days, text="星期").pack(side="left", padx=(0, px(8)))
        on = set(s.get("days", range(7)))
        self.day_vars = []
        for i, d in enumerate(WEEKDAYS):
            v = tk.BooleanVar(value=i in on)
            ttk.Checkbutton(days, text=d, variable=v).pack(side="left", padx=(0, px(8)))
            self.day_vars.append(v)
        self.enabled = tk.BooleanVar(value=s.get("enabled", True))
        ttk.Checkbutton(conf, text="啟用", variable=self.enabled).grid(row=4, column=0, columnspan=2, sticky="w")
        wrap_label(conf, "排程在本程式執行中才會動作（可在「選項」勾選開機自動啟動）。").grid(
            row=5, column=0, columnspan=2, sticky="we", pady=(px(6), 0))

        status = ttk.Frame(nb, padding=px(12))
        nb.add(status, text="狀態")
        nxt = next_run(s) if schedule else None
        for r, (label, value) in enumerate((
                ("排程", describe_when(s) if schedule else "（尚未建立）"),
                ("下次執行", fmt_date(nxt) if nxt else "—"),
                ("上次執行", fmt_time(s.get("last_run")) or "尚未執行"),
                ("上次結果", s.get("last_report", "") or "—"))):
            ttk.Label(status, text=label, style="Secondary.TLabel").grid(row=r, column=0, sticky="nw", pady=px(4))
            ttk.Label(status, text=value, wraplength=px(420), justify="left").grid(row=r, column=1, sticky="w",
                                                                                 padx=(px(12), 0), pady=px(4))
        self.buttons(("確定", self._ok, True), ("取消", self.destroy)).pack(fill="x", pady=(px(10), 0))
        self._scope()
        self.center(px(600), px(480))

    def _scope(self) -> None:
        for c in self._map_checks:
            c.state(["disabled"] if self.all_var.get() else ["!disabled"])

    def _ok(self) -> None:
        t = self.time.get().strip()
        try:
            hhmm = time.strftime("%H:%M", time.strptime(t, "%H:%M"))
        except ValueError:
            messagebox.showerror("時間格式錯誤", "請輸入 HH:MM，例如 03:00", parent=self)
            return
        days = [i for i, v in enumerate(self.day_vars) if v.get()]
        if not days:
            messagebox.showerror("請選擇星期", "至少要勾選一天", parent=self)
            return
        ids = [] if self.all_var.get() else [mid for mid, v in self.map_vars.items() if v.get()]
        self.result = {"name": self.name.get().strip(), "time": hhmm, "days": days,
                       "action": "scan" if self.action.get() == "只比對" else "sync", "mapping_ids": ids,
                       "enabled": bool(self.enabled.get())}
        self.destroy()


def schedule_from(result: dict, old: dict | None = None) -> dict:
    s = dict(old) if old else new_schedule()
    s.update(result)
    return s


# =========================================================================== 「同步」視窗（比較結果）
ACTIONS = {  # 動作 → (圖示, 說明)
    "upload": ("to_right", "複製到 115（上傳，先秒傳）"), "download": ("to_left", "複製到 NAS（下載）"),
    "del_cloud": ("del_right", "刪除 115 上的檔案"), "del_local": ("del_left", "刪除 NAS 上的檔案"),
    "review": ("question", "待本人核對"), "only_cloud": ("only_right", "僅 115 有（不處理）"),
    "only_local": ("only_left", "僅 NAS 有（不處理）"), "equal": ("equal", "兩邊相同"),
    "unreadable": ("skipped", "暫時無法讀取"),
}
ACTION_FILTERS = [("upload", "複製到 115"), ("download", "複製到 NAS"), ("delete", "刪除"), ("review", "待核對"),
                  ("only", "僅一邊有"), ("equal", "相同")]
STATE_FILTERS = [("queued", "排隊中"), ("done", "已完成"), ("failed", "失敗"), ("none", "不處理")]


class SyncWindow(tk.Toplevel):
    """一組同步的比較結果：左邊 NAS、右邊 115，中間是每個檔案要做的動作（比照 Air Explorer 的 Synchronize 視窗）。"""

    def __init__(self, win, mapping):
        super().__init__(win.root)
        self.win, self.mapping = win, mapping
        self.title(f"{mapping.title} - 同步")
        self.configure(background=theme.P.bg)
        self.plan = None
        self.rows: list[tuple] = []        # (鍵, 動作, 相對路徑, 說明)
        self.shown: list[tuple] = []
        self.status: dict[str, tuple[str, str]] = {}  # 鍵 → (狀態, 文字)
        self.local: dict = {}
        self.cloud: dict = {}
        self.action_on = {k: tk.BooleanVar(self, value=k != "equal") for k, _ in ACTION_FILTERS}
        self.state_on = {k: tk.BooleanVar(self, value=True) for k, _ in STATE_FILTERS}
        self.scroll_var = tk.BooleanVar(self, value=True)
        self.search = tk.StringVar(self)
        self._build()
        theme.apply_titlebar(self)
        w, h = px(1120), px(640)
        r = win.root
        self.geometry(f"{w}x{h}+{r.winfo_rootx() + px(40)}+{r.winfo_rooty() + px(40)}")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        plan = win.sync_page.plans.get(mapping.id)
        if plan is not None:
            self.load_plan(plan)
        else:
            self._summary("按「比較」讀取 NAS 與 115，列出兩邊的差異（不會傳輸）。")

    # ------------------------------------------------------------ 介面
    def _build(self) -> None:
        m, p = self.mapping, theme.P
        acct = self.win.config.get_account(m.account_id)
        top = ttk.Frame(self, padding=(px(6), px(6), px(6), px(2)))
        top.pack(fill="x")
        top.columnconfigure(0, weight=1, uniform="side")
        top.columnconfigure(2, weight=1, uniform="side")
        for col, icon, text, cmd in ((0, "computer", m.local_path, self.open_local),
                                     (2, "cloud", f"{acct.label if acct else '115'}　{m.cloud_path}", self.open_cloud)):
            box = tk.Frame(top, background=p.chrome, highlightthickness=1, highlightbackground=p.line)
            box.grid(row=0, column=col, sticky="we")
            tk.Label(box, image=theme.icon(icon), background=p.chrome).pack(side="left", padx=px(4))
            tk.Label(box, text=text, background=p.chrome, foreground=p.text, font="UI", anchor="w").pack(
                side="left", fill="x", expand=True, pady=px(3))
            tool_button(box, "open", command=cmd, style="Chrome.Tool.TButton",
                        hint="開啟這個資料夾").pack(side="right")
        self.dir_icon = ttk.Label(top, image=theme.icon(DIRECTION_ICON[m.direction]))
        self.dir_icon.grid(row=0, column=1, padx=px(8))

        bar = ttk.Frame(self, padding=(px(6), 0))
        bar.pack(fill="x")
        self.btn_conf = tool_button(bar, "wrench", "", self.configure_sync, hint="同步方式、刪除處理、篩選")
        self.btn_conf.pack(side="left")
        self.btn_confirm = tool_button(bar, "shield", "核對確認…", self.confirm, hint="由本人核對 NAS 與 115 的路徑")
        self.btn_confirm.pack(side="left")
        theme.line(bar, vertical=True).pack(side="left", fill="y", padx=px(4), pady=px(3))
        self.btn_compare = tool_button(bar, "compare", "比較", self.compare, hint="讀取兩邊並列出差異（不傳輸）")
        self.btn_compare.pack(side="left")
        self.btn_start = tool_button(bar, "start", "開始", self.start, hint="執行列出的動作（上傳先嘗試秒傳）")
        self.btn_start.pack(side="left")
        tool_button(bar, "export", "匯出 CSV…", self.export, hint="把比較結果匯出成 CSV").pack(side="left")
        tool_button(bar, "search", command=lambda: self.apply_filters(), hint="依路徑篩選").pack(side="right")
        e = ttk.Entry(bar, textvariable=self.search, width=18)
        e.pack(side="right", padx=(px(4), 0))
        e.bind("<Return>", lambda ev: self.apply_filters())
        self.btn_state = tool_button(bar, "filter", "狀態篩選 ▾", lambda: popup_below(self.btn_state, self._filter_items(
            STATE_FILTERS, self.state_on)))
        self.btn_state.pack(side="right")
        self.btn_action = tool_button(bar, "filter", "動作篩選 ▾", lambda: popup_below(self.btn_action, self._filter_items(
            ACTION_FILTERS, self.action_on)))
        self.btn_action.pack(side="right")
        Toggle(bar, self.scroll_var, "chev_down", "自動捲動", hint="同步時自動捲到正在處理的檔案").pack(side="right")
        theme.line(self).pack(fill="x", pady=(px(2), 0))

        bottom = ttk.Frame(self, style="Chrome.TFrame", padding=(px(8), px(4)))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="關閉", command=self.destroy).pack(side="right")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=px(150))
        self.progress.pack(side="right", padx=px(8))
        self.summary = ttk.Label(bottom, text="", style="Chrome.TLabel")
        self.summary.pack(side="left")
        theme.line(self).pack(side="bottom", fill="x")

        cols = [Column("lp", "路徑（NAS）", 300, stretch=True), Column("ls", "大小", 84, anchor="e"),
                Column("ld", "日期", 128), Column("act", "", 40, "icon", "center"),
                Column("rp", "路徑（115）", 300), Column("rs", "大小", 84, anchor="e"), Column("rd", "日期", 128),
                Column("st", "狀態", 190)]
        self.grid_list = GridList(self, cols, render=self._render, row_id=lambda r: r[0], on_open=self._open_row,
                                  on_menu=self._menu)
        self.grid_list.pack(fill="both", expand=True)
        self._update_buttons()

    def _update_buttons(self) -> None:
        m = self.mapping
        self.btn_conf.configure(text=f"設定－{DIRECTION_LABELS[m.direction].split('（')[0]}")
        self.dir_icon.configure(image=theme.icon(DIRECTION_ICON[m.direction]))
        if m.confirmed:
            self.btn_confirm.configure(text="已核對" if m.enabled else "已核對（停用中）", image=theme.icon_pair("ok"))
        else:
            self.btn_confirm.configure(text="核對確認…", image=theme.icon_pair("shield"))
        todo = self.plan is not None and any(r[1] in ("upload", "download", "del_cloud", "del_local") for r in self.rows)
        self.btn_start.state(["!disabled"] if todo else ["disabled"])

    def _filter_items(self, defs, variables) -> list:
        def flip(key: str) -> None:
            variables[key].set(not variables[key].get())
            self.apply_filters()

        return [{"label": label, "check": variables[key].get(), "command": lambda k=key: flip(k)} for key, label in defs]

    def _summary(self, text: str) -> None:
        self.summary.configure(text=text)

    # ------------------------------------------------------------ 資料
    def load_plan(self, plan) -> None:
        """把比對結果攤成每個檔案一列。"""
        m, db = self.mapping, self.win.db
        self.plan = plan
        self.local = {r.rel_key: r for r in db.local_files(m.id)}
        cloud: dict = {}
        for c in db.cloud_files(m.id):
            cloud.setdefault(c.rel_key, c)
        self.cloud = cloud
        rows: list[tuple] = []
        seen: set[str] = set()

        def add(action: str, rel: str, note: str = "") -> None:
            key = norm_key(rel)
            if (key, action) not in seen:
                seen.add((key, action))
                seen.add(key)
                rows.append((key, action, rel, note))

        reasons = {"new": "新檔案", "modified": "已修改", "savedata": "遊戲存檔", "review": "本人核對後", "retry": "重試",
                   "restore": "還原"}
        for u in plan.uploads:
            add("upload", u.rel_path, reasons.get(u.reason, u.reason))
        for d in plan.downloads:
            add("download", d.rel_path, reasons.get(d.reason, d.reason))
        for d in plan.deletes:
            add("del_cloud", d.rel_path, d.reason)
        for d in plan.local_deletes:
            add("del_local", d.rel_path, d.reason)
        for r in plan.reviews:
            add("review", r.rel_path or "（整個同步）", KIND_LABELS.get(r.kind, r.kind))
        for c in plan.cloud_only:
            add("only_cloud", c.rel_path)
        for rel in plan.local_only:
            add("only_local", rel)
        for rel in plan.unreadable:
            add("unreadable", rel)
        for key in sorted(self.local.keys() & self.cloud.keys()):
            if key not in seen:
                rows.append((key, "equal", self.local[key].rel_path, ""))
        self.rows = rows
        self.status = {}
        self.apply_filters()
        self._update_buttons()
        s = plan.summary()
        text = (f"{s['uploads'] + s['downloads'] + s['deletes'] + s['local_deletes']} 項同步工作，"
                f"{human_size(s['upload_bytes'] + s['download_bytes'])}；待核對 {s['reviews']} 項；已相同 {s['identical']} 個")
        if plan.warnings:
            text += "　⚠ " + "；".join(plan.warnings)
        self._summary(text)

    def _state_of(self, row: tuple) -> str:
        key, action = row[0], row[1]
        if key in self.status:
            return self.status[key][0]
        return "queued" if action in ("upload", "download", "del_cloud", "del_local") else "none"

    def apply_filters(self) -> None:
        on_action = {k for k, v in self.action_on.items() if v.get()}
        on_state = {k for k, v in self.state_on.items() if v.get()}
        group = {"upload": "upload", "download": "download", "del_cloud": "delete", "del_local": "delete",
                 "review": "review", "only_cloud": "only", "only_local": "only", "equal": "equal",
                 "unreadable": "only"}
        text = norm_key(self.search.get().strip())
        self.shown = [r for r in self.rows if group[r[1]] in on_action and self._state_of(r) in on_state
                      and (not text or text in r[0])]
        self.grid_list.set_rows(self.shown)

    def _render(self, row: tuple) -> dict:
        key, action, rel, note = row
        lf, cf = self.local.get(key), self.cloud.get(key)
        state, text = self.status.get(key, ("", ""))
        if not text:
            text = {"upload": "排隊中", "download": "排隊中", "del_cloud": "排隊中", "del_local": "排隊中"}.get(action, "")
            text = (text + ("（" + note + "）" if note and text else "")) or (ACTIONS[action][1] if action != "review" else f"待核對：{note}")
        p = theme.P
        fg = {"failed": p.danger, "done": p.ok}.get(state) or (p.warn if action == "review" else
                                                               p.text2 if action in ("equal", "only_cloud", "only_local") else None)
        return {"fg": fg, "cells": {
            "lp": rel if lf is not None or action in ("upload", "only_local", "del_local", "unreadable", "review") else "",
            "ls": human_size(lf.size) if lf is not None else "",
            "ld": fmt_date(lf.mtime_ns / 1e9) if lf is not None else "",
            "act": ACTIONS[action][0],
            "rp": rel if cf is not None or action in ("download", "only_cloud", "del_cloud") else "",
            "rs": human_size(cf.size) if cf is not None else "",
            "rd": fmt_date(cf.mtime) if cf is not None else "",
            "st": text}}

    # ------------------------------------------------------------ 動作
    def configure_sync(self) -> None:
        self.win.sync_page.edit(self.mapping, parent=self)
        self._update_buttons()

    def confirm(self) -> bool:
        if self.mapping.confirmed:
            return True
        self.win.sync_page.confirm(self.mapping, then=self._update_buttons)
        return False

    def compare(self) -> None:
        m = self.mapping
        if not m.confirmed:
            messagebox.showinfo("需要先核對", "這組同步還沒有由本人核對路徑，請先按「核對確認」。", parent=self)
            self.confirm()
            return
        if not m.enabled:
            messagebox.showinfo("已停用", "這組同步目前是停用狀態，請先在「同步」頁啟用。", parent=self)
            return
        self._summary("比較中…")

        def done(plans) -> None:
            plan = next((p for p in plans if p.mapping_id == m.id), None)
            if plan is not None and self.winfo_exists():
                self.load_plan(plan)
                self.progress.configure(value=0)

        self.win.scan([m], on_done=done)

    def start(self) -> None:
        if self.plan is None:
            return

        def done(_stats) -> None:
            if self.winfo_exists():
                self.plan = None
                self.progress.configure(value=0)
                self._update_buttons()
                self._summary(self.summary.cget("text") + "　— 同步完成")

        self.win.execute([self.plan], on_done=done)

    def export(self) -> None:
        if self.plan is None:
            messagebox.showinfo("尚未比較", "請先按「比較」", parent=self)
            return
        from ..manual import export_plans_csv

        path = filedialog.asksaveasfilename(parent=self, title="匯出比較結果", defaultextension=".csv",
                                            initialfile=time.strftime("比對報告_%Y%m%d-%H%M.csv"),
                                            filetypes=[("CSV（Excel 可開）", "*.csv")])
        if path:
            n = export_plans_csv(self.win.engine, [self.plan], path)
            self._summary(f"已匯出 {n} 筆到 {path}")

    def show_progress(self, stage: str, done: int, total: int, _text: str) -> None:
        self.progress.configure(maximum=max(1, total), value=done if total else 0)

    def on_event(self, ev: str, data: dict) -> None:
        """同步進行中：更新每個檔案的狀態。"""
        if data.get("mapping_id") != self.mapping.id:
            return
        key = norm_key(data.get("rel_path", ""))
        if ev == "uploaded":
            self.status[key] = ("done", "秒傳完成" if data.get("instant") else "已上傳")
        elif ev == "downloaded":
            self.status[key] = ("done", "已從本機複製" if data.get("copied") else "已下載")
        elif ev in ("deleted", "deleted_local"):
            self.status[key] = ("done", "已刪除")
        elif ev == "failed":
            self.status[key] = ("failed", f"失敗：{data.get('error', '')}")
        self.grid_list.refresh()
        if self.scroll_var.get():
            self.grid_list.see(key)

    def _selected_row(self) -> tuple | None:
        sel = self.grid_list.selected_rows()
        return sel[0] if sel else None

    def open_local(self, rel: str = "") -> None:
        open_local(local_join(self.mapping.local_path, rel) if rel else self.mapping.local_path)

    def open_cloud(self, rel: str = "") -> None:
        m = self.mapping
        c = self.win.engine.cloud_for(m)
        cf = self.cloud.get(norm_key(rel)) if rel else None
        cid = cf.pid if cf is not None else (m.cloud_cid or None)
        if cid:
            self.win.open_cloud_path(m.account_id, cid)
        else:
            self.win.run_bg("開啟 115", lambda: c.resolve_path(m.cloud_path),
                            lambda found: found and self.win.open_cloud_path(m.account_id, found))

    def _open_row(self, row: tuple) -> None:
        if row[1] == "review":
            self.win.show_page("reviews")
            self.win.reviews_page.select_path(self.mapping.id, row[2])
            self.win.root.lift()

    def menu_items(self) -> list:
        row = self._selected_row()
        rel = row[2] if row else ""
        has = row is not None
        return [{"label": "開啟 NAS 位置", "icon": "computer", "enabled": has and row[0] in self.local,
                 "command": lambda: self.open_local(rel)},
                {"label": "在 115 瀏覽器開啟", "icon": "globe", "enabled": has and row[0] in self.cloud,
                 "command": lambda: self.open_cloud(rel)},
                None,
                {"label": "前往待核對", "icon": "reviews", "enabled": has and row[1] == "review",
                 "command": lambda: self._open_row(row)},
                {"label": "複製路徑", "icon": "copy", "enabled": has, "command": lambda: self._copy(rel)}]

    def _copy(self, rel: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(rel)

    def _menu(self, event, _row) -> None:
        popup(self.grid_list.canvas, self.menu_items(), event.x_root, event.y_root)
