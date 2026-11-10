"""「瀏覽」頁：比照 Air Explorer 的檔案面板。

每個面板可以連到「本機」（含 NAS 的網路磁碟機／UNC 路徑）或任何一個 115 帳號；面板上方是分頁列，
裡面依序是位址列（上一頁／下一頁、可點擊的路徑、書籤星號、重新整理）、工具列、資料夾樹＋檔案清單、狀態列（項目數、可用空間）。
兩個面板之間是傳輸箭頭：本機 → 115 是上傳（先秒傳），115 → 本機是下載。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, simpledialog, ttk

from .. import manual
from ..app import open_in_115_browser
from ..cloud.base import CloudEntry
from ..paths import cloud_norm, long_path, name_key, unsafe_name
from . import theme
from .theme import px
from .util import (AutoScrollbar, fmt_date, human_size, menu_item, open_file, open_local, popup, popup_below)
from .widgets import Breadcrumb, TabStrip, Toggle, tool_button

COMPARE_LABELS = {"same": "＝ 相同", "different": "≠ 不同", "nas_only": "只在本機", "cloud_only": "只在 115"}
CONFLICT_LABELS = {manual.KEEP_BOTH: "保留兩者（新檔加上時間）", manual.OVERWRITE: "覆蓋（舊檔可復原）", manual.SKIP: "略過"}


@dataclass
class Item:
    key: str            # 本機：完整路徑；115：檔案／資料夾 ID
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    sha1: str = ""
    entry: CloudEntry | None = None
    icon: str | None = None  # 指定圖示（磁碟機、NAS 捷徑）


# =========================================================================== 位置來源
class LocalSource:
    """本機（含網路磁碟機與 \\\\NAS\\共用 這種 UNC 路徑）。位置以路徑字串表示，空字串是「本機」最上層。"""
    kind, icon, account_id, root = "local", "computer", "", ""
    title = "本機"

    def __init__(self, config):
        self.config = config

    def same(self, other) -> bool:
        return other is not None and other.kind == "local"

    def crumbs(self, place: str) -> list[tuple[str, str]]:
        out = [(self.title, "")]
        if not place:
            return out
        drive, rest = os.path.splitdrive(place)
        base = (drive + os.sep) if drive else os.sep
        out.append((drive or os.sep, base))
        cur = base
        for part in [x for x in rest.replace("\\", "/").split("/") if x]:
            cur = os.path.join(cur, part)
            out.append((part, cur))
        return out

    def parent(self, place: str) -> str | None:
        if not place:
            return None
        up = os.path.dirname(place.rstrip("\\/")) if place.rstrip("\\/") else place
        if not up or os.path.normcase(up.rstrip("\\/")) == os.path.normcase(place.rstrip("\\/")):
            return ""
        return up

    def child(self, place: str, item: Item) -> str:
        return item.key

    def path_text(self, place: str) -> str:
        return place

    def label(self, place: str) -> str:
        return place or self.title

    def list(self, place: str, show_hidden: bool) -> list[Item]:
        if not place:
            return self._roots()
        out = []
        with os.scandir(long_path(place)) as it:
            for e in it:
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                except OSError:
                    continue
                if not show_hidden and _hidden(e.name, st):
                    continue
                out.append(Item(os.path.join(place, e.name), e.name, is_dir, 0 if is_dir else st.st_size, st.st_mtime))
        return out

    def _roots(self) -> list[Item]:
        """「本機」最上層：磁碟機、已設定同步的 NAS 資料夾、使用者資料夾。"""
        out: list[Item] = []
        seen: set[str] = set()

        def add(path: str, name: str, icon: str) -> None:
            key = os.path.normcase(path.rstrip("\\/")) or path
            if key not in seen:
                seen.add(key)
                out.append(Item(path, name, True, icon=icon))

        if sys.platform == "win32":
            for d in _drives():
                add(d, d.rstrip("\\"), "drive")
        else:
            add(os.sep, os.sep, "drive")
        for m in self.config.mappings:
            add(m.local_path, m.local_path, "nas")
        home = os.path.expanduser("~")
        if os.path.isdir(home):
            add(home, f"使用者資料夾（{home}）", "folder")
        return out

    def free_space(self, place: str) -> tuple[int, int] | None:
        """(總容量, 已使用)。"""
        if not place:
            return None
        try:
            u = shutil.disk_usage(long_path(place))
            return u.total, u.used
        except OSError:
            return None


def _drives() -> list[str]:
    if hasattr(os, "listdrives"):
        try:
            return list(os.listdrives())
        except OSError:
            pass
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
        return [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    except (AttributeError, OSError):
        return ["C:\\"]


def _hidden(name: str, st: os.stat_result) -> bool:
    return name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & 0x6)  # 隱藏或系統檔


class CloudSource:
    """一個 115 帳號。位置以 ((資料夾 ID, 名稱), …) 表示，第一項是根目錄。"""
    kind, icon = "cloud", "cloud"

    def __init__(self, engine, account):
        self.engine, self.account_id, self.title = engine, account.id, account.label

    @property
    def client(self):
        return self.engine.client(self.account_id)

    @property
    def root(self) -> tuple:
        return ((self.client.root_cid, ""),)

    def same(self, other) -> bool:
        return other is not None and other.kind == "cloud" and other.account_id == self.account_id

    def crumbs(self, place: tuple) -> list[tuple[str, tuple]]:
        return [(self.title, place[:1])] + [(name, place[:i + 1]) for i, (_cid, name) in enumerate(place) if i]

    def parent(self, place: tuple) -> tuple | None:
        return place[:-1] if len(place) > 1 else None

    def child(self, place: tuple, item: Item) -> tuple:
        return place + ((item.key, item.name),)

    def path_text(self, place: tuple) -> str:
        return "/" + "/".join(n for _c, n in place[1:])

    def label(self, place: tuple) -> str:
        return f"{self.title} {self.path_text(place)}"

    def cid(self, place: tuple) -> str:
        return place[-1][0]

    def list(self, place: tuple, show_hidden: bool) -> list[Item]:
        return [Item(e.fid, e.name, e.is_dir, e.size, e.mtime, e.sha1, e)
                for e in self.client.list_dir(self.cid(place)) if show_hidden or not e.name.startswith(".")]

    def resolve(self, text: str) -> tuple:
        """把輸入的 115 路徑（/備份/相片）轉成位置；找不到時丟出 ValueError。"""
        c = self.client
        place = [(c.root_cid, "")]
        for part in [p for p in cloud_norm(text).split("/") if p]:
            child = c.find_child(place[-1][0], part, want_dir=True)
            if child is None:
                raise ValueError(f"115 上找不到：{cloud_norm(text)}")
            place.append((child.fid, child.name))
        return tuple(place)

    def place_of(self, cid: str) -> tuple:
        """由資料夾 ID 找出完整位置（搜尋結果的「檢視資料夾」用）。"""
        c = self.client
        if str(cid) == str(c.root_cid):
            return self.root
        path = c.path_of(cid)
        if path is None:
            return self.root + ((str(cid), f"（資料夾 {cid}）"),)
        return self.root + tuple(path)

    def free_space(self, place=None) -> tuple[int, int] | None:
        return self.client.space_info()


# =========================================================================== 檔案面板
class FilePanel(tk.Frame):
    def __init__(self, group: "PanelGroup", source, place=None):
        p = theme.P
        super().__init__(group.body, background=p.bg, highlightthickness=1, highlightbackground=p.line,
                         highlightcolor=p.line, borderwidth=0)
        self.group, self.page, self.win = group, group.page, group.page.win
        self.source = source
        self.place = source.root
        self.items: list[Item] = []
        self.rows: dict[str, Item] = {}
        self.compare: dict[str, str] = {}
        self.sort: tuple[str, bool] = ("#0", False)
        self.history: list = []
        self.hist_pos = -1
        self._gen = 0
        self._busy = 0
        self._nodes: dict[str, object] = {}
        self._node_of: dict[object, str] = {}
        self._tree_guard = False
        self._tree_loaded: set[str] = set()  # 已讀過子資料夾的節點
        self._drag = None
        self._press_sel: tuple | None = None
        self._quota: tuple[int, int] | None = None
        self._images: list = []
        self._build()
        self.go(place if place is not None else source.root, follow=False)

    # ------------------------------------------------------------ 介面
    def _build(self) -> None:
        p = theme.P
        # 位址列
        addr = tk.Frame(self, background=p.field, highlightthickness=1, highlightbackground=p.line, borderwidth=0)
        addr.pack(fill="x", padx=px(4), pady=(px(4), px(2)))
        self.btn_back = tool_button(addr, "back", command=self.back, hint="上一頁")
        self.btn_fwd = tool_button(addr, "forward", command=self.forward, hint="下一頁")
        self.btn_back.pack(side="left")
        self.btn_fwd.pack(side="left")
        self.btn_loc = tool_button(addr, self.source.icon, command=self._location_menu, hint="切換位置：本機、書籤、115 帳號")
        self.btn_loc.pack(side="left", padx=(px(2), 0))
        self.btn_refresh = tool_button(addr, "refresh", command=self.refresh, hint="重新整理（F5）")
        self.btn_refresh.pack(side="right")
        self.btn_star = tool_button(addr, "star", command=self.toggle_bookmark, hint="把這個資料夾加入書籤")
        self.btn_star.pack(side="right")
        self.crumb = Breadcrumb(addr, self.go, self.go_text)
        self.crumb.pack(side="left", fill="x", expand=True)

        # 工具列
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=px(4))
        self.tree_var = tk.BooleanVar(self, value=bool(self.win.config.settings.ui.get("tree", True)))
        Toggle(bar, self.tree_var, "tree", command=self._toggle_tree, hint="顯示／隱藏資料夾樹").pack(side="left")
        tool_button(bar, "newfolder", command=self.new_folder, hint="新資料夾").pack(side="left")
        theme.line(bar, vertical=True).pack(side="left", fill="y", padx=px(4), pady=px(3))
        Toggle(bar, self.page.instant_var, "bolt", "只秒傳", command=self.page.save_options,
               hint="上傳到 115 時只做秒傳：115 上還沒有相同內容的檔案不會實際上傳").pack(side="left")
        Toggle(bar, self.page.hidden_var, "eye", command=self.page.hidden_changed, hint="顯示隱藏檔").pack(side="left")
        theme.line(bar, vertical=True).pack(side="left", fill="y", padx=px(4), pady=px(3))
        tool_button(bar, "compare", "比較兩邊", command=lambda: self.page.compare(self),
                    hint="以 MD5／SHA-1 比較兩個面板目前的資料夾").pack(side="left")
        self.btn_view = tool_button(bar, "list", "▾", command=self._view_menu, hint="顯示的欄位、同名檔案的處理方式")
        self.btn_view.pack(side="right")
        self.btn_marks = tool_button(bar, "star_on", "▾", command=self._bookmark_menu, hint="書籤")
        self.btn_marks.pack(side="right")

        # 狀態列（先放：視窗再矮也看得到）
        status = ttk.Frame(self)
        status.pack(side="bottom", fill="x", padx=px(4), pady=(0, px(2)))
        self.status = ttk.Label(status, text="")
        self.status.pack(side="left", padx=px(2))
        self.btn_quota = tool_button(status, None, "", command=self._quota_menu, hint="可用空間")
        self.btn_quota.pack(side="right")
        theme.line(self).pack(side="bottom", fill="x")

        # 資料夾樹 ＋ 檔案清單
        theme.line(self).pack(fill="x", pady=(px(2), 0))
        self.pane = ttk.PanedWindow(self, orient="horizontal")
        self.pane.pack(fill="both", expand=True)
        self.tree_frame = ttk.Frame(self.pane)
        self.ftree = ttk.Treeview(self.tree_frame, show="tree", selectmode="browse", height=4)
        self.ftree.column("#0", width=px(180), minwidth=px(120), stretch=True)
        sb = AutoScrollbar(self.tree_frame, orient="vertical", command=self.ftree.yview)
        self.ftree.configure(yscrollcommand=sb.set)
        self.ftree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.ftree.bind("<<TreeviewOpen>>", self._tree_open)
        self.ftree.bind("<<TreeviewSelect>>", self._tree_select)

        list_frame = ttk.Frame(self.pane)
        cols = [("size", "大小", 84), ("date", "日期", 128), ("sha1", "SHA-1", 108), ("cmp", "比較", 84)]
        self.tree = ttk.Treeview(list_frame, columns=[c[0] for c in cols], show="tree headings", height=4,
                                 selectmode="extended")
        self.tree.heading("#0", text="名稱", anchor="w", command=lambda: self.sort_by("#0"))
        self.tree.column("#0", width=px(240), minwidth=px(120), stretch=True)
        for cid, label, width in cols:
            self.tree.heading(cid, text=label, anchor="w", command=lambda c=cid: self.sort_by(c))
            self.tree.column(cid, width=px(width), minwidth=px(40), stretch=False, anchor="w")
        vb = AutoScrollbar(list_frame, orient="vertical", command=self.tree.yview)
        hb = AutoScrollbar(list_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vb.set, xscrollcommand=hb.set)
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vb.grid(row=0, column=1, sticky="ns")
        hb.grid(row=1, column=0, sticky="ew")
        self.notice = ttk.Label(list_frame, text="", style="Secondary.TLabel", justify="center")
        self.pane.add(self.tree_frame, weight=0)
        self.pane.add(list_frame, weight=1)
        if not self.tree_var.get():
            self.pane.forget(self.tree_frame)

        t = self.tree
        t.bind("<Configure>", lambda e: self._fit_columns(), add="+")
        t.bind("<Double-Button-1>", lambda e: self.open_selected())
        t.bind("<Return>", lambda e: self.open_selected())
        t.bind("<BackSpace>", lambda e: self.go_up())
        t.bind("<F2>", lambda e: self.rename())
        t.bind("<Delete>", lambda e: self.delete())
        t.bind("<F5>", lambda e: self.refresh())
        t.bind("<F4>", lambda e: self.crumb.edit())
        t.bind("<Control-a>", lambda e: (t.selection_set(t.get_children()), "break")[1])
        t.bind("<Control-c>", lambda e: self.page.copy(self))
        t.bind("<Control-x>", lambda e: self.page.copy(self, cut=True))
        t.bind("<Control-v>", lambda e: self.page.paste(self))
        t.bind("<Button-3>", self._context)
        t.bind("<<TreeviewSelect>>", lambda e: self._update_status())
        t.bind("<ButtonPress-1>", self._press, add="+")
        t.bind("<B1-Motion>", self._motion)
        t.bind("<ButtonRelease-1>", self._release, add="+")
        for w in (t, self.ftree):
            w.bind("<FocusIn>", lambda e: self.page.set_active(self), add="+")
        self._show_columns()

    def _toggle_tree(self) -> None:
        if self.tree_var.get():
            self.pane.insert(0, self.tree_frame, weight=0)
        else:
            self.pane.forget(self.tree_frame)
        self.win.config.settings.ui["tree"] = bool(self.tree_var.get())
        self.win.save_ui()

    def _show_columns(self) -> None:
        shown = self.win.config.settings.ui.get("columns") or {}
        cols = [c for c in ("size", "date") if shown.get(c, True)]
        if self.source.kind == "cloud" and shown.get("sha1", False):
            cols.append("sha1")
        if self.compare:
            cols.append("cmp")
        self.tree.configure(displaycolumns=cols)
        self._fit_columns()

    # ------------------------------------------------------------ 導覽
    def go(self, place, record: bool = True, follow: bool = True, select: str | None = None) -> None:
        """開啟一個位置。115 的資料夾在背景讀取，讀完才換畫面。"""
        self._gen += 1
        gen, src, hidden = self._gen, self.source, bool(self.page.hidden_var.get())
        prev = self.place

        def done(items) -> None:
            if gen == self._gen and self.winfo_exists():
                self._show(place, items, record, select)
                if follow:
                    self.group.pair.follow(self, prev, place)

        def failed(e: Exception) -> None:
            if gen == self._gen and self.winfo_exists():
                self._failed(place, e)

        if src.kind == "local":
            try:
                done(src.list(place, hidden))
            except OSError as e:
                failed(e)
            return
        self._set_busy(1)

        def work() -> None:
            try:
                items = src.list(place, hidden)
            except Exception as e:  # noqa: BLE001
                self.win.ui.call(lambda err=e: (self._set_busy(-1), failed(err)))
                return
            self.win.ui.call(lambda: (self._set_busy(-1), done(items)))

        threading.Thread(target=work, daemon=True).start()

    def go_text(self, text: str) -> None:
        """位址列輸入的路徑。"""
        src = self.source
        if src.kind == "local":
            path = os.path.expanduser(text.strip().strip('"'))
            self.go(os.path.abspath(path) if path else "")
            return
        self._set_busy(1)

        def work() -> None:
            try:
                place = src.resolve(text)
            except Exception as e:  # noqa: BLE001
                self.win.ui.call(lambda err=e: (self._set_busy(-1), self._error("無法開啟", err)))
                return
            self.win.ui.call(lambda: (self._set_busy(-1), self.go(place)))

        threading.Thread(target=work, daemon=True).start()

    def go_up(self) -> None:
        up = self.source.parent(self.place)
        if up is not None:
            self.go(up)

    def back(self) -> None:
        if self.hist_pos > 0:
            self.hist_pos -= 1
            self.go(self.history[self.hist_pos], record=False)

    def forward(self) -> None:
        if self.hist_pos < len(self.history) - 1:
            self.hist_pos += 1
            self.go(self.history[self.hist_pos], record=False)

    def refresh(self) -> None:
        names = [it.name for it in self.selected()]
        self.go(self.place, record=False, follow=False)
        if names and self.source.kind == "local":
            self.select_names(names)

    def _failed(self, place, e: Exception) -> None:
        if self.items or place == self.place:
            self._error("無法開啟", e)
        if place == self.place or not self.history:  # 第一次開啟就失敗：留在這裡顯示原因
            self.place, self.items = place, []
            self._render()
            self.crumb.set(self.source.crumbs(place), self.source.path_text(place))
            self.notice.configure(text=self._why(e))
            self.notice.place(relx=0.5, rely=0.4, anchor="center")
            self._update_status()

    @staticmethod
    def _why(e: Exception) -> str:
        from ..cloud.base import AuthError

        if isinstance(e, AuthError):
            return f"{e}\n請到「帳號」頁用 115 App 掃碼登入"
        return f"無法開啟：{e}"

    def _error(self, title: str, e: Exception) -> None:
        self.win.set_status(f"{title}：{e}")
        messagebox.showerror(title, str(e), parent=self.win.root)

    def _set_busy(self, delta: int) -> None:
        self._busy = max(0, self._busy + delta)
        if self.winfo_exists():
            self._update_status()

    def _show(self, place, items: list[Item], record: bool, select: str | None) -> None:
        changed = place != self.place
        self.place, self.items = place, items
        if changed:
            self.compare = {}
        if record and (not self.history or self.history[self.hist_pos] != place):
            del self.history[self.hist_pos + 1:]
            self.history.append(place)
            self.hist_pos = len(self.history) - 1
        self.notice.place_forget()
        self.btn_back.state(["!disabled"] if self.hist_pos > 0 else ["disabled"])
        self.btn_fwd.state(["!disabled"] if self.hist_pos < len(self.history) - 1 else ["disabled"])
        self.crumb.set(self.source.crumbs(place), self.source.path_text(place))
        self._show_columns()
        self._render()
        if select:
            self.select_names([select])
        self._tree_sync(place, [it for it in items if it.is_dir])
        self._update_star()
        self._update_status()
        self._update_quota()
        self.group.retitle(self)
        self.page.remember()

    # ------------------------------------------------------------ 清單
    def sort_by(self, col: str) -> None:
        self.sort = (col, not self.sort[1] if self.sort[0] == col else False)
        names = [it.name for it in self.selected()]
        self._render()
        self.select_names(names)

    def _sorted(self) -> list[Item]:
        col, reverse = self.sort
        keys = {"#0": lambda it: name_key(it.name), "size": lambda it: (it.size, name_key(it.name)),
                "date": lambda it: (it.mtime, name_key(it.name)), "sha1": lambda it: (it.sha1, name_key(it.name)),
                "cmp": lambda it: (self.compare.get(name_key(it.name), ""), name_key(it.name))}
        key = keys.get(col, keys["#0"])
        dirs = sorted((it for it in self.items if it.is_dir), key=key if col in ("#0", "date") else keys["#0"],
                      reverse=reverse and col in ("#0", "date"))
        files = sorted((it for it in self.items if not it.is_dir), key=key, reverse=reverse)
        return dirs + files  # 資料夾永遠排在前面

    def _render(self) -> None:
        t = self.tree
        t.delete(*t.get_children())
        self.rows.clear()
        for col in ("#0", "size", "date", "sha1", "cmp"):
            arrow = theme.icon("sort_down" if self.sort[1] else "sort_up", 12) if col == self.sort[0] else ""
            t.heading(col, image=arrow)
        for n, it in enumerate(self._sorted()):
            iid = f"r{n}"
            self.rows[iid] = it
            img = theme.icon(it.icon) if it.icon else theme.file_icon(it.name, it.is_dir)
            t.insert("", "end", iid=iid, text=" " + it.name, image=img, values=(
                "" if it.is_dir else human_size(it.size), fmt_date(it.mtime), it.sha1[:12],
                COMPARE_LABELS.get(self.compare.get(name_key(it.name), ""), "")))

    def selected(self) -> list[Item]:
        return [self.rows[i] for i in self.tree.selection() if i in self.rows]

    def select_names(self, names) -> None:
        want = {name_key(n) for n in names}
        ids = [i for i, it in self.rows.items() if name_key(it.name) in want]
        self.tree.selection_set(ids)
        if ids:
            self.tree.see(ids[0])
            self.tree.focus(ids[0])

    def item_named(self, name: str) -> Item | None:
        return next((it for it in self.items if name_key(it.name) == name_key(name)), None)

    def _update_status(self) -> None:
        sel = self.selected()
        if self._busy:
            text = "讀取中…"
        elif sel:
            size = sum(it.size for it in sel if not it.is_dir)
            text = f"已選取 {len(sel)} 個項目" + (f"：{human_size(size)}" if size else "")
        else:
            text = f"{len(self.items)} 個項目"
        self.status.configure(text=text)

    def set_compare(self, result: dict[str, str]) -> None:
        self.compare = result
        self._show_columns()
        names = [it.name for it in self.selected()]
        self._render()
        self.select_names(names)

    # ------------------------------------------------------------ 可用空間
    def _update_quota(self) -> None:
        src, place = self.source, self.place
        if src.kind == "local":
            self._set_quota(src.free_space(place))
            return
        cached = self.page.quota_cache.get(src.account_id)
        if cached is not None:
            self._set_quota(cached)
            return

        def work() -> None:
            try:
                q = src.free_space()
            except Exception:  # noqa: BLE001 - 只是顯示用
                q = None
            self.page.quota_cache[src.account_id] = q
            self.win.ui.call(lambda: self.winfo_exists() and self.source is src and self._set_quota(q))

        threading.Thread(target=work, daemon=True).start()

    def _set_quota(self, q: tuple[int, int] | None) -> None:
        self._quota = q
        if not q or q[0] <= 0:
            self.btn_quota.configure(text="", image="")
            return
        total, used = q
        self.btn_quota.configure(text=f"{human_size(total - used)} 可用 ▾", compound="left",
                                 image=theme.icon(f"pie:{round(20 * used / total)}"))

    def _quota_menu(self) -> None:
        if self._quota:
            total, used = self._quota
            popup_below(self.btn_quota, [
                {"label": f"總容量：{human_size(total)}", "enabled": False},
                {"label": f"已使用：{human_size(used)}（{used * 100 // max(1, total)}%）", "enabled": False},
                {"label": f"可用：{human_size(total - used)}", "enabled": False},
                None, {"label": "重新讀取", "icon": "refresh", "command": self._requery_quota}])

    def _requery_quota(self) -> None:
        self.page.quota_cache.pop(self.source.account_id, None)
        self._update_quota()

    def _fit_columns(self) -> None:
        """名稱欄吸收剩下的寬度：面板變窄時先縮名稱欄，大小與日期才不會被擠出畫面。"""
        t = self.tree
        shown = t.cget("displaycolumns")
        cols = list(t.cget("columns")) if "#all" in shown else list(shown)
        rest = sum(int(t.column(c, "width")) for c in cols)
        want = max(px(120), t.winfo_width() - rest - 2)
        if t.winfo_width() > 1 and abs(int(t.column("#0", "width")) - want) > 1:
            t.column("#0", width=want)

    # ------------------------------------------------------------ 資料夾樹
    def _tree_reset(self) -> None:
        self.ftree.delete(*self.ftree.get_children())
        self._nodes.clear()
        self._node_of.clear()
        self._tree_loaded.clear()
        self._images = [theme.icon(self.source.icon)]
        root = self.source.root
        iid = self.ftree.insert("", "end", text=" " + self.source.title, image=self._images[0], open=True)
        self._nodes[iid] = root
        self._node_of[root] = iid

    def _tree_node(self, parent_iid: str, place, name: str, icon: str | None = None) -> str:
        iid = self._node_of.get(place)
        if iid is None or not self.ftree.exists(iid):
            iid = self.ftree.insert(parent_iid, "end", text=" " + name, image=theme.icon(icon or "folder"))
            self.ftree.insert(iid, "end", iid=iid + "_x", text="")  # 佔位：讓資料夾可以展開
            self._nodes[iid] = place
            self._node_of[place] = iid
        return iid

    def _tree_fill(self, iid: str, place, dirs: list[Item]) -> None:
        """把 place 這個節點底下的資料夾換成 dirs（保留已展開的節點）。"""
        ft = self.ftree
        if not ft.exists(iid):
            return
        self._tree_loaded.add(iid)
        keep = set()
        for it in sorted(dirs, key=lambda d: name_key(d.name)):
            child = self.source.child(place, it)
            keep.add(self._tree_node(iid, child, it.name, it.icon))
        on_path = {pl for _label, pl in self.source.crumbs(self.place)}  # 目前所在的那一支不能刪（例如隱藏資料夾）
        for kid in ft.get_children(iid):
            if kid not in keep and self._nodes.get(kid) in on_path:
                keep.add(kid)
            if kid not in keep:
                for sub in self._descendants(kid):
                    self._node_of.pop(self._nodes.pop(sub, None), None)
                ft.delete(kid)
        ordered = sorted(keep, key=lambda k: name_key(ft.item(k, "text")))
        for i, k in enumerate(ordered):
            ft.move(k, iid, i)

    def _descendants(self, iid: str) -> list[str]:
        out, stack = [], [iid]
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(self.ftree.get_children(cur))
        return out

    def _tree_sync(self, place, dirs: list[Item]) -> None:
        """清單換資料夾後，讓資料夾樹展開並選到同一個位置。"""
        if not self._nodes:
            self._tree_reset()
        ft = self.ftree
        crumbs = self.source.crumbs(place)
        iid = self._node_of[crumbs[0][1]]
        for label, pl in crumbs[1:]:
            ft.item(iid, open=True)
            parent = iid
            iid = self._tree_node(parent, pl, label, "drive" if self.source.kind == "local" and pl == crumbs[1][1] else None)
            if ft.exists(parent + "_x"):
                ft.delete(parent + "_x")
            if parent not in self._tree_loaded:  # 上層只有這一支：在背景補上同一層的其他資料夾
                self._tree_load(parent)
        self._tree_fill(iid, place, dirs)
        ft.item(iid, open=True)
        self._tree_guard = True
        try:
            ft.selection_set(iid)
            ft.see(iid)
        finally:
            self.after_idle(lambda: setattr(self, "_tree_guard", False))

    def _tree_open(self, _event=None) -> None:
        iid = self.ftree.focus()
        if iid in self._nodes and iid not in self._tree_loaded:
            self._tree_load(iid)

    def _tree_load(self, iid: str) -> None:
        """在背景讀取一個節點底下的資料夾。"""
        place = self._nodes.get(iid)
        if place is None:
            return
        self._tree_loaded.add(iid)
        src, hidden = self.source, bool(self.page.hidden_var.get())

        def fill(items) -> None:
            if self.source is not src or not self.winfo_exists() or not self.ftree.exists(iid):
                return
            if items is None:  # 讀不到：下次展開時再試
                self._tree_loaded.discard(iid)
                if self.ftree.exists(iid + "_x") and len(self.ftree.get_children(iid)) > 1:
                    self.ftree.delete(iid + "_x")
                return
            self._tree_fill(iid, place, [it for it in items if it.is_dir])

        def work() -> None:
            try:
                items = src.list(place, hidden)
            except Exception:  # noqa: BLE001
                items = None
            self.win.ui.call(lambda: fill(items))

        threading.Thread(target=work, daemon=True).start()

    def _tree_select(self, _event=None) -> None:
        if self._tree_guard:
            return
        sel = self.ftree.selection()
        place = self._nodes.get(sel[0]) if sel else None
        if place is not None and place != self.place:
            self.go(place)

    # ------------------------------------------------------------ 位置、書籤、檢視選單
    def connect(self, source, place=None) -> None:
        """把這個分頁改連到另一個位置（本機或某個 115 帳號）。"""
        self.source = source
        self.history, self.hist_pos, self.compare = [], -1, {}
        self.items, self._quota = [], None
        self.btn_loc.configure(image=theme.icon_pair(source.icon))
        self._nodes.clear()
        self._node_of.clear()
        self._tree_loaded.clear()
        self.ftree.delete(*self.ftree.get_children())
        self.place = source.root
        self.go(place if place is not None else source.root, follow=False)

    def location_items(self) -> list:
        marks = [{"label": b.get("name") or b.get("path", ""), "icon": "computer" if b.get("kind") == "local" else "cloud",
                  "command": lambda b=b: self.page.open_bookmark(self, b)} for b in self.win.config.settings.bookmarks]
        marks += [None] * bool(marks) + [{"label": "編輯書籤…", "icon": "rename", "command": self.win.edit_bookmarks}]
        items: list = [{"label": "本機", "icon": "computer", "command": lambda: self.connect(self.page.local)},
                       {"label": "書籤", "icon": "star_on", "submenu": marks}, None]
        for src in self.page.cloud_sources():
            items.append({"label": src.title + " － 115 網盤", "icon": "cloud",
                          "command": lambda s=src: self.connect(s)})
        return items

    def _location_menu(self) -> None:
        popup_below(self.btn_loc, self.location_items())

    def bookmark_items(self) -> list:
        on = self._bookmark() is not None
        items: list = [{"label": "從書籤移除這個資料夾" if on else "把這個資料夾加入書籤",
                        "icon": "star" if on else "star_on", "command": self.toggle_bookmark}, None]
        for b in self.win.config.settings.bookmarks:
            items.append({"label": b.get("name") or b.get("path", ""),
                          "icon": "computer" if b.get("kind") == "local" else "cloud",
                          "command": lambda b=b: self.page.open_bookmark(self, b)})
        if self.win.config.settings.bookmarks:
            items.append(None)
        items.append({"label": "編輯書籤…", "icon": "rename", "command": self.win.edit_bookmarks})
        return items

    def _bookmark_menu(self) -> None:
        popup_below(self.btn_marks, self.bookmark_items())

    def _bookmark(self) -> dict | None:
        path = self.source.path_text(self.place)
        for b in self.win.config.settings.bookmarks:
            if b.get("kind") == self.source.kind and b.get("account_id", "") == self.source.account_id \
                    and b.get("path") == path:
                return b
        return None

    def toggle_bookmark(self) -> None:
        s = self.win.config.settings
        cur = self._bookmark()
        if cur is not None:
            s.bookmarks.remove(cur)
        else:
            path = self.source.path_text(self.place)
            if self.source.kind == "local" and not path:
                messagebox.showinfo("書籤", "請先開啟一個資料夾", parent=self.win.root)
                return
            s.bookmarks.append({"name": f"{self.source.title} － {path}", "kind": self.source.kind,
                                "account_id": self.source.account_id, "path": path})
        self.win.config.save()
        self._update_star()

    def _update_star(self) -> None:
        self.btn_star.configure(image=theme.icon_pair("star_on" if self._bookmark() is not None else "star"))

    def view_items(self) -> list:
        ui = self.win.config.settings.ui
        shown = ui.get("columns") or {}

        def flip(col: str) -> None:
            cur = dict(ui.get("columns") or {})
            cur[col] = not cur.get(col, col != "sha1")
            ui["columns"] = cur
            self.win.save_ui()
            self.page.each_panel(lambda p: p._show_columns())

        def conflict(mode: str) -> None:
            self.win.config.settings.manual_conflict = mode
            self.win.config.save()

        cur = self.win.config.settings.manual_conflict
        return [{"label": "顯示「大小」", "check": shown.get("size", True), "command": lambda: flip("size")},
                {"label": "顯示「日期」", "check": shown.get("date", True), "command": lambda: flip("date")},
                {"label": "顯示「SHA-1」（115）", "check": shown.get("sha1", False), "command": lambda: flip("sha1")},
                None,
                {"label": "同名但內容不同時", "icon": "conflict", "submenu": [
                    {"label": label, "check": mode == cur, "command": lambda m=mode: conflict(m)}
                    for mode, label in CONFLICT_LABELS.items()]}]

    def _view_menu(self) -> None:
        popup_below(self.btn_view, self.view_items())

    # ------------------------------------------------------------ 右鍵選單
    def menu_items(self) -> list:
        sel = self.selected()
        one = len(sel) == 1
        other = self.page.other_panel(self)
        local = self.source.kind == "local"
        in_folder = bool(self.place) if local else True
        clip = self.page.clipboard
        if local:
            send = "上傳到 115（先秒傳）" if other is not None and other.source.kind == "cloud" else "複製到另一個面板"
            send_icon = "upload"
        else:
            send = "下載到本機" if other is not None and other.source.kind == "local" else "複製到另一個面板"
            send_icon = "download"
        items: list = [
            {"label": "開啟", "icon": "open", "enabled": one, "command": self.open_selected},
            None,
            {"label": send, "icon": send_icon, "enabled": bool(sel) and other is not None,
             "command": lambda: self.page.transfer(self, other)},
            None,
            {"label": "剪下", "icon": "cut", "enabled": bool(sel), "command": lambda: self.page.copy(self, cut=True)},
            {"label": "複製", "icon": "copy", "enabled": bool(sel), "command": lambda: self.page.copy(self)},
            {"label": "貼上", "icon": "paste", "enabled": bool(clip) and in_folder, "command": lambda: self.page.paste(self)},
            None,
            {"label": "重新命名", "icon": "rename", "enabled": one, "command": self.rename},
            {"label": "刪除", "icon": "delete", "enabled": bool(sel), "command": self.delete},
        ]
        if not local:
            items.append({"label": "移動到…", "icon": "move", "enabled": bool(sel), "command": self.move_to})
        items += [None,
                  {"label": "在檔案總管開啟" if local else "在 115 瀏覽器開啟", "icon": "open" if local else "globe",
                   "enabled": in_folder, "command": self.open_external},
                  {"label": "複製路徑", "icon": "copy", "enabled": in_folder, "command": self.copy_path},
                  None,
                  {"label": "新資料夾", "icon": "newfolder", "enabled": in_folder, "command": self.new_folder},
                  None,
                  {"label": "內容", "icon": "info", "enabled": one, "command": self.properties}]
        return items

    def menu(self, label: str) -> None:
        """（鍵盤與測試用）執行右鍵選單中的一項。"""
        it = menu_item(self.menu_items(), label)
        if not it.get("enabled", True):
            raise RuntimeError(f"選單項目「{label}」目前不能使用")
        it["command"]()

    def _context(self, e) -> None:
        self.tree.focus_set()
        row = self.tree.identify_row(e.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        elif not row:
            self.tree.selection_set(())
        popup(self.tree, self.menu_items(), e.x_root, e.y_root)

    # ------------------------------------------------------------ 拖放（拖到另一個面板 = 傳輸）
    def _press(self, e):
        self._drag = (e.x_root, e.y_root, False)
        self._press_sel = None
        row = self.tree.identify_row(e.y)
        sel = self.tree.selection()
        if row and row in sel and len(sel) > 1 and not e.state & 0x5:
            self._press_sel = (row, sel)  # 按在已選取的多個項目上：先不要取消其他選取，可能是要拖曳
            self.tree.focus_set()
            self.tree.focus(row)
            return "break"
        return None

    def _motion(self, e):
        if self._drag and not self._drag[2]:
            x0, y0, _ = self._drag
            if abs(e.x_root - x0) + abs(e.y_root - y0) > px(8) and self.tree.selection():
                self._drag = (x0, y0, True)
                self.tree.configure(cursor="exchange")
        return "break"  # 不使用清單內建的「拖曳選取」

    def _release(self, e):
        drag, pressed = self._drag, self._press_sel
        self._drag = self._press_sel = None
        self.tree.configure(cursor="")
        if drag and drag[2]:
            target = self.page.panel_at(e.x_root, e.y_root)
            if target is not None and target is not self:
                self.page.transfer(self, target)
        elif pressed:
            self.tree.selection_set(pressed[0])

    # ------------------------------------------------------------ 檔案操作
    def _folder_ready(self) -> bool:
        if self.source.kind == "local" and not self.place:
            messagebox.showinfo("請先開啟資料夾", "請先在這個面板開啟一個資料夾", parent=self.win.root)
            return False
        return True

    def _cloud_bg(self, title: str, fn, select: str | None = None) -> None:
        """執行 115 的操作（背景），完成後重新整理。"""
        self._set_busy(1)

        def work() -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.win.ui.call(lambda err=e: (self._set_busy(-1), self._error(title + "失敗", err)))
                return
            self.page.quota_cache.pop(self.source.account_id, None)
            self.win.ui.call(lambda: (self._set_busy(-1), self.go(self.place, record=False, follow=False, select=select)))

        threading.Thread(target=work, daemon=True).start()

    def open_selected(self) -> None:
        sel = self.selected()
        if len(sel) != 1:
            return
        it = sel[0]
        if it.is_dir:
            self.go(self.source.child(self.place, it))
        elif self.source.kind == "local":
            try:
                open_file(it.key)
            except OSError as e:
                self._error("無法開啟", e)
        else:
            self.properties()

    def new_folder(self) -> None:
        if not self._folder_ready():
            return
        name = simpledialog.askstring("新資料夾", "資料夾名稱：", parent=self.win.root)
        name = (name or "").strip()
        if not name:
            return
        if why := unsafe_name(name):
            messagebox.showerror("名稱不能使用", why, parent=self.win.root)
            return
        if self.source.kind == "local":
            try:
                os.makedirs(long_path(os.path.join(self.place, name)), exist_ok=True)
            except OSError as e:
                self._error("無法建立資料夾", e)
            self.go(self.place, record=False, follow=False, select=name)
        else:
            c, cid = self.source.client, self.source.cid(self.place)
            self._cloud_bg("建立資料夾", lambda: c.ensure_child_dir(cid, name), select=name)

    def rename(self) -> None:
        sel = self.selected()
        if len(sel) != 1 or not self._folder_ready():
            return
        it = sel[0]
        new = simpledialog.askstring("重新命名", "新名稱：", initialvalue=it.name, parent=self.win.root)
        new = (new or "").strip()
        if not new or new == it.name:
            return
        if why := unsafe_name(new):
            messagebox.showerror("名稱不能使用", why, parent=self.win.root)
            return
        if self.source.kind == "local":
            dest = os.path.join(self.place, new)
            try:
                if os.path.lexists(long_path(dest)) and os.path.normcase(dest) != os.path.normcase(it.key):
                    raise OSError(f"已有同名項目：{new}")
                os.rename(long_path(it.key), long_path(dest))
            except OSError as e:
                self._error("無法重新命名", e)
            self.go(self.place, record=False, follow=False, select=new)
        else:
            c = self.source.client
            self._cloud_bg("重新命名", lambda: c.rename(it.key, new), select=new)

    def delete(self) -> None:
        sel = self.selected()
        if not sel or not self._folder_ready():
            return
        if self.source.kind == "local":
            if not messagebox.askyesno("刪除", f"將 {len(sel)} 個項目移到資源回收筒？\n（網路磁碟上的檔案 Windows 可能會直接刪除）",
                                       parent=self.win.root):
                return
            errors = []
            for it in sel:
                try:
                    manual.send_to_recycle_bin(it.key)
                except OSError as e:
                    errors.append(str(e))
            if errors:
                messagebox.showerror("部分失敗", "\n".join(errors[:10]), parent=self.win.root)
            self.go(self.place, record=False, follow=False)
        else:
            if not messagebox.askyesno("刪除", f"刪除 115 上 {len(sel)} 個項目？（會進入 115 回收站）", parent=self.win.root):
                return
            c, cid, fids = self.source.client, self.source.cid(self.place), [it.key for it in sel]
            self._cloud_bg("刪除", lambda: c.delete(fids, cid))

    def move_to(self) -> None:
        sel = self.selected()
        if not sel or self.source.kind != "cloud":
            return
        dest = self.win.pick_cloud_folder(self.source.account_id, "移動到", self.source.path_text(self.place))
        if not dest:
            return
        c, fids = self.source.client, [it.key for it in sel]
        self._cloud_bg("移動", lambda: c.move(fids, c.ensure_path(dest)))

    def open_external(self) -> None:
        if not self._folder_ready():
            return
        if self.source.kind == "local":
            sel = self.selected()
            try:
                open_local(sel[0].key if len(sel) == 1 else self.place)
            except OSError as e:
                self._error("無法開啟", e)
        else:
            sel = self.selected()
            cid = sel[0].key if len(sel) == 1 and sel[0].is_dir else self.source.cid(self.place)
            open_in_115_browser(self.source.client.web_url(cid), self.win.config.settings.browser_path)

    def copy_path(self) -> None:
        sel = self.selected()
        base = self.source.path_text(self.place)
        if self.source.kind == "local":
            paths = [it.key for it in sel] or [base]
        else:
            paths = [base.rstrip("/") + "/" + it.name for it in sel] or [base]
        self.clipboard_clear()
        self.clipboard_append("\n".join(paths))
        self.win.set_status(f"已複製 {len(paths)} 個路徑")

    def properties(self) -> None:
        sel = self.selected()
        if len(sel) == 1:
            self.win.show_properties(self, sel[0])


# =========================================================================== 分頁群組（一邊的面板）
class Chooser(tk.Frame):
    """新分頁的內容：選擇要連線的位置（本機、115 帳號、書籤）。"""

    def __init__(self, group: "PanelGroup"):
        p = theme.P
        super().__init__(group.body, background=p.bg, highlightthickness=1, highlightbackground=p.line, borderwidth=0)
        self.group, self.page = group, group.page
        ttk.Label(self, text="選擇要連線的位置", style="Title.TLabel").pack(anchor="w", padx=px(14), pady=(px(12), px(6)))
        self.tree = ttk.Treeview(self, show="tree", selectmode="browse", height=6)
        self.tree.pack(fill="both", expand=True, padx=px(8), pady=(0, px(8)))
        self.choices: dict[str, tuple] = {}
        self.fill()
        self.tree.bind("<Double-Button-1>", lambda e: self.choose())
        self.tree.bind("<Return>", lambda e: self.choose())
        self.tree.bind("<ButtonRelease-1>", lambda e: self.choose())

    def fill(self) -> None:
        t = self.tree
        t.delete(*t.get_children())
        self.choices.clear()
        page = self.page
        iid = t.insert("", "end", text="  本機（電腦與 NAS 的資料夾）", image=theme.icon("computer"))
        self.choices[iid] = (page.local, None)
        for src in page.cloud_sources():
            iid = t.insert("", "end", text=f"  {src.title} － 115 網盤", image=theme.icon("cloud"))
            self.choices[iid] = (src, None)
        for b in page.win.config.settings.bookmarks:
            iid = t.insert("", "end", text="  ★ " + (b.get("name") or b.get("path", "")),
                           image=theme.icon("computer" if b.get("kind") == "local" else "cloud"))
            self.choices[iid] = ("bookmark", b)

    def choose(self, iid: str | None = None) -> None:
        sel = [iid] if iid else self.tree.selection()
        if not sel or sel[0] not in self.choices:
            return
        src, extra = self.choices[sel[0]]
        self.group.connect_chooser(self, src, extra)


class PanelGroup(ttk.Frame):
    """一邊的面板：分頁列 ＋ 目前分頁的內容。"""

    def __init__(self, pair: "PanelPair", name: str):
        super().__init__(pair)
        self.pair, self.page, self.name = pair, pair.page, name
        self.tabs = TabStrip(self, self.select, self.close_tab, self.add_tab)
        self.tabs.pack(fill="x")
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.panels: list[tk.Frame] = []  # FilePanel 或 Chooser

    @property
    def active(self) -> FilePanel | None:
        i = self.tabs.active
        w = self.panels[i] if 0 <= i < len(self.panels) else None
        return w if isinstance(w, FilePanel) else None

    def add_tab(self, source=None, place=None, select: bool = True) -> tk.Frame:
        if source is None:
            w: tk.Frame = Chooser(self)
            self.tabs.add("新分頁", "plus")
        else:
            self.tabs.add(source.title, source.icon)
            w = FilePanel(self, source, place)
        self.panels.append(w)
        if select or len(self.panels) == 1:
            self.select(len(self.panels) - 1)
        self.page.remember()
        return w

    def connect_chooser(self, chooser: Chooser, src, extra) -> None:
        i = self.panels.index(chooser)
        place = None
        if src == "bookmark":
            src, place = self.page.bookmark_target(extra)
            if src is None:
                return
        if src.kind == "cloud" and isinstance(place, str):
            panel = FilePanel(self, src)
            panel.go_text(place)
        else:
            panel = FilePanel(self, src, place)
        self.panels[i] = panel
        chooser.destroy()
        self.tabs.set(i, src.title, src.icon)
        self.select(i)
        self.page.remember()

    def retitle(self, panel: FilePanel) -> None:
        if panel in self.panels:
            self.tabs.set(self.panels.index(panel), panel.source.title, panel.source.icon)

    def select(self, index: int) -> None:
        if not 0 <= index < len(self.panels):
            return
        for w in self.panels:
            w.pack_forget()
        self.panels[index].pack(fill="both", expand=True)
        self.tabs.select(index)
        w = self.panels[index]
        if isinstance(w, FilePanel):
            self.page.set_active(w)
        self.page.remember()

    def close_tab(self, index: int) -> None:
        if len(self.panels) <= 1 or not 0 <= index < len(self.panels):
            return  # 每一邊至少留一個分頁
        w = self.panels.pop(index)
        self.page.forget_panel(w)
        w.destroy()
        self.tabs.remove(index)
        self.select(min(index, len(self.panels) - 1))

    def state(self) -> dict:
        tabs = []
        for w in self.panels:
            if isinstance(w, FilePanel):
                tabs.append({"kind": w.source.kind, "account_id": w.source.account_id,
                             "path": w.source.path_text(w.place)})
        return {"tabs": tabs, "active": max(0, self.tabs.active)}


class PanelPair(ttk.Frame):
    """左右兩個面板與中間的傳輸箭頭。"""

    def __init__(self, page: "ExplorePage", name: str):
        super().__init__(page)
        self.page, self.name = page, name
        self.link_var = tk.BooleanVar(self, value=False)
        self.left = PanelGroup(self, name + "L")
        self.right = PanelGroup(self, name + "R")
        self.gutter = ttk.Frame(self)
        Toggle(self.gutter, self.link_var, "link", icon_on="link_on", style="Gutter.Tool.TButton",
               hint="連動瀏覽：一邊進入子資料夾時，另一邊也進入同名的資料夾").pack(pady=(px(30), px(2)))
        tool_button(self.gutter, "sync_small", command=lambda: page.sync_folders(self), style="Gutter.Tool.TButton",
                    hint="把兩邊目前的資料夾建立為同步").pack(pady=(0, px(18)))
        self.btn_right = tool_button(self.gutter, "tri_right", style="Gutter.Tool.TButton",
                                     command=lambda: page.transfer(self.left.active, self.right.active),
                                     hint="把左邊選取的項目傳到右邊（本機 → 115 是上傳，先秒傳）")
        self.btn_right.pack(pady=px(2))
        self.btn_left = tool_button(self.gutter, "tri_left", style="Gutter.Tool.TButton",
                                    command=lambda: page.transfer(self.right.active, self.left.active),
                                    hint="把右邊選取的項目傳到左邊（115 → 本機 是下載）")
        self.btn_left.pack(pady=px(2))
        self.columnconfigure(0, weight=1, uniform="pair")
        self.columnconfigure(2, weight=1, uniform="pair")
        self.rowconfigure(0, weight=1)
        self.left.grid(row=0, column=0, sticky="nsew")
        self.gutter.grid(row=0, column=1, sticky="ns")
        self.right.grid(row=0, column=2, sticky="nsew")

    def single(self, on: bool) -> None:
        """單一面板：只顯示左邊。"""
        if on:
            self.gutter.grid_remove()
            self.right.grid_remove()
            self.columnconfigure(2, weight=0, uniform="")
        else:
            self.columnconfigure(2, weight=1, uniform="pair")
            self.gutter.grid()
            self.right.grid()

    def other(self, group: PanelGroup) -> PanelGroup:
        return self.right if group is self.left else self.left

    def follow(self, panel: FilePanel, prev, place) -> None:
        """連動瀏覽：另一邊跟著進入同名的子資料夾，或一起回上一層。"""
        if not self.link_var.get() or not self.right.winfo_viewable():
            return
        other = self.other(panel.group).active
        if other is None or other is panel:
            return
        src = panel.source
        if src.parent(place) == prev and prev is not None:  # 進入子資料夾
            name = src.crumbs(place)[-1][0]
            it = other.item_named(name)
            if it is not None and it.is_dir:
                other.go(other.source.child(other.place, it), follow=False)
        elif src.parent(prev) == place:  # 回上一層
            up = other.source.parent(other.place)
            if up is not None:
                other.go(up, follow=False)


# =========================================================================== 「瀏覽」頁
class ExplorePage(ttk.Frame):
    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.engine = win, win.engine
        s = win.config.settings
        self.local = LocalSource(win.config)
        self._clouds: dict[str, CloudSource] = {}
        self.instant_var = tk.BooleanVar(self, value=bool(s.ui.get("instant_only", False)))
        self.hidden_var = tk.BooleanVar(self, value=bool(s.show_hidden))
        self.quota_cache: dict[str, tuple | None] = {}
        self.clipboard: dict | None = None
        self._active: FilePanel | None = None
        self._restoring = True
        self.pairs = [PanelPair(self, "A"), PanelPair(self, "B")]
        self.view = s.ui.get("view", "two") if s.ui.get("view") in ("one", "two", "four") else "two"
        self._restore()
        self._restoring = False
        self.set_view(self.view)

    # ------------------------------------------------------------ 位置來源
    def cloud_sources(self) -> list[CloudSource]:
        out = []
        for a in self.win.config.accounts:
            src = self._clouds.get(a.id)
            if src is None:
                src = self._clouds[a.id] = CloudSource(self.engine, a)
            src.title = a.label
            out.append(src)
        return out

    def cloud_source(self, account_id: str) -> CloudSource | None:
        return next((s for s in self.cloud_sources() if s.account_id == account_id), None)

    def bookmark_target(self, b: dict):
        """書籤 → (位置來源, 位置)。115 的位置是路徑文字，要連線後才能解析。"""
        if b.get("kind") == "local":
            return self.local, b.get("path", "")
        src = self.cloud_source(b.get("account_id", "default"))
        if src is None:
            messagebox.showwarning("書籤", "這個書籤的 115 帳號已不存在", parent=self.win.root)
            return None, None
        return src, b.get("path", "/")

    def open_bookmark(self, panel: FilePanel, b: dict) -> None:
        src, place = self.bookmark_target(b)
        if src is None:
            return
        if src.kind == "local":
            panel.connect(src, place)
        else:
            if not panel.source.same(src):
                panel.connect(src)
            panel.go_text(place)

    # ------------------------------------------------------------ 檢視方式與狀態
    def groups(self) -> list[PanelGroup]:
        n = {"one": 1, "two": 2, "four": 4}[self.view]
        return [g for pair in self.pairs for g in (pair.left, pair.right)][:n]

    def refresh_visible(self) -> None:
        """重新讀取畫面上看得到的面板（同步完成後兩邊的內容都可能變了）。"""
        for g in self.groups():
            if isinstance(g.active, FilePanel):
                g.active.refresh()

    def each_panel(self, fn) -> None:
        for pair in self.pairs:
            for g in (pair.left, pair.right):
                for w in g.panels:
                    if isinstance(w, FilePanel):
                        fn(w)

    def set_view(self, view: str) -> None:
        self.view = view
        for pair in self.pairs:
            pair.pack_forget()
        self.pairs[0].single(view == "one")
        self.pairs[0].pack(fill="both", expand=True, padx=px(4), pady=(px(2), px(2)))
        if view == "four":
            self.pairs[1].pack(fill="both", expand=True, padx=px(4), pady=(0, px(2)))
        self.remember()

    def _default_tabs(self, group_name: str) -> list[dict]:
        active = self.win.config.active_mappings()
        if group_name.endswith("L"):
            return [{"kind": "local", "path": active[0].local_path if active else ""}]
        return [{"kind": "cloud", "account_id": active[0].account_id if active else "default",
                 "path": active[0].cloud_path if active else "/"}]

    def _restore(self) -> None:
        saved = self.win.config.settings.ui.get("panels") or {}
        for pair in self.pairs:
            for g in (pair.left, pair.right):
                st = saved.get(g.name) or {}
                tabs = st.get("tabs") or self._default_tabs(g.name)
                for tab in tabs:
                    self._open_tab(g, tab)
                if not g.panels:
                    self._open_tab(g, self._default_tabs(g.name)[0])
                g.select(min(int(st.get("active", 0) or 0), len(g.panels) - 1))

    def _open_tab(self, g: PanelGroup, tab: dict) -> None:
        if tab.get("kind") == "local":
            path = tab.get("path") or ""
            g.add_tab(self.local, path if not path or os.path.isdir(long_path(path)) else "", select=False)
            return
        src = self.cloud_source(tab.get("account_id", "default"))
        if src is None:
            return
        panel = g.add_tab(src, select=False)
        path = tab.get("path") or "/"
        if path != "/" and isinstance(panel, FilePanel) and (self.win.app.demo or src.client.is_logged_in()):
            panel.go_text(path)

    def remember(self) -> None:
        """記住目前開啟的分頁與檢視方式，下次開啟時還原。"""
        if self._restoring:
            return
        ui = self.win.config.settings.ui
        ui["view"] = self.view
        ui["panels"] = {g.name: g.state() for pair in self.pairs for g in (pair.left, pair.right)}
        self.win.save_ui()

    def save_options(self) -> None:
        self.win.config.settings.ui["instant_only"] = bool(self.instant_var.get())
        self.win.save_ui()

    def hidden_changed(self) -> None:
        self.win.config.settings.show_hidden = bool(self.hidden_var.get())
        self.win.config.save()
        self.each_panel(lambda p: p.go(p.place, record=False, follow=False))

    def accounts_changed(self) -> None:
        """帳號新增、改名或刪除後：更新分頁標題；已刪除帳號的分頁改回本機。"""
        alive = {s.account_id for s in self.cloud_sources()}
        for a in list(self._clouds):
            if a not in alive:
                del self._clouds[a]

        def fix(p: FilePanel) -> None:
            if p.source.kind == "cloud" and p.source.account_id not in alive:
                p.connect(self.local)
            p.group.retitle(p)

        self.each_panel(fix)
        for pair in self.pairs:
            for g in (pair.left, pair.right):
                for w in g.panels:
                    if isinstance(w, Chooser):
                        w.fill()
        self.quota_cache.clear()

    # ------------------------------------------------------------ 目前的面板
    def set_active(self, panel: FilePanel) -> None:
        self._active = panel

    def forget_panel(self, panel) -> None:
        if self._active is panel:
            self._active = None
        if self.clipboard and self.clipboard["panel"] is panel:
            self.clipboard = None

    @property
    def active(self) -> FilePanel | None:
        if self._active is not None and self._active.winfo_exists() and self._active.winfo_viewable():
            return self._active
        return self.pairs[0].left.active

    def other_panel(self, panel: FilePanel) -> FilePanel | None:
        if self.view == "one":
            return None
        other = panel.group.pair.other(panel.group)
        return other.active if other.winfo_viewable() or not self.winfo_viewable() else None

    def panel_at(self, x_root: int, y_root: int) -> FilePanel | None:
        w = self.winfo_containing(x_root, y_root)
        while w is not None:
            if isinstance(w, FilePanel):
                return w
            w = getattr(w, "master", None)
        return None

    def show_location(self, source, place=None, text: str | None = None, select: str | None = None) -> None:
        """在目前的面板開啟指定位置（搜尋結果的「檢視資料夾」、同步工作的「開啟」）。"""
        want_right = source.kind == "cloud" and self.view != "one"
        group = self.pairs[0].right if want_right else self.pairs[0].left
        panel = group.active
        if panel is None:
            panel = group.add_tab(source)
        if not panel.source.same(source):
            panel.connect(source)
        if text is not None:
            panel.go_text(text)
        elif place is not None:
            panel.go(place, select=select)

    # ------------------------------------------------------------ 剪貼簿
    def copy(self, panel: FilePanel, cut: bool = False):
        items = panel.selected()
        if items:
            self.clipboard = {"panel": panel, "source": panel.source, "items": items, "cut": cut}
            self.win.set_status(f"已{'剪下' if cut else '複製'} {len(items)} 個項目，到目的面板按「貼上」")
        return "break"

    def paste(self, panel: FilePanel):
        clip = self.clipboard
        if clip:
            self.transfer(clip["panel"], panel, items=clip["items"], source=clip["source"], move=clip["cut"])
            if clip["cut"]:
                self.clipboard = None
        return "break"

    # ------------------------------------------------------------ 傳輸
    def transfer(self, src: FilePanel | None, dst: FilePanel | None, items: list[Item] | None = None, source=None,
                 move: bool = False) -> None:
        """把 src 面板選取的項目傳到 dst 面板目前的資料夾。"""
        win = self.win
        if src is None or dst is None or src is dst and items is None:
            messagebox.showinfo("傳輸", "請在兩個面板分別開啟來源與目的資料夾", parent=win.root)
            return
        s = source or src.source
        d = dst.source
        items = list(items if items is not None else src.selected())
        if not items:
            messagebox.showinfo("請選取", "請先選取要傳輸的檔案或資料夾", parent=win.root)
            return
        if d.kind == "local" and not dst.place:
            messagebox.showinfo("請先開啟資料夾", "請先在目的面板開啟一個資料夾", parent=win.root)
            return
        if any(it.icon in ("drive", "nas") and s.kind == "local" and not it.key.strip("\\/") for it in items):
            return
        conflict = win.config.settings.manual_conflict
        engine, n = self.engine, len(items)
        src_text = s.label(src.place) if src.winfo_exists() and src.source is s else s.title
        dst_text = d.label(dst.place)
        if s.kind == "local" and d.kind == "cloud":
            only = bool(self.instant_var.get())
            if move:
                messagebox.showinfo("移動", "本機與 115 之間請用「複製」，確認無誤後再刪除來源。", parent=win.root)
                return
            paths, cid, acc = [it.key for it in items], d.cid(dst.place), d.account_id

            def done(st: dict) -> None:
                msg = f"上傳完成：秒傳 {st['instant']} 個、實際上傳 {st['uploaded']} 個、略過 {st['skipped']} 個、失敗 {st['failed']} 個"
                if only:
                    msg += f"；{st['not_instant']} 個無法秒傳（未上傳）"
                win.set_status(msg)
                self._after_transfer(src, dst, d)

            win.run_bg("上傳", lambda: manual.upload_paths(engine, acc, paths, cid, conflict, instant_only=only), done,
                       batch=("upload", f"{'只秒傳' if only else '上傳'} {n} 個項目", src_text, dst_text))
        elif s.kind == "cloud" and d.kind == "local":
            if move:
                messagebox.showinfo("移動", "本機與 115 之間請用「複製」，確認無誤後再刪除來源。", parent=win.root)
                return
            entries, dest, acc = [it.entry for it in items], dst.place, s.account_id

            def done(st: dict) -> None:
                win.set_status(f"下載完成：{st['downloaded']} 個（其中從本機相同檔案複製 {st['copied']} 個）、"
                               f"略過 {st['skipped']} 個、失敗 {st['failed']} 個")
                self._after_transfer(src, dst, d)

            win.run_bg("下載", lambda: manual.download_entries(engine, acc, entries, dest, conflict), done,
                       batch=("download", f"下載 {n} 個項目", src_text, dst_text))
        elif s.kind == "local" and d.kind == "local":
            paths, dest = [it.key for it in items], dst.place

            def done(st: dict) -> None:
                win.set_status(f"{'移動' if move else '複製'}完成：{st['copied']} 個、略過 {st['skipped']} 個、"
                               f"失敗 {st['failed']} 個")
                self._after_transfer(src, dst, d, also_source=move)

            win.run_bg("移動" if move else "複製", lambda: manual.copy_local(engine, paths, dest, conflict, move=move),
                       done, batch=("copy", f"{'移動' if move else '複製'} {n} 個項目", src_text, dst_text))
        elif s.same(d):
            fids, cid, c = [it.key for it in items], d.cid(dst.place), d.client
            if any(f == p[0] for f in fids for p in dst.place):
                messagebox.showwarning("無法" + ("移動" if move else "複製"), "不能把資料夾放到它自己裡面", parent=win.root)
                return

            def done(_res) -> None:
                win.set_status(f"已在 115 上{'移動' if move else '複製'} {n} 個項目")
                self._after_transfer(src, dst, d, also_source=move)

            win.run_bg("移動" if move else "複製", (lambda: c.move(fids, cid)) if move else (lambda: c.copy(fids, cid)),
                       done, batch=("copy", f"在 115 {'移動' if move else '複製'} {n} 個項目", src_text, dst_text))
        else:
            if move:
                messagebox.showinfo("移動", "不同 115 帳號之間請用「複製」。", parent=win.root)
                return
            entries, cid = [it.entry for it in items], d.cid(dst.place)

            def done(st: dict) -> None:
                win.set_status(f"傳輸完成：秒傳 {st['instant']} 個、實際上傳 {st['uploaded']} 個、略過 {st['skipped']} 個、"
                               f"失敗 {st['failed']} 個")
                self._after_transfer(src, dst, d)

            win.run_bg("帳號間傳輸", lambda: manual.transfer_between_accounts(
                engine, s.account_id, entries, d.account_id, cid, conflict), done,
                batch=("upload", f"帳號間傳輸 {n} 個項目", src_text, dst_text))

    def _after_transfer(self, src: FilePanel, dst: FilePanel, d, also_source: bool = False) -> None:
        if d.kind == "cloud":
            self.quota_cache.pop(d.account_id, None)
        if dst.winfo_exists():
            dst.go(dst.place, record=False, follow=False)
        if also_source and src.winfo_exists():
            src.go(src.place, record=False, follow=False)

    # ------------------------------------------------------------ 比較、建立同步
    def _local_and_cloud(self, pair: PanelPair) -> tuple[FilePanel, FilePanel] | None:
        a, b = pair.left.active, pair.right.active
        if a is None or b is None or self.view == "one":
            return None
        if a.source.kind == "local" and b.source.kind == "cloud":
            return a, b
        if a.source.kind == "cloud" and b.source.kind == "local":
            return b, a
        return None

    def compare(self, panel: FilePanel) -> None:
        both = self._local_and_cloud(panel.group.pair)
        if both is None or not both[0].place:
            messagebox.showinfo("比較兩邊", "請在一邊開啟本機（NAS）資料夾、另一邊開啟 115 資料夾", parent=self.win.root)
            return
        local, cloud = both
        local_dir, acc, cid = local.place, cloud.source.account_id, cloud.source.cid(cloud.place)

        def done(result: dict) -> None:
            for p in (local, cloud):
                if p.winfo_exists():
                    p.set_compare(result)
            n = {k: list(result.values()).count(k) for k in COMPARE_LABELS}
            self.win.set_status(f"比較完成：相同 {n['same']}、不同 {n['different']}、只在本機 {n['nas_only']}、"
                                f"只在 115 {n['cloud_only']}")

        self.win.run_bg("比較", lambda: manual.compare_folders(self.engine, local_dir, acc, cid), done)

    def sync_folders(self, pair: PanelPair) -> None:
        both = self._local_and_cloud(pair)
        if both is None or not both[0].place:
            messagebox.showinfo("建立同步", "請在一邊開啟本機（NAS）資料夾、另一邊開啟 115 資料夾，再按這個按鈕", parent=self.win.root)
            return
        local, cloud = both
        self.win.new_sync(local.place, cloud.source.account_id, cloud.source.path_text(cloud.place))
