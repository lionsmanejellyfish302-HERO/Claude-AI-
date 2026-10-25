"""雙窗格瀏覽器（類似 Air Explorer）：左邊 NAS、右邊 115，可上傳、下載、新增資料夾、改名、刪除、搬移、搜尋、比較。"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import manual
from ..app import open_in_115_browser
from ..cloud.base import CloudEntry
from ..paths import cloud_norm, name_key
from .util import fmt_time, human_size, open_local, tree_with_scroll

STATUS = {"same": "＝ 相同", "different": "≠ 不同", "nas_only": "只在 NAS", "cloud_only": "只在 115"}
CONFLICT_CHOICES = [(manual.KEEP_BOTH, "保留兩者（新檔加時間戳記）"), (manual.OVERWRITE, "覆蓋（舊檔可復原）"),
                    (manual.SKIP, "略過")]


class BrowserTab:
    def __init__(self, win, parent: ttk.Frame):
        self.win = win
        self.engine = win.engine
        self.local_dir = ""
        self.cloud_stack: list[tuple[str, str]] = [("0", "")]  # (cid, 名稱)
        self.cloud_items: dict[str, CloudEntry] = {}
        self.compare: dict[str, str] = {}

        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Button(top, text="⇄ 比較兩邊（MD5/SHA-1）", command=self.compare_sides).pack(side="left")
        ttk.Label(top, text="  同名但內容不同時：").pack(side="left")
        self.conflict = ttk.Combobox(top, state="readonly", width=24, values=[c[1] for c in CONFLICT_CHOICES])
        self.conflict.set(CONFLICT_CHOICES[0][1])
        self.conflict.pack(side="left")
        ttk.Label(top, text="  上傳一律先嘗試秒傳；下載會先找 NAS 上相同內容的檔案直接複製，並驗證 SHA-1。",
                  foreground="#555").pack(side="left")

        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=6)
        pane.add(self._build_local(pane), weight=1)
        pane.add(self._build_cloud(pane), weight=1)

    # ================================================================ NAS 窗格
    def _build_local(self, pane) -> ttk.Frame:
        f = ttk.LabelFrame(pane, text="NAS / 本機", padding=6)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="↑", width=3, command=self.local_up).pack(side="left")
        self.local_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.local_var)
        e.pack(side="left", fill="x", expand=True, padx=4)
        e.bind("<Return>", lambda ev: self.local_go(self.local_var.get()))
        ttk.Button(bar, text="瀏覽…", command=self.local_pick).pack(side="left")
        ttk.Button(bar, text="⟳", width=3, command=lambda: self.local_go(self.local_dir)).pack(side="left", padx=(4, 0))
        self.local_tree = tree_with_scroll(f, [("name", "名稱", 200), ("size", "大小", 70), ("mtime", "修改時間", 130),
                                               ("cmp", "比較", 70)], height=18)
        self.local_tree.frame.pack(fill="both", expand=True, pady=4)
        self.local_tree.bind("<Double-1>", self._local_open)
        ops = ttk.Frame(f)
        ops.pack(fill="x")
        for text, cmd in (("上傳選取 →", self.upload_selected), ("新資料夾", self.local_mkdir),
                          ("重新命名", self.local_rename), ("刪除（資源回收筒）", self.local_delete),
                          ("在檔案總管開啟", lambda: open_local(self.local_dir) if self.local_dir else None)):
            ttk.Button(ops, text=text, command=cmd).pack(side="left", padx=(0, 4))
        return f

    def set_initial_local(self, path: str) -> None:
        if path and not self.local_dir:
            self.local_go(path, quiet=True)  # 程式啟動時 NAS 可能尚未連線，不跳出錯誤視窗

    def local_pick(self) -> None:
        d = filedialog.askdirectory(parent=self.win.root, title="選擇 NAS 資料夾")
        if d:
            self.local_go(d)

    def local_up(self) -> None:
        if self.local_dir:
            parent = os.path.dirname(self.local_dir.rstrip("\\/"))
            if parent and parent != self.local_dir:
                self.local_go(parent)

    def local_go(self, path: str, quiet: bool = False) -> None:
        path = path.strip()
        if not path:
            return
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(), name_key(e.name)))
        except OSError as e:
            if quiet:
                self.local_var.set(path)
                self.win.set_status(f"無法開啟 NAS 資料夾（可能尚未連線）：{e}")
            else:
                messagebox.showerror("無法開啟", str(e), parent=self.win.root)
            return
        self.local_dir = path
        self.local_var.set(path)
        self.compare = {}
        t = self.local_tree
        t.delete(*t.get_children())
        for e in entries:
            try:
                st = e.stat()
                is_dir = e.is_dir()
            except OSError:
                continue
            t.insert("", "end", iid=e.name, values=(("📁 " if is_dir else "") + e.name,
                                                     "" if is_dir else human_size(st.st_size),
                                                     fmt_time(st.st_mtime), ""))

    def _local_open(self, _event=None) -> None:
        sel = self.local_tree.selection()
        if sel:
            p = os.path.join(self.local_dir, sel[0])
            if os.path.isdir(p):
                self.local_go(p)

    def _local_selected(self) -> list[str]:
        return [os.path.join(self.local_dir, i) for i in self.local_tree.selection()]

    def local_mkdir(self) -> None:
        if not self.local_dir:
            return
        name = simpledialog.askstring("新資料夾", "資料夾名稱：", parent=self.win.root)
        if name:
            try:
                os.makedirs(os.path.join(self.local_dir, name.strip()), exist_ok=True)
            except OSError as e:
                messagebox.showerror("無法建立資料夾", str(e), parent=self.win.root)
            self.local_go(self.local_dir)

    def local_rename(self) -> None:
        sel = self._local_selected()
        if len(sel) != 1:
            return
        new = simpledialog.askstring("重新命名", "新名稱：", initialvalue=os.path.basename(sel[0]), parent=self.win.root)
        if new and new.strip() and new != os.path.basename(sel[0]):
            dest = os.path.join(self.local_dir, new.strip())
            try:
                if os.path.exists(dest) and os.path.normcase(dest) != os.path.normcase(sel[0]):
                    raise OSError(f"已有同名項目：{new.strip()}")
                os.rename(sel[0], dest)
            except OSError as e:
                messagebox.showerror("無法重新命名", str(e), parent=self.win.root)
            self.local_go(self.local_dir)

    def local_delete(self) -> None:
        sel = self._local_selected()
        if not sel or not messagebox.askyesno(
                "刪除", f"將 {len(sel)} 個項目移到資源回收筒？\n（網路磁碟上的檔案 Windows 可能會直接刪除）",
                parent=self.win.root):
            return
        errors = []
        for p in sel:
            try:
                manual.send_to_recycle_bin(p)
            except OSError as e:
                errors.append(str(e))
        if errors:
            messagebox.showerror("部分失敗", "\n".join(errors[:10]), parent=self.win.root)
        self.local_go(self.local_dir)

    # ================================================================ 115 窗格
    def _build_cloud(self, pane) -> ttk.Frame:
        f = ttk.LabelFrame(pane, text="115 網盤", padding=6)
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text="帳號：").pack(side="left")
        self.account = ttk.Combobox(bar, state="readonly", width=14)
        self.account.pack(side="left")
        self.account.bind("<<ComboboxSelected>>", lambda e: self.cloud_go_path("/"))
        ttk.Button(bar, text="↑", width=3, command=self.cloud_up).pack(side="left", padx=(6, 0))
        self.cloud_var = tk.StringVar(value="/")
        e = ttk.Entry(bar, textvariable=self.cloud_var)
        e.pack(side="left", fill="x", expand=True, padx=4)
        e.bind("<Return>", lambda ev: self.cloud_go_path(self.cloud_var.get()))
        ttk.Button(bar, text="⟳", width=3, command=self.cloud_refresh).pack(side="left")
        sbar = ttk.Frame(f)
        sbar.pack(fill="x", pady=(4, 0))
        self.search_var = tk.StringVar()
        se = ttk.Entry(sbar, textvariable=self.search_var)
        se.pack(side="left", fill="x", expand=True)
        se.bind("<Return>", lambda ev: self.cloud_search())
        ttk.Button(sbar, text="🔍 在此資料夾搜尋", command=self.cloud_search).pack(side="left", padx=(4, 0))
        self.cloud_tree = tree_with_scroll(f, [("name", "名稱", 200), ("size", "大小", 70), ("mtime", "上傳時間", 130),
                                               ("sha1", "SHA-1", 100), ("cmp", "比較", 70)], height=16)
        self.cloud_tree.frame.pack(fill="both", expand=True, pady=4)
        self.cloud_tree.bind("<Double-1>", self._cloud_open)
        ops = ttk.Frame(f)
        ops.pack(fill="x")
        for text, cmd in (("← 下載選取", self.download_selected), ("新資料夾", self.cloud_mkdir),
                          ("重新命名", self.cloud_rename), ("刪除（進 115 回收站）", self.cloud_delete),
                          ("移動到…", self.cloud_move), ("在 115 瀏覽器開啟", self.cloud_open_browser)):
            ttk.Button(ops, text=text, command=cmd).pack(side="left", padx=(0, 4))
        self.cloud_status = ttk.Label(f, text="", foreground="#555")
        self.cloud_status.pack(anchor="w")
        return f

    def refresh_accounts(self) -> None:
        cfg = self.engine.config
        self.account_ids = [a.id for a in cfg.accounts]
        self.account.configure(values=[a.label for a in cfg.accounts])
        if not self.account.get() and cfg.accounts:
            self.account.set(cfg.accounts[0].label)

    @property
    def account_id(self) -> str:
        labels = list(self.account.cget("values"))
        cur = self.account.get()
        return self.account_ids[labels.index(cur)] if cur in labels else "default"

    @property
    def client(self):
        return self.engine.client(self.account_id)

    @property
    def cid(self) -> str:
        return self.cloud_stack[-1][0]

    def _cloud_path_text(self) -> str:
        return "/" + "/".join(n for _, n in self.cloud_stack[1:])

    def _bg(self, fn, done=None, status: str = "") -> None:
        if status:
            self.cloud_status.configure(text=status)

        def run():
            try:
                res = fn()
                self.win.ui.call(lambda: (self.cloud_status.configure(text=""), done and done(res)))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                self.win.ui.call(lambda: (self.cloud_status.configure(text=f"失敗：{msg}"),
                                          messagebox.showerror("115 操作失敗", msg, parent=self.win.root)))

        threading.Thread(target=run, daemon=True).start()

    def cloud_go_path(self, path: str) -> None:
        path = cloud_norm(path)
        c = self.client  # 背景執行緒不能讀取 Tk 元件，先在主執行緒取得

        def work():
            stack, cid = [(c.root_cid, "")], c.root_cid
            for part in [p for p in path.split("/") if p]:
                child = c.find_child(cid, part, want_dir=True)
                if child is None:
                    raise ValueError(f"115 上找不到：{path}")
                cid = child.fid
                stack.append((cid, child.name))
            return stack, c.list_dir(cid)

        self._bg(work, lambda r: self._show_cloud(*r), "讀取中…")

    def cloud_refresh(self) -> None:
        stack, c = list(self.cloud_stack), self.client
        self._bg(lambda: c.list_dir(stack[-1][0]), lambda entries: self._show_cloud(stack, entries),
                 "讀取中…")

    def cloud_up(self) -> None:
        if len(self.cloud_stack) > 1:
            stack, c = self.cloud_stack[:-1], self.client
            self._bg(lambda: c.list_dir(stack[-1][0]), lambda e: self._show_cloud(stack, e), "讀取中…")

    def _show_cloud(self, stack, entries, title: str | None = None) -> None:
        self.cloud_stack = stack
        self.cloud_var.set(title or self._cloud_path_text())
        self.compare = {}
        t = self.cloud_tree
        t.delete(*t.get_children())
        self.cloud_items.clear()
        for e in sorted(entries, key=lambda x: (not x.is_dir, name_key(x.name))):
            iid = e.fid
            self.cloud_items[iid] = e
            t.insert("", "end", iid=iid, values=(("📁 " if e.is_dir else "") + e.name,
                                                  "" if e.is_dir else human_size(e.size),
                                                  fmt_time(e.mtime), e.sha1[:12], ""))

    def _cloud_open(self, _event=None) -> None:
        sel = self.cloud_tree.selection()
        if not sel:
            return
        e = self.cloud_items.get(sel[0])
        if e is None:
            return
        c = self.client
        if e.is_dir:
            stack = self.cloud_stack + [(e.fid, e.name)]
            self._bg(lambda: c.list_dir(e.fid), lambda entries: self._show_cloud(stack, entries), "讀取中…")
        elif e.pid and e.pid != self.cid:  # 搜尋結果：跳到所在資料夾
            stack = [(c.root_cid, ""), (e.pid, f"（資料夾 ID {e.pid}）")]
            self._bg(lambda: c.list_dir(e.pid), lambda entries: self._show_cloud(stack, entries), "讀取中…")

    def cloud_search(self) -> None:
        kw = self.search_var.get().strip()
        if not kw:
            return
        cid, c = self.cid, self.client
        self._bg(lambda: c.search(kw, cid),
                 lambda res: self._show_cloud(self.cloud_stack, res, f"搜尋「{kw}」：{len(res)} 筆"), "搜尋中…")

    def _cloud_selected(self) -> list[CloudEntry]:
        return [self.cloud_items[i] for i in self.cloud_tree.selection() if i in self.cloud_items]

    def cloud_mkdir(self) -> None:
        name = simpledialog.askstring("新資料夾", "115 資料夾名稱：", parent=self.win.root)
        if name:
            cid, c = self.cid, self.client
            self._bg(lambda: c.ensure_child_dir(cid, name), lambda _: self.cloud_refresh())

    def cloud_rename(self) -> None:
        sel = self._cloud_selected()
        if len(sel) != 1:
            return
        new = simpledialog.askstring("重新命名", "新名稱：", initialvalue=sel[0].name, parent=self.win.root)
        if new and new != sel[0].name:
            c = self.client
            self._bg(lambda: c.rename(sel[0].fid, new), lambda _: self.cloud_refresh())

    def cloud_delete(self) -> None:
        sel = self._cloud_selected()
        if sel and messagebox.askyesno("刪除", f"刪除 115 上 {len(sel)} 個項目？（會進入 115 回收站）",
                                       parent=self.win.root):
            cid, c = self.cid, self.client
            self._bg(lambda: c.delete([e.fid for e in sel], cid), lambda _: self.cloud_refresh())

    def cloud_move(self) -> None:
        sel = self._cloud_selected()
        if not sel:
            return
        dest = simpledialog.askstring("移動到", "目的 115 資料夾路徑（例如 /備份/整理）：", parent=self.win.root)
        if not dest:
            return
        c = self.client

        def work():
            to = c.ensure_path(dest)
            c.move([e.fid for e in sel], to)

        self._bg(work, lambda _: self.cloud_refresh(), "移動中…")

    def cloud_open_browser(self) -> None:
        open_in_115_browser(self.client.web_url(self.cid), self.engine.settings.browser_path)

    # ================================================================ 傳輸與比較
    def _conflict_mode(self) -> str:
        return next((k for k, v in CONFLICT_CHOICES if v == self.conflict.get()), manual.KEEP_BOTH)

    def upload_selected(self) -> None:
        paths = self._local_selected()
        if not paths:
            messagebox.showinfo("請選取", "請在左側選取要上傳的檔案或資料夾", parent=self.win.root)
            return
        cid, acc, mode = self.cid, self.account_id, self._conflict_mode()

        def done(stats):
            self.win.set_status(f"上傳完成：秒傳 {stats['instant']}、實際上傳 {stats['uploaded']}、"
                                f"略過 {stats['skipped']}、失敗 {stats['failed']}")
            self.cloud_refresh()

        self.win.run_bg("上傳", lambda: manual.upload_paths(self.engine, acc, paths, cid, mode), done)
        self.win.show_transfers()

    def download_selected(self) -> None:
        items = self._cloud_selected()
        if not items or not self.local_dir:
            messagebox.showinfo("請選取", "請在右側選取要下載的項目，並在左側開啟目的資料夾", parent=self.win.root)
            return
        dest, acc, mode = self.local_dir, self.account_id, self._conflict_mode()

        def done(stats):
            self.win.set_status(f"下載完成：{stats['downloaded']} 個（其中從 NAS 複製 {stats['copied']}）、"
                                f"略過 {stats['skipped']}、失敗 {stats['failed']}")
            self.local_go(dest)

        self.win.run_bg("下載", lambda: manual.download_entries(self.engine, acc, items, dest, mode), done)
        self.win.show_transfers()

    def compare_sides(self) -> None:
        if not self.local_dir:
            return
        local_dir, acc, cid = self.local_dir, self.account_id, self.cid

        def done(result):
            self.compare = result
            for iid in self.local_tree.get_children():
                self.local_tree.set(iid, "cmp", STATUS.get(result.get(name_key(iid)), ""))
            for iid, e in self.cloud_items.items():
                self.cloud_tree.set(iid, "cmp", STATUS.get(result.get(name_key(e.name)), ""))
            n = {k: list(result.values()).count(k) for k in STATUS}
            self.win.set_status(f"比較完成：相同 {n['same']}、不同 {n['different']}、只在 NAS {n['nas_only']}、"
                                f"只在 115 {n['cloud_only']}")

        self._bg(lambda: manual.compare_folders(self.engine, local_dir, acc, cid), done, "計算 SHA-1 並比較中…")
