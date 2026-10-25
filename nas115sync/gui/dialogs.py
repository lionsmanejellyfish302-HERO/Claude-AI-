"""對話框：新增對應、自動建議、本人核對路徑、QR 碼登入。"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ..app import open_in_115_browser
from ..mapping_check import inspect_mapping, suggest_mappings
from ..paths import cloud_norm
from .util import open_local, scrolled_text, set_text, tree_with_scroll


class _Dialog(tk.Toplevel):
    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def center(self, w: int, h: int) -> None:
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + max(0, (p.winfo_width() - w) // 2)
        y = p.winfo_rooty() + max(0, (p.winfo_height() - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def modal(self):
        self.grab_set()
        self.wait_window()
        return self.result


class AddMappingDialog(_Dialog):
    def __init__(self, parent):
        super().__init__(parent, "新增路徑對應")
        b = self.body
        ttk.Label(b, text="NAS 資料夾（例如 \\\\NAS\\home\\Photos 或 Z:\\Photos）：").grid(row=0, column=0, sticky="w")
        self.local = tk.StringVar()
        ttk.Entry(b, textvariable=self.local, width=60).grid(row=1, column=0, sticky="we")
        ttk.Button(b, text="瀏覽…", command=self._browse).grid(row=1, column=1, padx=4)
        ttk.Label(b, text="115 資料夾路徑（例如 /NAS備份/Photos）：").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.cloud = tk.StringVar()
        ttk.Entry(b, textvariable=self.cloud, width=60).grid(row=3, column=0, sticky="we")
        self.savedata = tk.BooleanVar()
        ttk.Checkbutton(b, text="整個資料夾都是遊戲存檔（每個子資料夾為一個遊戲）", variable=self.savedata).grid(
            row=4, column=0, sticky="w", pady=8)
        ttk.Label(b, text="新增後狀態為「待核對」，需按「核對確認」由本人確認後才會同步。",
                  foreground="#a05000").grid(row=5, column=0, columnspan=2, sticky="w")
        bar = ttk.Frame(b)
        bar.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(bar, text="新增", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        b.columnconfigure(0, weight=1)
        self.center(640, 260)

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="選擇 NAS 資料夾")
        if d:
            self.local.set(d.replace("/", "\\") if "\\" in d or ":" in d else d)

    def _ok(self):
        if not self.local.get().strip() or not self.cloud.get().strip():
            messagebox.showwarning("缺少資料", "請填寫 NAS 資料夾與 115 路徑", parent=self)
            return
        self.result = (self.local.get().strip(), cloud_norm(self.cloud.get().strip()), self.savedata.get())
        self.destroy()


class SuggestDialog(_Dialog):
    """把 NAS 根目錄下所有資料夾依名稱配對到 115，勾選後加入為「待核對」。"""

    def __init__(self, parent, cloud, ui):
        super().__init__(parent, "自動建議路徑對應（NAS 所有資料夾）")
        self.cloud = cloud
        self.ui = ui
        b = self.body
        row = ttk.Frame(b)
        row.pack(fill="x")
        ttk.Label(row, text="NAS 根目錄：").pack(side="left")
        self.nas_root = tk.StringVar()
        ttk.Entry(row, textvariable=self.nas_root, width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="瀏覽…", command=self._browse).pack(side="left", padx=4)
        row2 = ttk.Frame(b)
        row2.pack(fill="x", pady=6)
        ttk.Label(row2, text="115 根目錄：").pack(side="left")
        self.cloud_root = tk.StringVar(value="/NAS備份")
        ttk.Entry(row2, textvariable=self.cloud_root, width=40).pack(side="left", fill="x", expand=True)
        self.btn = ttk.Button(row2, text="產生建議", command=self._suggest)
        self.btn.pack(side="left", padx=4)
        self.tree = tree_with_scroll(b, [("local", "NAS 資料夾", 320), ("cloud", "115 資料夾", 280),
                                         ("state", "115 狀態", 110)], height=14)
        self.tree.frame.pack(fill="both", expand=True)
        self.status = ttk.Label(b, text="選取要加入的項目（可多選），加入後仍需逐一「核對確認」。")
        self.status.pack(anchor="w", pady=4)
        bar = ttk.Frame(b)
        bar.pack(anchor="e")
        ttk.Button(bar, text="全選", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(
            side="left", padx=4)
        ttk.Button(bar, text="加入選取項目", command=self._ok).pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left")
        self.items = {}
        self.center(780, 520)

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="選擇 NAS 根目錄")
        if d:
            self.nas_root.set(d)

    def _suggest(self):
        nas, root = self.nas_root.get().strip(), cloud_norm(self.cloud_root.get().strip() or "/")
        if not nas:
            return
        self.btn.configure(state="disabled")
        self.status.configure(text="讀取中…")

        def work():
            try:
                res = suggest_mappings(nas, root, self.cloud)
                self.ui.call(self._show, res, None)
            except Exception as e:  # noqa: BLE001
                self.ui.call(self._show, [], e)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, res, err):
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

    def _ok(self):
        self.result = [self.items[i] for i in self.tree.selection() if i in self.items]
        self.destroy()


class ConfirmMappingDialog(_Dialog):
    """本人核對：同時列出 NAS 與 115 兩邊的內容，勾選「我已核對」後才可確認。"""

    def __init__(self, parent, mapping, cloud, mappings, ui, browser_path: str = ""):
        super().__init__(parent, "核對路徑對應")
        self.m, self.cloud, self.ui, self.browser_path = mapping, cloud, ui, browser_path
        self.info = None
        b = self.body
        ttk.Label(b, text="請核對 NAS 與 115 兩邊的路徑與內容是否為同一份資料：", font=("", 11, "bold")).pack(anchor="w")
        self.text = scrolled_text(b, height=22, wrap="char")
        self.text.frame.pack(fill="both", expand=True, pady=6)
        set_text(self.text, "讀取 NAS 與 115 內容中…")
        self.create = tk.BooleanVar(value=False)
        self.create_holder = ttk.Frame(b)  # 115 上沒有資料夾時才顯示「建立」選項
        self.create_holder.pack(fill="x")
        self.checked = tk.BooleanVar(value=False)
        ttk.Checkbutton(b, text="我已本人核對：NAS 與 115 路徑對應正確", variable=self.checked,
                        command=self._update).pack(anchor="w")
        bar = ttk.Frame(b)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="開啟 NAS 資料夾", command=lambda: open_local(self.m.local_path)).pack(side="left")
        self.open115 = ttk.Button(bar, text="在 115 瀏覽器開啟", command=self._open115, state="disabled")
        self.open115.pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="right")
        self.ok = ttk.Button(bar, text="確認對應", command=self._ok, state="disabled")
        self.ok.pack(side="right", padx=4)
        self.center(760, 620)
        threading.Thread(target=self._load, args=(mappings,), daemon=True).start()

    def _load(self, mappings):
        try:
            info = inspect_mapping(self.m, self.cloud, mappings)
            self.ui.call(self._loaded, info, None)
        except Exception as e:  # noqa: BLE001
            self.ui.call(self._loaded, None, e)

    def _loaded(self, info, err):
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
                            variable=self.create, command=self._update).pack(anchor="w")
        self._update()

    def _update(self):
        ok = (self.info is not None and self.info.local_ok and self.checked.get()
              and (self.info.cloud_cid is not None or (not self.info.cloud_error and self.create.get())))
        self.ok.configure(state="normal" if ok else "disabled")

    def _open115(self):
        if self.info and self.info.cloud_cid is not None:
            open_in_115_browser(self.cloud.web_url(self.info.cloud_cid), self.browser_path)

    def _ok(self):
        self.result = {"cid": self.info.cloud_cid, "create": self.info.cloud_cid is None}
        self.destroy()


class QrLoginDialog(_Dialog):
    def __init__(self, parent, cloud, ui):
        super().__init__(parent, "115 掃碼登入")
        self.cloud, self.ui = cloud, ui
        self._closed = threading.Event()
        b = self.body
        ttk.Label(b, text="請開啟手機 115 App 掃描 QR 碼，並在手機上確認授權。").pack()
        self.canvas = tk.Canvas(b, width=300, height=300, bg="white", highlightthickness=0)
        self.canvas.pack(pady=10)
        self.status = ttk.Label(b, text="取得 QR 碼中…")
        self.status.pack()
        ttk.Button(b, text="取消", command=self.close).pack(pady=(8, 0))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.center(380, 440)
        threading.Thread(target=self._run, daemon=True).start()

    def close(self):
        self._closed.set()
        self.destroy()

    def _draw(self, text: str):
        if not self.winfo_exists():
            return
        try:
            import qrcode
        except ImportError:
            self.canvas.create_text(150, 150, text=text, width=280)
            return
        qr = qrcode.QRCode(border=2)
        qr.add_data(text)
        qr.make(fit=True)
        mat = qr.get_matrix()
        scale = max(2, 300 // len(mat))
        size = scale * len(mat)
        self.canvas.configure(width=size, height=size)
        for y, row in enumerate(mat):
            for x, v in enumerate(row):
                if v:
                    self.canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale,
                                                 fill="black", outline="black")

    def _set(self, msg: str):
        if self.winfo_exists():
            self.status.configure(text=msg)

    def _run(self):
        from ..cloud.open115 import QR_CONFIRMED, QR_SCANNED

        try:
            login = self.cloud.start_qr_login()
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

    def _done(self):
        if self.winfo_exists():
            self.destroy()
