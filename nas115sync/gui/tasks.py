"""工作清單（瀏覽頁下方）：比照 Air Explorer，每個工作一列，展開後是每個檔案的進度。

欄位：操作、來源、目的、狀態、大小、進度、速度、預估。秒傳的檔案以閃電圖示標示。
"""
from __future__ import annotations

import os
from tkinter import ttk

from ..paths import cloud_join, local_join
from . import icons, theme
from .gridlist import Column, GridList
from .theme import px
from .util import Tooltip, fmt_duration, human_size, popup
from .widgets import tool_button

KIND_ICON = {"upload": "upload", "download": "download", "copy": "copy", "sync": "sync", "compare": "compare"}
KIND_LABEL = {"upload": "上傳", "download": "下載", "copy": "複製", "sync": "同步"}
STATUS = {"queued": ("queued", "排隊中"), "running": ("running", "傳輸中"), "paused": ("paused", "已暫停"),
          "done": ("ok", "已完成"), "failed": ("error", "失敗"), "cancelled": ("skipped", "已取消"),
          "skipped": ("skipped", "略過")}
ACTIVE = ("queued", "running", "paused")


def _place_icon(text: str) -> str:
    """來源／目的文字是本機路徑還是 115：本機路徑有磁碟機代號、UNC 或以 / 開頭。"""
    t = text.strip()
    return "computer" if (t[1:3] == ":\\" or t.startswith("\\\\") or (os.sep == "/" and t.startswith("/"))
                          or t == "本機") else "cloud"


class TaskArea(ttk.Frame):
    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.mgr = win, win.engine.transfers
        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text="工作清單", style="Bold.TLabel").pack(side="left", padx=(px(6), px(10)))
        self.summary = ttk.Label(head, text="", style="Secondary.TLabel")
        self.summary.pack(side="left")
        tool_button(head, "broom", command=self.clear, hint="清除已完成的工作").pack(side="right", padx=(0, px(2)))
        tool_button(head, "stop", command=self.cancel_selected, hint="取消選取的工作或檔案").pack(side="right")
        self.btn_pause = tool_button(head, "pause", command=self.toggle_pause)
        self.btn_pause.pack(side="right")
        self._pause_tip = Tooltip(self.btn_pause, "全部暫停")
        theme.line(self).pack(fill="x")
        cols = [Column("op", "操作", 150, "icontext"), Column("src", "來源", 250, "icontext", stretch=True),
                Column("dst", "目的", 230, "icontext"), Column("status", "狀態", 130, "icontext"),
                Column("size", "大小", 140), Column("prog", "進度", 110, "progress"),
                Column("speed", "速度", 86, anchor="e"), Column("eta", "預估", 64, anchor="e")]
        self.grid_list = GridList(self, cols, tree=True, on_menu=self._menu)
        self.grid_list.pack(fill="both", expand=True)
        self._sig = None
        self._seen: set = set()
        self.count = 0
        self.totals = (0, 0, 0.0, 0.0)  # 進行中：已完成位元組、總位元組、下載速度、上傳速度

    # ------------------------------------------------------------ 動作
    def toggle_pause(self) -> None:
        if self.mgr.paused.is_set():
            self.mgr.resume_all()
        else:
            self.mgr.pause_all()
        self.refresh(force=True)

    def cancel_selected(self) -> None:
        for rid in self.grid_list.selection():
            kind, key = rid
            if kind == "t":
                self.mgr.cancel(key)
            else:
                for t in self.mgr.snapshot():
                    if self._group_of(t) == rid and t["status"] in ACTIVE:
                        self.mgr.cancel(t["id"])
                if kind == "b" and key == self.win.current_batch:
                    self.win.cancel()
        self.refresh(force=True)

    def clear(self) -> None:
        self.mgr.clear_finished()
        self.refresh(force=True)

    def menu_items(self) -> list:
        paused = self.mgr.paused.is_set()
        return [{"label": "全部繼續" if paused else "全部暫停", "icon": "play" if paused else "pause",
                 "command": self.toggle_pause},
                {"label": "取消選取的工作", "icon": "stop", "enabled": bool(self.grid_list.selection()),
                 "command": self.cancel_selected},
                None,
                {"label": "清除已完成", "icon": "broom", "command": self.clear}]

    def _menu(self, event, _row) -> None:
        popup(self.grid_list.canvas, self.menu_items(), event.x_root, event.y_root)

    # ------------------------------------------------------------ 資料
    @staticmethod
    def _group_of(t: dict) -> tuple:
        return ("b", t["batch"]) if t["batch"] else ("m", t["mapping_id"])

    def _child(self, t: dict, src_base: str, dst_base: str, mapping) -> dict:
        kind, rel = t["kind"], t["rel_path"]
        if mapping is not None:
            local, cloud = local_join(mapping.local_path, rel), cloud_join(mapping.cloud_path, rel)
            src, dst = (cloud, local) if kind == "download" else (local, cloud)
        elif kind == "download":
            src, dst = src_base, rel
        else:
            src, dst = rel, dst_base
        icon, label = STATUS.get(t["status"], ("queued", t["status"]))
        if t["status"] == "done" and t["instant"]:
            label = "秒傳完成" if kind == "upload" else "從本機複製"
        elif t["status"] == "failed" and t["error"]:
            label = f"失敗：{t['error']}"
        size, done = t["size"], t["done"]
        frac = 1.0 if t["status"] == "done" else (done / size if size else 0.0)
        state = {"done": "done", "failed": "fail", "paused": "paused"}.get(t["status"], "run")
        running = t["status"] == "running"
        eta = fmt_duration((size - done) / t["speed"]) if running and t["speed"] > 0 else ""
        name = rel.replace("\\", "/").rsplit("/", 1)[-1]
        op = ("bolt", "秒傳") if t["instant"] and kind == "upload" else (KIND_ICON.get(kind, "upload"),
                                                                      KIND_LABEL.get(kind, kind))
        return {"id": ("t", t["id"]), "fg": theme.P.danger if t["status"] == "failed" else None,
                "cells": {"op": op, "src": (icons.file_icon_name(name), src),
                          "dst": ({"upload": "cloud", "download": "computer"}.get(kind) or _place_icon(dst), dst),
                          "status": (icon, label), "size": f"{human_size(done)} / {human_size(size)}",
                          "prog": (frac, f"{int(frac * 100)}%", state),
                          "speed": f"{human_size(t['speed'])}/s" if running and t["speed"] else "", "eta": eta}}

    def _rows(self, snap: list[dict], batches: list[dict]) -> list[dict]:
        cfg = self.win.config
        groups: dict[tuple, list[dict]] = {}
        for t in snap:
            groups.setdefault(self._group_of(t), []).append(t)
        heads: list[tuple[float, dict]] = []
        for b in batches:
            gid = ("b", b["id"])
            heads.append((b["started"], {"gid": gid, "kind": b["kind"], "label": b["label"], "src": b["source"],
                                         "dst": b["dest"], "status": b["status"], "mapping": None}))
        for gid, items in groups.items():
            if gid[0] == "m":
                m = cfg.get_mapping(gid[1])
                acct = cfg.get_account(m.account_id).label if m and cfg.get_account(m.account_id) else "115"
                active = any(t["status"] in ACTIVE for t in items)
                heads.append((min(t["started"] for t in items), {
                    "gid": gid, "kind": "sync", "label": "即時同步" if m else "其他傳輸",
                    "src": m.local_path if m else "", "dst": f"{acct} {m.cloud_path}" if m else "",
                    "status": "running" if active else "done", "mapping": m}))
        rows = []
        for _started, h in sorted(heads, key=lambda x: x[0], reverse=True):
            items = sorted(groups.get(h["gid"], []), key=lambda t: t["id"])
            mapping = h["mapping"]
            kids = [self._child(t, h["src"], h["dst"], mapping or cfg.get_mapping(t["mapping_id"])) for t in items]
            total = sum(t["size"] for t in items)
            done = sum(t["size"] if t["status"] == "done" else t["done"] for t in items)
            failed = sum(t["status"] == "failed" for t in items)
            instant = sum(bool(t["instant"]) for t in items)
            speed = sum(t["speed"] for t in items if t["status"] == "running")
            status = h["status"]
            if status == "running":
                st = ("paused", "已暫停") if self.mgr.paused.is_set() else ("running", "執行中")
            elif status == "cancelled":
                st = ("skipped", "已取消")
            elif failed or status == "failed":
                st = ("error", f"失敗 {failed} 個" if failed else "失敗")
            else:
                st = ("ok", f"已完成（秒傳 {instant} 個）" if instant else "已完成")
            if items:
                frac = done / total if total else (0.0 if status == "running" else 1.0)
                prog = (frac, f"{int(frac * 100)}%", "fail" if st[0] == "error" else "done" if status != "running" else "run")
                size = f"{human_size(done)} / {human_size(total)}"
            else:
                prog = (None, "…" if status == "running" else "", "run")
                size = ""
            rows.append({"id": h["gid"], "children": kids, "cells": {
                "op": (KIND_ICON.get(h["kind"], "sync"), h["label"]),
                "src": (None if not h["dst"] and h["kind"] == "sync" else _place_icon(h["src"]), h["src"])
                if h["src"] else (None, ""),
                "dst": (_place_icon(h["dst"]), h["dst"]) if h["dst"] else (None, ""),
                "status": st, "size": size, "prog": prog,
                "speed": f"{human_size(speed)}/s" if speed else "",
                "eta": fmt_duration((total - done) / speed) if speed > 0 and status == "running" else ""}})
        return rows

    def refresh(self, force: bool = False) -> None:
        snap, batches = self.mgr.snapshot(), self.mgr.batches()
        active = [t for t in snap if t["status"] in ("running", "paused")]
        self.totals = (sum(t["done"] for t in active), sum(t["size"] for t in active),
                       sum(t["speed"] for t in active if t["kind"] == "download" and t["status"] == "running"),
                       sum(t["speed"] for t in active if t["kind"] != "download" and t["status"] == "running"))
        sig = (tuple((t["id"], t["status"], t["done"], int(t["speed"])) for t in snap),
               tuple((b["id"], b["status"]) for b in batches), self.mgr.paused.is_set())
        if sig == self._sig and not force:
            return
        self._sig = sig
        paused = self.mgr.paused.is_set()
        self.btn_pause.configure(image=theme.icon_pair("play" if paused else "pause"))
        self._pause_tip.text = "全部繼續" if paused else "全部暫停"
        rows = self._rows(snap, batches)
        for r in rows:  # 新出現的工作自動展開，之後照使用者的展開狀態
            if r["id"] not in self._seen:
                self._seen.add(r["id"])
                self.grid_list._open.add(r["id"])
        self.count = len(rows)
        self.grid_list.set_rows(rows)
        instant = sum(1 for t in snap if t["instant"])
        self.summary.configure(text=(f"進行中 {len(active)} 個" if active else "沒有進行中的傳輸")
                               + (f"・本次啟動後秒傳／免下載 {instant} 個" if instant else ""))
