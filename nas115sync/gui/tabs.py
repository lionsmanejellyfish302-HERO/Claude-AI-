"""「傳輸」與「重複檔案」分頁。"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .. import manual
from . import theme
from .theme import S, px
from .util import AutoScrollbar, Flow, human_size, open_local, tree_with_scroll

STATUS_LABELS = {"queued": "排隊中", "running": "傳輸中", "paused": "已暫停", "done": "完成", "failed": "失敗",
                 "cancelled": "已取消", "skipped": "略過"}


class TransfersTab:
    """傳輸佇列：進度、速度、暫停／繼續、取消、限速（即時生效）。"""

    def __init__(self, win, parent: ttk.Frame):
        self.win = win
        self.mgr = win.engine.transfers
        top = Flow(parent)
        top.pack(fill="x", pady=(0, px(S)))
        s = win.config.settings
        self.pause_btn = ttk.Button(top, text="全部暫停", command=self.toggle_pause)
        self.up_var = tk.StringVar(value=str(s.upload_limit_kb))
        self.down_var = tk.StringVar(value=str(s.download_limit_kb))
        top.add(self.pause_btn,
                ttk.Button(top, text="取消選取的傳輸", style="Destructive.TButton", command=self.cancel_selected),
                ttk.Button(top, text="清除已完成", command=self.clear),
                ttk.Label(top, text="上傳限速 KB/s"), ttk.Entry(top, textvariable=self.up_var, width=8),
                ttk.Label(top, text="下載限速 KB/s"), ttk.Entry(top, textvariable=self.down_var, width=8),
                ttk.Button(top, text="套用（0 = 不限）", command=self.apply_limits))
        self.summary = ttk.Label(parent, text="", style="Secondary.TLabel")
        self.summary.pack(anchor="w", pady=(0, px(S)))
        self.tree = tree_with_scroll(parent, [("id", "#", 50), ("kind", "方向", 70), ("path", "檔案", 420),
                                              ("size", "大小", 90), ("pct", "進度", 70), ("speed", "速度", 90),
                                              ("status", "狀態", 110), ("err", "說明", 260)], height=22)
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.tag_configure("failed", foreground=theme.P.danger_text)
        self.tree.tag_configure("instant", foreground=theme.P.ok_text)

    def toggle_pause(self) -> None:
        if self.mgr.paused.is_set():
            self.mgr.resume_all()
        else:
            self.mgr.pause_all()
        self.refresh()

    def cancel_selected(self) -> None:
        for iid in self.tree.selection():
            self.mgr.cancel(int(iid))

    def clear(self) -> None:
        self.mgr.clear_finished()
        self.refresh()

    def apply_limits(self) -> None:
        try:
            up, down = int(self.up_var.get() or 0), int(self.down_var.get() or 0)
        except ValueError:
            messagebox.showerror("格式錯誤", "限速請填整數（KB/s）", parent=self.win.root)
            return
        s = self.win.config.settings
        s.upload_limit_kb, s.download_limit_kb = up, down
        self.win.config.save()
        self.mgr.set_limits(up, down)
        self.win.set_status(f"限速已套用：上傳 {up or '不限'} KB/s、下載 {down or '不限'} KB/s")

    def refresh(self) -> None:
        self.pause_btn.configure(text="全部繼續" if self.mgr.paused.is_set() else "全部暫停")
        snap = self.mgr.snapshot()
        active = [t for t in snap if t["status"] in ("running", "paused")]
        speed = sum(t["speed"] for t in active)
        instant = sum(1 for t in snap if t["instant"])
        self.summary.configure(text=f"進行中 {len(active)} 個，總速度 {human_size(speed)}/s；"
                                    f"本次啟動後秒傳／免下載 {instant} 個")
        seen = set()
        for t in reversed(snap):
            iid = str(t["id"])
            seen.add(iid)
            pct = f"{t['done'] * 100 // max(1, t['size'])}%" if t["size"] else "—"
            status = STATUS_LABELS.get(t["status"], t["status"])
            if t["instant"] and t["status"] == "done":
                status = "秒傳完成" if t["kind"] == "upload" else "從 NAS 複製"
            vals = (t["id"], "上傳" if t["kind"] == "upload" else "下載", t["rel_path"], human_size(t["size"]), pct,
                    f"{human_size(t['speed'])}/s" if t["status"] == "running" else "", status, t["error"])
            tags = ("failed",) if t["status"] == "failed" else ("instant",) if t["instant"] else ()
            if self.tree.exists(iid):
                self.tree.item(iid, values=vals, tags=tags)
            else:
                self.tree.insert("", 0, iid=iid, values=vals, tags=tags)
        for iid in self.tree.get_children():
            if iid not in seen:
                self.tree.delete(iid)


class DuplicatesTab:
    """依 SHA-1 找出 NAS 與 115 上的重複檔案（同一邊有 2 份以上）。"""

    def __init__(self, win, parent: ttk.Frame):
        self.win = win
        top = Flow(parent)
        top.pack(fill="x", pady=(0, px(S)))
        top.add(ttk.Button(top, text="重新整理（用最近一次比對的索引）", style="Accent.TButton", command=self.refresh),
                ttk.Button(top, text="完整比對後重新整理", command=self.rescan),
                ttk.Button(top, text="刪除選取的 115 副本", style="Destructive.TButton", command=self.delete_selected),
                ttk.Button(top, text="開啟位置", style="Link.TButton", command=self.open_selected))
        self.summary = ttk.Label(parent, text="按「重新整理」分析重複檔案。", style="Secondary.TLabel")
        self.summary.pack(anchor="w", pady=(0, px(S)))
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("where", "size", "info"), show="tree headings", height=22)
        self.tree.heading("#0", text="SHA-1 ／ 路徑", anchor="w")
        self.tree.heading("where", text="位置")
        self.tree.heading("size", text="大小")
        self.tree.heading("info", text="說明")
        self.tree.column("#0", width=560)
        self.tree.column("where", width=70)
        self.tree.column("size", width=90)
        self.tree.column("info", width=260)
        sb = AutoScrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.items: dict[str, dict] = {}

    def refresh(self) -> None:
        def work():
            return manual.duplicate_report(self.win.engine)

        def done(groups):
            self.tree.delete(*self.tree.get_children())
            self.items.clear()
            for gi, g in enumerate(groups):
                parent = self.tree.insert("", "end", iid=f"g{gi}", text=g["sha1"], open=False, values=(
                    "", human_size(g["size"]), f"NAS {g['nas_count']} 份、115 {g['cloud_count']} 份，"
                                               f"多佔 {human_size(g['wasted'])}"))
                for ii, it in enumerate(g["items"]):
                    iid = f"g{gi}i{ii}"
                    self.items[iid] = it
                    self.tree.insert(parent, "end", iid=iid, text=it["path"], values=(it["where"], "", ""))
            total = sum(g["wasted"] for g in groups)
            self.summary.configure(text=f"共 {len(groups)} 組重複檔案，可省下約 {human_size(total)}。"
                                        "展開群組後可選取 115 上多餘的副本刪除（進 115 回收站）。")

        self.win.run_bg("分析重複檔案", work, done)

    def rescan(self) -> None:
        if self.win.ready_to_sync():
            self.win.run_bg("完整比對", lambda: self.win.engine.analyze(progress=self.win.progress_cb),
                            lambda _: self.refresh())

    def delete_selected(self) -> None:
        items = [self.items[i] for i in self.tree.selection() if i in self.items and self.items[i]["where"] == "115"]
        if not items:
            messagebox.showinfo("請選取", "請展開群組並選取要刪除的 115 副本", parent=self.win.root)
            return
        if not messagebox.askyesno("刪除", f"刪除 115 上 {len(items)} 個重複副本？（會進入 115 回收站）",
                                   parent=self.win.root):
            return
        self.win.run_bg("刪除重複副本", lambda: manual.delete_cloud_items(self.win.engine, items),
                        lambda n: (self.win.set_status(f"已刪除 {n} 個 115 副本"), self.refresh()))

    def open_selected(self) -> None:
        for iid in self.tree.selection():
            it = self.items.get(iid)
            if it and it["where"] == "NAS":
                open_local(it["path"])
                return

