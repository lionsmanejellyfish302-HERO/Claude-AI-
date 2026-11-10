"""主工具列的各個頁面：帳號、搜尋、同步、排程、待核對，以及「工具」選單裡的遊戲存檔、重複檔案、紀錄。

版面比照 Air Explorer：每頁上方一列平面工具按鈕，下面是清單（帳號與同步是卡片清單），右邊是選取項目的詳細資料。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import manual
from ..app import make_client, open_in_115_browser
from ..compare import DECISIONS, KIND_LABELS, SAVEDATA_GROUP, available_decisions
from ..config import DELETE_LABELS, DIRECTION_LABELS
from ..engine import Cancelled
from ..paths import local_join, norm_key, rel_parent
from ..scheduler import describe_when, next_run
from . import dialogs, icons, theme
from .theme import px
from .util import (AutoScrollbar, fmt_date, fmt_time, human_size, make_tree, open_file, open_local, popup,
                   scrolled_text, set_text)
from .widgets import Toggle, tool_button

GROUP_STATUS = {"pending": "待核對", "approved": "自動同步（Steam 雲存檔式）", "normal": "一般檔案", "exclude": "不同步"}
DANGEROUS = {"delete_cloud", "delete_all", "delete_extra", "upload_replace", "download_replace", "delete_local"}
HISTORY_ACTIONS = {"upload": "上傳", "upload_replace": "上傳新版", "download": "下載", "delete_cloud": "刪除 115",
                   "delete_local": "刪除 NAS", "move_cloud": "115 搬移", "savedata": "存檔同步"}


class Bar(ttk.Frame):
    """頁面上方的工具列（圖示＋文字的平面按鈕）。"""

    def __init__(self, parent, title: str = ""):
        super().__init__(parent, padding=(px(4), px(3)))
        if title:
            ttk.Label(self, text=title, style="Bold.TLabel").pack(side="left", padx=(px(4), px(10)))

    def button(self, icon: str | None, text: str, command, hint: str = "", side: str = "left") -> ttk.Button:
        b = tool_button(self, icon, text, command, hint)
        b.pack(side=side, padx=(0, px(2)))
        return b

    def sep(self, side: str = "left") -> None:
        theme.line(self, vertical=True).pack(side=side, fill="y", padx=px(4), pady=px(3))


class CardList(ttk.Frame):
    """可捲動的卡片清單（帳號、同步工作）：一次選一張，按兩下開啟。"""

    def __init__(self, parent, on_select=None, on_open=None, on_menu=None):
        super().__init__(parent)
        p = theme.P
        self.on_select, self.on_open, self.on_menu = on_select, on_open, on_menu
        self.canvas = tk.Canvas(self, background=p.bg, highlightthickness=0, borderwidth=0, takefocus=1)
        sb = AutoScrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, background=p.bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=(0, 0, e.width, e.height)))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.cards: dict[str, tk.Frame] = {}
        self.order: list[str] = []
        self.selected: str | None = None
        self._wheel(self.canvas)
        self.canvas.bind("<Up>", lambda e: self._step(-1))
        self.canvas.bind("<Down>", lambda e: self._step(1))
        self.canvas.bind("<Return>", lambda e: self.selected and self.on_open and self.on_open(self.selected))

    def _wheel(self, w: tk.Misc) -> None:
        w.bind("<MouseWheel>", lambda e: self._scroll(-1 if e.delta > 0 else 1), add="+")
        w.bind("<Button-4>", lambda e: self._scroll(-1), add="+")
        w.bind("<Button-5>", lambda e: self._scroll(1), add="+")

    def _scroll(self, n: int) -> None:
        if self.inner.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(n * 2, "units")

    def _step(self, d: int) -> None:
        if not self.order:
            return
        i = self.order.index(self.selected) + d if self.selected in self.order else 0
        self.select(self.order[max(0, min(len(self.order) - 1, i))])

    def clear(self) -> None:
        for c in self.cards.values():
            c.destroy()
        self.cards, self.order = {}, []

    def add(self, key: str) -> tk.Frame:
        """新增一張卡片，回傳要放內容的框（內容用 tk 元件，選取時底色才會跟著變）。"""
        p = theme.P
        card = tk.Frame(self.inner, background=p.bg, highlightthickness=1, highlightbackground=p.line,
                        padx=px(8), pady=px(6))
        card.pack(fill="x", padx=px(6), pady=(px(6), 0))
        self.cards[key] = card
        self.order.append(key)
        return card

    def finish(self) -> None:
        """卡片都建立後呼叫：綁定滑鼠事件並還原選取。"""
        for key, card in self.cards.items():
            for w in self._walk(card):
                self._wheel(w)
                if isinstance(w, (ttk.Button, ttk.Entry)):
                    continue
                w.bind("<Button-1>", lambda e, k=key: self.select(k), add="+")
                w.bind("<Double-Button-1>", lambda e, k=key: self.on_open and self.on_open(k), add="+")
                w.bind("<Button-3>", lambda e, k=key: self._menu(k, e), add="+")
        if self.selected not in self.cards:
            self.selected = self.order[0] if self.order else None
        self._paint()

    @staticmethod
    def _walk(w: tk.Misc):
        yield w
        for c in w.winfo_children():
            yield from CardList._walk(c)

    def _menu(self, key: str, event) -> None:
        self.select(key)
        if self.on_menu:
            self.on_menu(key, event)

    def _paint(self) -> None:
        p = theme.P
        for key, card in self.cards.items():
            on = key == self.selected
            bg = p.sel if on else p.bg
            card.configure(highlightbackground=p.sel_line if on else p.line)
            for w in self._walk(card):
                if isinstance(w, (tk.Frame, tk.Label, tk.Canvas)) and not isinstance(w, ttk.Widget):
                    w.configure(background=bg)

    def select(self, key: str | None, notify: bool = True) -> None:
        if key is not None and key not in self.cards:
            return
        self.selected = key
        self._paint()
        self.canvas.focus_set()
        if key is not None:
            self.update_idletasks()
            card, total = self.cards[key], max(1, self.inner.winfo_height())
            top, bottom = card.winfo_y(), card.winfo_y() + card.winfo_height()
            view = self.canvas.winfo_height()
            y0 = self.canvas.canvasy(0)
            if top < y0:
                self.canvas.yview_moveto(max(0, top - px(6)) / total)
            elif bottom > y0 + view and view > 1:
                self.canvas.yview_moveto((bottom - view + px(6)) / total)
        if notify and self.on_select:
            self.on_select(key)


def _lbl(parent, text: str = "", font: str = "UI", fg: str | None = None, **kw) -> tk.Label:
    p = theme.P
    return tk.Label(parent, text=text, font=font, foreground=fg or p.text, background=p.bg, anchor="w",
                    borderwidth=0, **kw)


def _quota_bar(parent, used: float, total: float, width: int = 150) -> tk.Canvas:
    """容量長條（已用的部分是藍色，快滿時變紅）。"""
    p = theme.P
    w, h = px(width), px(6)
    c = tk.Canvas(parent, width=w, height=h, background=p.bg, highlightthickness=0)
    frac = max(0.0, min(1.0, used / total)) if total else 0.0
    c.create_rectangle(0, 0, w, h, fill=p.trough, outline="")
    if frac:
        c.create_rectangle(0, 0, max(2, round(w * frac)), h, fill=p.danger if frac > 0.9 else p.accent, outline="")
    return c


def _detail_rows(parent, rows: list[tuple[str, str]], wrap: int = 210) -> ttk.Frame:
    """「欄位　值」的兩欄表格（右側詳細資料用）。"""
    f = ttk.Frame(parent)
    f.columnconfigure(1, weight=1)
    for r, (label, value) in enumerate(rows):
        ttk.Label(f, text=label, style="Secondary.TLabel").grid(row=r, column=0, sticky="nw", pady=px(3), padx=(0, px(12)))
        ttk.Label(f, text=value, wraplength=px(wrap), justify="left").grid(row=r, column=1, sticky="w", pady=px(3))
    return f


# =========================================================================== 帳號
class AccountsPage(ttk.Frame):
    """115 帳號（可多個）與 NAS 資料夾。115 的登入方式與 115 電腦版相同：用手機 App 掃碼。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.config, self.engine = win, win.config, win.engine
        self.info: dict[str, dict] = {}  # 帳號 ID → {"state": demo/on/off/error, "name", "space"}
        self.nas_info: dict[str, tuple | None] = {}  # NAS 資料夾 → (總容量, 已使用, 可用)；讀不到是 None
        self._loading: set[str] = set()
        bar = Bar(self)
        bar.pack(fill="x")
        self.btn_add = bar.button("user", "新增帳號", self.add_account, "新增另一個 115 帳號（每個帳號各自登入）")
        self.filter = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.filter, width=20)
        entry.pack(side="left", padx=px(6))
        self.filter.trace_add("write", lambda *_: self._fill())
        bar.sep()
        self.btn_reload = bar.button("refresh", "重新整理", self.reload, "重新讀取登入狀態與容量")
        theme.line(self).pack(fill="x")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.cats = ttk.Treeview(body, show="tree", selectmode="browse")
        self.cats.column("#0", width=px(170), stretch=False)
        for key, icon, text in (("all", "globe", "全部"), ("cloud", "cloud", "115 網盤"), ("nas", "nas", "NAS 資料夾")):
            self.cats.insert("", "end", iid=key, text=" " + text, image=theme.icon(icon))
        self.cats.selection_set("all")
        self.cats.bind("<<TreeviewSelect>>", lambda e: self._fill())
        self.cats.pack(side="left", fill="y")
        theme.line(body, vertical=True).pack(side="left", fill="y")
        self.detail = ttk.Frame(body, padding=px(12), width=px(330))
        self.detail.pack(side="right", fill="y")
        self.detail.pack_propagate(False)
        theme.line(body, vertical=True).pack(side="right", fill="y")
        self.cards = CardList(body, on_select=lambda k: self._show_detail(), on_open=self.open_in_explore,
                              on_menu=lambda k, e: popup(self.cards.canvas, self.menu_items(), e.x_root, e.y_root))
        self.cards.pack(side="left", fill="both", expand=True)
        self.refresh()

    # ------------------------------------------------------------ 資料
    def on_show(self) -> None:
        self.refresh()

    def reload(self) -> None:
        self.info.clear()
        self.nas_info.clear()
        self.win.explore.quota_cache.clear()
        self.refresh()

    def refresh(self) -> None:
        ids = {a.id for a in self.config.accounts}
        for aid in list(self.info):
            if aid not in ids:
                del self.info[aid]
        for a in self.config.accounts:
            if a.id not in self.info:
                self._load(a.id)
        self._fill()

    def _load(self, aid: str) -> None:
        if aid in self._loading:
            return
        self._loading.add(aid)
        cloud, demo = self.win.cloud(aid), self.win.app.demo

        def work() -> None:
            info: dict = {"state": "off", "name": "尚未登入", "space": None}
            try:
                if demo:
                    info = {"state": "demo", "name": "示範模式（模擬 115，不會連線）", "space": cloud.space_info()}
                elif cloud.is_logged_in():
                    info = {"state": "on", "name": cloud.user_label(), "space": None}
                    info["space"] = cloud.space_info()
            except Exception as e:  # noqa: BLE001
                info = {"state": "error", "name": f"登入失效：{e}", "space": None}
            self.win.ui.call(self._loaded, aid, info)

        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, aid: str, info: dict) -> None:
        self._loading.discard(aid)
        if self.winfo_exists():
            self.info[aid] = info
            self._fill()

    def _load_nas(self, path: str) -> None:
        """NAS 的容量在背景讀取：NAS 離線時 Windows 要等很久才會回應，不能卡住畫面。"""
        key = "n:" + path
        if key in self._loading:
            return
        self._loading.add(key)

        def work() -> None:
            try:
                du = shutil.disk_usage(path)
                usage: tuple | None = (du.total, du.used, du.free)
            except OSError:
                usage = None
            self.win.ui.call(done, usage)

        def done(usage) -> None:
            self._loading.discard(key)
            if self.winfo_exists():
                self.nas_info[path] = usage
                self._fill()

        threading.Thread(target=work, daemon=True).start()

    def nas_folders(self) -> dict[str, list]:
        """所有同步用到的 NAS 資料夾 → 使用它的同步。"""
        out: dict[str, list] = {}
        for m in self.config.mappings:
            out.setdefault(m.local_path, []).append(m)
        return out

    def _fill(self) -> None:
        cat = (self.cats.selection() or ("all",))[0]
        text = self.filter.get().strip().casefold()
        p = theme.P
        self.cards.clear()
        if cat in ("all", "cloud"):
            for a in self.config.accounts:
                info = self.info.get(a.id, {"state": "loading", "name": "讀取中…", "space": None})
                if text and text not in a.label.casefold() and text not in info["name"].casefold():
                    continue
                card = self.cards.add("a:" + a.id)
                tk.Label(card, image=theme.icon("cloud" if info["state"] in ("on", "demo") else "cloud_off", 32),
                         background=p.bg).pack(side="left", padx=(px(2), px(12)))
                box = tk.Frame(card, background=p.bg)
                box.pack(side="left", fill="x", expand=True)
                head = tk.Frame(box, background=p.bg)
                head.pack(fill="x")
                _lbl(head, a.label, "UIBold").pack(side="left")
                _lbl(head, "115 網盤　" + info["name"], fg=p.danger if info["state"] == "error" else p.text2).pack(
                    side="left", padx=(px(10), 0))
                row = tk.Frame(box, background=p.bg)
                row.pack(fill="x", pady=(px(5), 0))
                if info["state"] in ("off", "error"):
                    tool_button(row, "connect", "連線", lambda i=a.id: self.login(i),
                                hint="用手機 115 App 掃碼登入").pack(side="left")
                elif info["space"]:
                    total, used = info["space"]
                    _lbl(row, f"容量　{human_size(total)}　　可用　{human_size(max(0, total - used))}").pack(side="left")
                    _quota_bar(row, used, total).pack(side="left", padx=(px(12), 0))
                else:
                    _lbl(row, "已連線" if info["state"] != "loading" else "", fg=p.text2).pack(side="left")
        if cat in ("all", "nas"):
            for path, ms in self.nas_folders().items():
                if text and text not in path.casefold():
                    continue
                card = self.cards.add("n:" + path)
                tk.Label(card, image=theme.icon("nas", 32), background=p.bg).pack(side="left", padx=(px(2), px(12)))
                box = tk.Frame(card, background=p.bg)
                box.pack(side="left", fill="x", expand=True)
                head = tk.Frame(box, background=p.bg)
                head.pack(fill="x")
                _lbl(head, path, "UIBold").pack(side="left")
                _lbl(head, f"NAS 資料夾　{len(ms)} 組同步使用", fg=p.text2).pack(side="left", padx=(px(10), 0))
                row = tk.Frame(box, background=p.bg)
                row.pack(fill="x", pady=(px(5), 0))
                if path not in self.nas_info:
                    self._load_nas(path)
                    _lbl(row, "讀取中…", fg=p.text2).pack(side="left")
                elif self.nas_info[path] is None:
                    _lbl(row, "目前無法讀取（NAS 離線或路徑不存在）", fg=p.danger).pack(side="left")
                else:
                    total, used, free = self.nas_info[path]
                    _lbl(row, f"容量　{human_size(total)}　　可用　{human_size(free)}").pack(side="left")
                    _quota_bar(row, used, total).pack(side="left", padx=(px(12), 0))
        self.cards.finish()
        self._show_detail()

    # ------------------------------------------------------------ 選取與詳細資料
    def selected_id(self) -> str:
        """選取的 115 帳號（沒有選取帳號時是第一個帳號）。"""
        key = self.cards.selected or ""
        if key.startswith("a:") and self.config.get_account(key[2:]):
            return key[2:]
        return self.config.accounts[0].id

    def select_account(self, aid: str) -> None:
        self.cards.select("a:" + aid)

    def _show_detail(self) -> None:
        d = self.detail
        for w in d.winfo_children():
            w.destroy()
        key = self.cards.selected or ""
        if key.startswith("a:"):
            a = self.config.get_account(key[2:])
            if a is None:
                return
            info = self.info.get(a.id, {"state": "loading", "name": "讀取中…", "space": None})
            ttk.Label(d, text=a.label, style="Big.TLabel", image=theme.icon("cloud", 24), compound="left").pack(anchor="w")
            rows = [("服務", "115 網盤"), ("狀態", info["name"])]
            if info["space"]:
                total, used = info["space"]
                rows += [("容量", human_size(total)), ("已使用", human_size(used)),
                         ("可用", human_size(max(0, total - used)))]
            n = sum(m.account_id == a.id for m in self.config.mappings)
            rows.append(("同步", f"{n} 組同步使用這個帳號"))
            _detail_rows(d, rows).pack(fill="x", pady=(px(8), px(10)))
            off = info["state"] in ("off", "error")
            for text, cmd, accent in (("掃碼登入…", lambda: self.login(a.id), off),
                                      ("在「瀏覽」頁開啟", lambda: self.open_in_explore(key), False),
                                      ("115 連線測試…", lambda: self.win.check_115(a.id), False),
                                      ("重新命名…", lambda: self.rename(a.id), False),
                                      ("登出", lambda: self.logout(a.id), False),
                                      ("移除帳號", lambda: self.remove(a.id), False)):
                ttk.Button(d, text=text, command=cmd, style="Accent.TButton" if accent else "TButton").pack(
                    fill="x", pady=(0, px(6)))
        elif key.startswith("n:"):
            path = key[2:]
            ms = self.nas_folders().get(path, [])
            ttk.Label(d, text="NAS 資料夾", style="Big.TLabel", image=theme.icon("nas", 24), compound="left").pack(anchor="w")
            _detail_rows(d, [("路徑", path), ("同步", "\n".join(m.title for m in ms) or "—")]).pack(
                fill="x", pady=(px(8), px(10)))
            ttk.Button(d, text="在「瀏覽」頁開啟", command=lambda: self.open_in_explore(key)).pack(fill="x", pady=(0, px(6)))
            ttk.Button(d, text="在檔案總管開啟", command=lambda: open_local(path)).pack(fill="x")
        else:
            ttk.Label(d, text="選取一個帳號查看詳細資料。", style="Secondary.TLabel").pack(anchor="w")

    def menu_items(self) -> list:
        key = self.cards.selected or ""
        if key.startswith("a:"):
            aid = key[2:]
            return [{"label": "在「瀏覽」頁開啟", "icon": "explore", "command": lambda: self.open_in_explore(key)},
                    {"label": "掃碼登入…", "icon": "qr", "command": lambda: self.login(aid)},
                    {"label": "115 連線測試…", "icon": "connect", "command": lambda: self.win.check_115(aid)},
                    None,
                    {"label": "重新命名…", "icon": "rename", "command": lambda: self.rename(aid)},
                    {"label": "登出", "icon": "logout", "command": lambda: self.logout(aid)},
                    {"label": "移除帳號", "icon": "delete", "enabled": aid != "default",
                     "command": lambda: self.remove(aid)}]
        if key.startswith("n:"):
            return [{"label": "在「瀏覽」頁開啟", "icon": "explore", "command": lambda: self.open_in_explore(key)},
                    {"label": "在檔案總管開啟", "icon": "open", "command": lambda: open_local(key[2:])}]
        return [{"label": "新增帳號…", "icon": "user", "command": self.add_account}]

    # ------------------------------------------------------------ 動作
    def add_account(self) -> None:
        label = simpledialog.askstring("新增 115 帳號", "帳號名稱（自訂，例如「家人帳號」）：", parent=self.win.root)
        if not label or not label.strip():
            return
        a = self.config.add_account(label.strip())
        self.engine.clouds[a.id] = make_client(self.config, a.id, self.win.app.demo)
        self.win.accounts_changed()
        self.select_account(a.id)

    def rename(self, aid: str) -> None:
        a = self.config.get_account(aid)
        if a is None:
            return
        label = simpledialog.askstring("重新命名帳號", "帳號名稱：", initialvalue=a.label, parent=self.win.root)
        if label and label.strip():
            a.label = label.strip()
            self.config.save()
            self.win.accounts_changed()

    def login(self, aid: str) -> None:
        if self.win.app.demo:
            messagebox.showinfo("示範模式", "示範模式使用模擬的 115，不需要登入。", parent=self.win.root)
            return
        if dialogs.QrLoginDialog(self.win.root, self.win.cloud(aid), self.win.ui).modal():
            messagebox.showinfo("登入成功", "已登入 115。建議按「115 連線測試」確認上傳、秒傳、下載都正常。",
                                parent=self.win.root)
        self.info.pop(aid, None)
        self.win.accounts_changed()

    def logout(self, aid: str) -> None:
        if self.win.app.demo:
            messagebox.showinfo("示範模式", "示範模式沒有登入狀態。", parent=self.win.root)
            return
        if messagebox.askyesno("登出", "確定要登出 115？（即時同步會停止；本機保存的登入資訊會刪除）\n"
                                     "也可以在 115 App 的「登入裝置管理」移除此裝置。", parent=self.win.root):
            if self.win.rt.running:
                self.win.rt.stop()
            self.win.cloud(aid).logout()
            self.info.pop(aid, None)
            self.win.accounts_changed()

    def remove(self, aid: str) -> None:
        a = self.config.get_account(aid)
        if a is None:
            return
        if aid == "default":
            messagebox.showinfo("無法移除", "預設帳號不能移除", parent=self.win.root)
            return
        if not messagebox.askyesno("移除帳號", f"移除帳號「{a.label}」？（本機保存的登入資訊也會刪除）", parent=self.win.root):
            return
        try:
            self.engine.remove_account(aid)
        except ValueError as e:
            messagebox.showerror("無法移除", str(e), parent=self.win.root)
            return
        self.info.pop(aid, None)
        self.win.accounts_changed()

    def open_in_explore(self, key: str | None = None) -> None:
        key = key or self.cards.selected or ""
        ex = self.win.explore
        if key.startswith("a:"):
            src = ex.cloud_source(key[2:])
            if src is not None:
                self.win.show_page("explore")
                ex.show_location(src, place=src.root)
        elif key.startswith("n:"):
            self.win.show_page("explore")
            ex.show_location(ex.local, text=key[2:])


# =========================================================================== 搜尋
SEARCH_DATES = [("不限", 0), ("今天", 1), ("7 天內", 7), ("30 天內", 30), ("一年內", 365)]
SEARCH_TYPES = [("全部", ()), ("資料夾", ("dir",)), ("圖片", ("file_image",)), ("影片", ("file_video",)),
                ("音樂", ("file_audio",)), ("文件", ("file_doc", "file_pdf")), ("壓縮檔", ("file_archive",)),
                ("遊戲存檔", ("file_save",)), ("其他", ("file", "file_exe"))]
SEARCH_SIZES = [("不限", 0, 0), ("小於 1 MB", 0, 1 << 20), ("1 MB ～ 100 MB", 1 << 20, 100 << 20),
                ("100 MB ～ 1 GB", 100 << 20, 1 << 30), ("大於 1 GB", 1 << 30, 0)]
TYPE_NAMES = {"dir": "資料夾", "file_image": "圖片", "file_video": "影片", "file_audio": "音樂", "file_doc": "文件",
              "file_pdf": "PDF", "file_archive": "壓縮檔", "file_save": "遊戲存檔", "file_exe": "程式", "file": "檔案"}
ALL_PLACES, NAS_INDEX = "所有位置", "NAS（已比對過的檔案）"


class SearchPage(ttk.Frame):
    """依檔名搜尋 115（所有帳號）與 NAS（用比對時建立的索引，不必重新掃描）。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win = win
        self.results: list[dict] = []
        self.shown: dict[str, dict] = {}
        self._seq = 0
        top = ttk.Frame(self, padding=(px(8), px(8), px(8), px(4)))
        top.pack(fill="x")
        self.query = tk.StringVar()
        self.query_box = ttk.Combobox(top, textvariable=self.query, values=list(win.config.settings.ui.get("searches", [])))
        self.query_box.pack(side="left", fill="x", expand=True)
        self.query_box.bind("<Return>", lambda e: self.search())
        self.btn_search = ttk.Button(top, text="搜尋", image=theme.icon_pair("search"), compound="left",
                                     style="Accent.TButton", command=self.search)
        self.btn_search.pack(side="left", padx=(px(6), 0))
        row = ttk.Frame(self, padding=(px(8), 0, px(8), px(6)))
        row.pack(fill="x")
        ttk.Label(row, text="位置").pack(side="left")
        self.target = ttk.Combobox(row, state="readonly", width=24)
        self.target.pack(side="left", padx=(px(6), px(14)))
        self.filters: dict[str, ttk.Combobox] = {}
        for key, label, values in (("date", "日期", SEARCH_DATES), ("type", "類型", SEARCH_TYPES), ("size", "大小", SEARCH_SIZES)):
            ttk.Label(row, text=label).pack(side="left")
            cb = ttk.Combobox(row, state="readonly", width=14, values=[v[0] for v in values])
            cb.set(values[0][0])
            cb.pack(side="left", padx=(px(6), px(14)))
            cb.bind("<<ComboboxSelected>>", lambda e: self._apply())
            self.filters[key] = cb
        theme.line(self).pack(fill="x")
        bottom = ttk.Frame(self, style="Chrome.TFrame", padding=(px(8), px(3)))
        bottom.pack(side="bottom", fill="x")
        self.status = ttk.Label(bottom, text="輸入檔名的一部分後按「搜尋」。", style="Chrome.TLabel")
        self.status.pack(side="left")
        self.btn_view = tool_button(bottom, "folder", "檢視資料夾", self.view_folder, style="Chrome.Tool.TButton",
                                    hint="在「瀏覽」頁開啟這個檔案所在的資料夾")
        self.btn_view.pack(side="right")
        theme.line(self).pack(side="bottom", fill="x")
        self.tree = make_tree(self, [("size", "大小", 90), ("date", "日期", 130), ("type", "類型", 80),
                                     ("where", "位置", 130), ("folder", "資料夾", 340)],
                              first=("名稱", 340), selectmode="browse", sortable=True)
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", lambda e: self.view_folder())
        self.tree.bind("<Return>", lambda e: self.view_folder())
        self.tree.bind("<Button-3>", self._menu)
        self.refresh_accounts()

    def on_show(self) -> None:
        self.query_box.focus_set()

    def refresh_accounts(self) -> None:
        values = [ALL_PLACES] + [a.label for a in self.win.config.accounts] + [NAS_INDEX]
        cur = self.target.get()
        self.target.configure(values=values)
        self.target.set(cur if cur in values else ALL_PLACES)

    # ------------------------------------------------------------ 搜尋
    def search(self) -> None:
        q = self.query.get().strip()
        if not q:
            return
        ui = self.win.config.settings.ui
        hist = [q] + [x for x in ui.get("searches", []) if x != q]
        ui["searches"] = hist[:15]
        self.query_box.configure(values=ui["searches"])
        self.win.save_ui()
        target = self.target.get()
        accounts = [a for a in self.win.config.accounts if target in (ALL_PLACES, a.label)]
        want_nas = target in (ALL_PLACES, NAS_INDEX)
        self._seq += 1
        seq = self._seq
        self.status.configure(text="搜尋中…")
        self.btn_search.state(["disabled"])
        demo = self.win.app.demo

        def work() -> None:
            results: list[dict] = []
            errors: list[str] = []
            if want_nas:
                for r in self.win.db.search_local(q, 1000):
                    m = self.win.config.get_mapping(r.mapping_id)
                    if m is None:
                        continue
                    path = local_join(m.local_path, r.rel_path)
                    results.append({"name": os.path.basename(path.replace("\\", "/")), "is_dir": False, "size": r.size,
                                    "mtime": r.mtime_ns / 1e9, "where": "NAS", "account_id": None,
                                    "folder": local_join(m.local_path, rel_parent(r.rel_path)), "path": path})
            for a in accounts:
                c = self.win.cloud(a.id)
                try:
                    if not demo and not c.is_logged_in():
                        if target != ALL_PLACES:
                            errors.append(f"{a.label}：尚未登入")
                        continue
                    for e in c.search(q, c.root_cid, 500):
                        results.append({"name": e.name, "is_dir": e.is_dir, "size": e.size, "mtime": e.mtime,
                                        "where": a.label, "account_id": a.id, "folder": "", "pid": e.pid, "fid": e.fid})
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{a.label}：{e}")
            self.win.ui.call(self._done, seq, results, errors)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, seq: int, results: list[dict], errors: list[str]) -> None:
        if seq != self._seq or not self.winfo_exists():
            return
        self.btn_search.state(["!disabled"])
        self.results = results
        self._apply()
        if errors:
            self.status.configure(text=self.status.cget("text") + "　⚠ " + "；".join(errors))

    def _kind(self, r: dict) -> str:
        return "dir" if r["is_dir"] else icons.file_icon_name(r["name"])

    def _apply(self) -> None:
        """套用日期／類型／大小篩選（不必重新搜尋）。"""
        days = dict(SEARCH_DATES)[self.filters["date"].get()]
        kinds = dict(SEARCH_TYPES)[self.filters["type"].get()]
        lo, hi = next((a, b) for label, a, b in SEARCH_SIZES if label == self.filters["size"].get())
        since = time.time() - days * 86400 if days else 0
        self.tree.delete(*self.tree.get_children())
        self.shown = {}
        keys: dict = {}
        for i, r in enumerate(self.results):
            kind = self._kind(r)
            if (kinds and kind not in kinds) or (since and (r["mtime"] or 0) < since):
                continue
            if (lo or hi) and (r["is_dir"] or r["size"] < lo or (hi and r["size"] >= hi)):
                continue
            iid = str(i)
            self.shown[iid] = r
            self.tree.insert("", "end", iid=iid, text=" " + r["name"],
                             image=theme.icon("folder" if r["is_dir"] else kind),
                             values=("" if r["is_dir"] else human_size(r["size"]), fmt_date(r["mtime"]),
                                     TYPE_NAMES.get(kind, "檔案"), r["where"], r["folder"] or "檢視資料夾"))
            keys[(iid, "size")] = (0, float(r["size"]), "")
            keys[(iid, "date")] = (0, float(r["mtime"] or 0), "")
        self.tree.sort_keys = keys  # type: ignore[attr-defined]
        total = len(self.results)
        self.status.configure(text=f"{len(self.shown)} 個結果" + (f"（共 {total} 個，其餘被篩選條件隱藏）"
                                                               if total != len(self.shown) else ""))

    # ------------------------------------------------------------ 動作
    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.shown.get(sel[0]) if sel else None

    def view_folder(self) -> None:
        r = self._selected()
        if r is None:
            return
        ex = self.win.explore
        if r["account_id"] is None:
            self.win.show_page("explore")
            ex.show_location(ex.local, place=r["folder"], select=r["name"])
            return
        src = ex.cloud_source(r["account_id"])
        if src is None:
            return
        self.status.configure(text="讀取資料夾位置…")

        def work() -> None:
            try:
                place, err = src.place_of(r["pid"]), None
            except Exception as e:  # noqa: BLE001
                place, err = None, e
            self.win.ui.call(show, place, err)

        def show(place, err) -> None:
            if err is not None:
                messagebox.showerror("檢視資料夾", f"無法讀取資料夾位置：{err}", parent=self.win.root)
                return
            r["folder"] = src.path_text(place)
            self.win.show_page("explore")
            ex.show_location(src, place=place, select=r["name"])

        threading.Thread(target=work, daemon=True).start()

    def open_item(self) -> None:
        r = self._selected()
        if r is None:
            return
        if r["account_id"] is None:
            open_file(r["path"])
        else:
            c = self.win.cloud(r["account_id"])
            open_in_115_browser(c.web_url(r["fid"] if r["is_dir"] else r["pid"]), self.win.config.settings.browser_path)

    def copy_name(self) -> None:
        r = self._selected()
        if r is not None:
            self.clipboard_clear()
            self.clipboard_append(r.get("path") or r["name"])

    def menu_items(self) -> list:
        r = self._selected()
        has = r is not None
        local = has and r["account_id"] is None
        return [{"label": "檢視資料夾", "icon": "folder", "enabled": has, "command": self.view_folder},
                {"label": "開啟檔案" if local else "在 115 瀏覽器開啟", "icon": "open" if local else "globe",
                 "enabled": has, "command": self.open_item},
                None,
                {"label": "複製路徑" if local else "複製名稱", "icon": "copy", "enabled": has, "command": self.copy_name}]

    def _menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        popup(self.tree, self.menu_items(), event.x_root, event.y_root)


# =========================================================================== 同步
def mapping_filter_text(m) -> str:
    parts = []
    if m.include_patterns:
        parts.append("只含 " + " ".join(m.include_patterns))
    if m.exclude_patterns:
        parts.append("排除 " + " ".join(m.exclude_patterns))
    if m.min_size or m.max_size:
        parts.append(f"大小 {human_size(m.min_size)} ～ {human_size(m.max_size) if m.max_size else '不限'}")
    if m.skip_hidden:
        parts.append("略過隱藏檔")
    return "；".join(parts) or "全部檔案"


def mapping_state(m) -> tuple[str, str]:
    """(文字, 圖示)"""
    if not m.confirmed:
        return "待核對（尚未由本人確認路徑，不會同步）", "warning"
    if not m.enabled:
        return "已核對・停用中", "paused"
    return "已核對", "ok"


class SyncPage(ttk.Frame):
    """已儲存的同步（NAS 資料夾 ⇄ 115 資料夾）。只有本人核對過路徑的同步才會比對與傳輸。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.config = win, win.config
        self.plans: dict[str, object] = {}  # 同步 ID → 最近一次比較結果
        bar = Bar(self)
        bar.pack(fill="x")
        self.btn_new = bar.button("plus", "建立新的同步", self.add, "選擇 NAS 資料夾與 115 資料夾")
        self.btn_suggest = bar.button("folder_link", "自動建議…", self.suggest, "把 NAS 根目錄下的每個資料夾配對到 115")
        bar.sep()
        self.btn_compare = bar.button("compare", "全部比較", self.compare_all, "比較所有已核對的同步（不傳輸）")
        self.btn_sync = bar.button("start", "全部同步", self.sync_all, "執行比較結果（上傳先嘗試秒傳）")
        bar.sep()
        self.rt_var = tk.BooleanVar(value=False)
        self.rt_toggle = Toggle(bar, self.rt_var, "dot_off", "即時同步", win.toggle_realtime,
                                hint="像 OneDrive：NAS 或 115 有變動時自動同步", icon_on="dot_on")
        self.rt_toggle.pack(side="left")
        self.btn_export = bar.button("export", "匯出", self.export_syncs, "把同步設定存成檔案", side="right")
        self.btn_import = bar.button("import", "匯入", self.import_syncs, "從檔案加入同步（加入後仍需核對）", side="right")
        theme.line(self).pack(fill="x")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.detail = ttk.Frame(body, padding=px(12), width=px(350))
        self.detail.pack(side="right", fill="y")
        self.detail.pack_propagate(False)
        theme.line(body, vertical=True).pack(side="right", fill="y")
        self.cards = CardList(body, on_select=lambda k: self._show_detail(), on_open=lambda k: self.open(),
                              on_menu=lambda k, e: popup(self.cards.canvas, self.menu_items(), e.x_root, e.y_root))
        self.cards.pack(side="left", fill="both", expand=True)
        self._sig = None

    # ------------------------------------------------------------ 清單
    def selected(self):
        return self.config.get_mapping(self.cards.selected or "")

    def select(self, mapping_id: str) -> None:
        self.cards.select(mapping_id)

    def _plan_text(self, m) -> str:
        plan = self.plans.get(m.id)
        if plan is None:
            return ""
        s = plan.summary()
        parts = []
        if s["uploads"]:
            parts.append(f"上傳 {s['uploads']}")
        if s["downloads"]:
            parts.append(f"下載 {s['downloads']}")
        if s["deletes"] + s["local_deletes"]:
            parts.append(f"刪除 {s['deletes'] + s['local_deletes']}")
        if s["reviews"]:
            parts.append(f"待核對 {s['reviews']}")
        return "比較結果：" + ("、".join(parts) if parts else "兩邊已一致") + f"（相同 {s['identical']} 個）"

    def refresh(self) -> None:
        ms = self.config.mappings
        for mid in list(self.plans):
            m = self.config.get_mapping(mid)
            if m is None or not m.active:
                del self.plans[mid]
        accounts = {a.id: a.label for a in self.config.accounts}
        sig = tuple((m.id, m.title, m.local_path, m.cloud_path, m.direction, m.confirmed, m.enabled, m.savedata,
                     accounts.get(m.account_id), self._plan_text(m)) for m in ms)
        if sig == self._sig:
            self._show_detail()
            return
        self._sig = sig
        p = theme.P
        self.cards.clear()
        for m in ms:
            card = self.cards.add(m.id)
            state, state_icon = mapping_state(m)
            head = tk.Frame(card, background=p.bg)
            head.pack(fill="x")
            _lbl(head, m.title, "UIBold", fg=p.text if m.enabled else p.text3).pack(side="left")
            if m.savedata:
                _lbl(head, " 遊戲存檔", fg=p.text2, image=theme.icon("gamepad"), compound="left").pack(
                    side="left", padx=(px(10), 0))
            _lbl(head, " " + state.split("（")[0], fg=p.warn if not m.confirmed else p.text2,
                 image=theme.icon(state_icon), compound="left").pack(side="right")
            for icon, text in (("computer", m.local_path),
                               ("cloud", f"{accounts.get(m.account_id, m.account_id)}　{m.cloud_path}")):
                _lbl(card, " " + text, fg=p.text if m.enabled else p.text3, image=theme.icon(icon),
                     compound="left").pack(fill="x", pady=(px(3), 0))
            row = tk.Frame(card, background=p.bg)
            row.pack(fill="x", pady=(px(5), 0))
            tool_button(row, DIRECTION_ICONS.get(m.direction, "sync"), "開啟", lambda i=m.id: self.open(i),
                        hint="開啟同步視窗：比較兩邊、查看每個檔案要做的動作").pack(side="left")
            _lbl(row, DIRECTION_LABELS.get(m.direction, m.direction), fg=p.text2).pack(side="left", padx=(px(10), 0))
            plan = self._plan_text(m)
            if plan:
                _lbl(row, plan, fg=p.text2).pack(side="right")
        if not ms:
            hint = tk.Frame(self.cards.inner, background=p.bg)
            hint.pack(fill="x", padx=px(20), pady=px(24))
            self.cards.cards["__hint__"] = hint
            _lbl(hint, "還沒有同步。按「建立新的同步」選擇 NAS 資料夾與 115 資料夾，\n"
                       "或按「自動建議…」把 NAS 根目錄下的每個資料夾配對到 115。", fg=p.text2, justify="left").pack(anchor="w")
            self.cards.order = []
            self.cards.selected = None
            self.cards._paint()
            del self.cards.cards["__hint__"]
            self._hint = hint
        else:
            self.cards.finish()
        self._show_detail()

    def _show_detail(self) -> None:
        d = self.detail
        for w in d.winfo_children():
            w.destroy()
        m = self.selected()
        if m is None:
            ttk.Label(d, text="選取一組同步查看設定。", style="Secondary.TLabel").pack(anchor="w")
            return
        acct = self.config.get_account(m.account_id)
        state, _icon = mapping_state(m)
        ttk.Label(d, text="設定", style="Bold.TLabel").pack(anchor="w")
        rows = [("名稱", m.title), ("類型", DIRECTION_LABELS.get(m.direction, m.direction)),
                ("帳號", acct.label if acct else m.account_id), ("篩選", mapping_filter_text(m)),
                ("內容比對", "是（MD5／SHA-1；上傳先嘗試秒傳）"),
                ("刪除處理", DELETE_LABELS.get(m.delete_mode, m.delete_mode)),
                ("狀態", state + (f"\n核對時間 {m.confirmed_at}" if m.confirmed and m.confirmed_at else ""))]
        if m.savedata:
            rows.insert(2, ("內容", "遊戲存檔（每個子資料夾為一個遊戲）"))
        _detail_rows(d, rows, wrap=230).pack(fill="x", pady=(px(4), 0))
        row = ttk.Frame(d)
        row.pack(fill="x", pady=(px(4), px(12)))
        ttk.Button(row, text="編輯…", command=lambda: self.edit(m)).pack(side="right")
        if not m.confirmed:
            ttk.Button(row, text="核對確認…", style="Accent.TButton", command=lambda: self.confirm(m)).pack(
                side="right", padx=(0, px(6)))

        theme.line(d).pack(fill="x")
        ttk.Label(d, text="排程", style="Bold.TLabel").pack(anchor="w", pady=(px(10), 0))
        scheds = [s for s in self.config.settings.schedules if not s.get("mapping_ids") or m.id in s["mapping_ids"]]
        if scheds:
            _detail_rows(d, [(s.get("name") or ("同步" if s.get("action") == "sync" else "只比對"),
                              describe_when(s) + ("" if s.get("enabled", True) else "（停用）")) for s in scheds[:4]],
                         wrap=230).pack(fill="x", pady=(px(4), 0))
        else:
            ttk.Label(d, text="沒有排程", style="Secondary.TLabel").pack(anchor="w", pady=(px(4), 0))
        row = ttk.Frame(d)
        row.pack(fill="x", pady=(px(4), px(12)))
        ttk.Button(row, text="新增排程…", command=lambda: self.win.scheduler_page.add(mapping_ids=[m.id])).pack(side="right")

        theme.line(d).pack(fill="x")
        grid = ttk.Frame(d)
        grid.pack(fill="x", pady=(px(10), 0))
        actions = [("開啟同步視窗", lambda: self.open(m.id)), ("比較", lambda: self.compare(m)),
                   ("啟用" if not m.enabled else "停用", lambda: self.toggle(m)),
                   ("取消核對", lambda: self.unconfirm(m)),
                   ("開啟 NAS 資料夾", lambda: open_local(m.local_path)),
                   ("在 115 瀏覽器開啟", lambda: self.open_cloud(m)),
                   ("在「瀏覽」頁並排", lambda: self.show_in_explore(m)), ("刪除同步", lambda: self.remove(m))]
        for i, (text, cmd) in enumerate(actions):
            ttk.Button(grid, text=text, command=cmd).grid(row=i // 2, column=i % 2, sticky="we", padx=(0, px(6)),
                                                          pady=(0, px(6)))
        for c in range(2):
            grid.columnconfigure(c, weight=1, uniform="act")

    def menu_items(self) -> list:
        m = self.selected()
        has = m is not None
        return [{"label": "開啟同步視窗", "icon": "sync", "enabled": has, "command": lambda: self.open(m.id)},
                {"label": "比較", "icon": "compare", "enabled": has, "command": lambda: self.compare(m)},
                None,
                {"label": "核對確認…", "icon": "shield", "enabled": has and not m.confirmed,
                 "command": lambda: self.confirm(m)},
                {"label": "取消核對", "enabled": has and m.confirmed, "command": lambda: self.unconfirm(m)},
                {"label": "啟用" if has and not m.enabled else "停用", "icon": "pause", "enabled": has,
                 "command": lambda: self.toggle(m)},
                {"label": "編輯…", "icon": "wrench", "enabled": has, "command": lambda: self.edit(m)},
                None,
                {"label": "開啟 NAS 資料夾", "icon": "computer", "enabled": has, "command": lambda: open_local(m.local_path)},
                {"label": "在 115 瀏覽器開啟", "icon": "globe", "enabled": has, "command": lambda: self.open_cloud(m)},
                {"label": "在「瀏覽」頁並排", "icon": "explore", "enabled": has, "command": lambda: self.show_in_explore(m)},
                None,
                {"label": "刪除同步", "icon": "delete", "enabled": has, "command": lambda: self.remove(m)}]

    # ------------------------------------------------------------ 比較結果
    def show_plans(self, plans) -> None:
        for p in plans:
            self.plans[p.mapping_id] = p
        self.refresh()

    def forget_plans(self, mapping_ids) -> None:
        for mid in mapping_ids:
            self.plans.pop(mid, None)
        self.refresh()

    def update_realtime(self, on: bool) -> None:
        if bool(self.rt_var.get()) != bool(on):
            self.rt_var.set(bool(on))

    def compare_all(self) -> None:
        self.win.scan()

    def sync_all(self) -> None:
        plans = [p for p in self.plans.values()]
        if not plans:
            messagebox.showinfo("尚未比較", "請先按「全部比較」，確認要做的動作後再同步。", parent=self.win.root)
            return
        self.win.execute(plans)

    def compare(self, m) -> None:
        if not m.confirmed:
            messagebox.showinfo("需要先核對", "這組同步還沒有由本人核對路徑，請先按「核對確認」。", parent=self.win.root)
            return
        if not m.enabled:
            messagebox.showinfo("已停用", "這組同步目前是停用狀態，請先啟用。", parent=self.win.root)
            return
        self.win.scan([m])

    # ------------------------------------------------------------ 新增、核對、編輯
    def add(self, local_path: str = "", account_id: str = "", cloud_path: str = "") -> None:
        r = dialogs.AddMappingDialog(self.win, None, local_path, account_id or self.win.accounts_page.selected_id(),
                                     cloud_path).modal()
        if not r:
            return
        m = self.config.add_mapping(r.pop("local_path"), r.pop("cloud_path"), **r)
        self.refresh()
        self.select(m.id)
        if messagebox.askyesno("核對路徑", "要現在核對這組同步的路徑嗎？（核對後才會同步）", parent=self.win.root):
            self.confirm(m)

    def suggest(self) -> None:
        aid = self.win.accounts_page.selected_id()
        cloud = self.win.cloud(aid)
        if not self.win.app.demo and not cloud.is_logged_in():
            messagebox.showwarning("尚未登入", "請先到「帳號」頁登入 115", parent=self.win.root)
            return
        items = dialogs.SuggestDialog(self.win.root, cloud, self.win.ui).modal()
        if not items:
            return
        existing = {(m.local_path, m.cloud_path) for m in self.config.mappings}
        n = 0
        for s in items:
            if (s.local_path, s.cloud_path) not in existing:
                self.config.add_mapping(s.local_path, s.cloud_path, account_id=aid)
                n += 1
        self.refresh()
        messagebox.showinfo("已加入", f"已加入 {n} 組「待核對」的同步，請逐一選取並按「核對確認」。", parent=self.win.root)

    def confirm(self, m, then=None) -> None:
        cloud = self.win.engine.cloud_for(m)
        r = dialogs.ConfirmMappingDialog(self.win.root, m, cloud, self.config.mappings, self.win.ui,
                                         self.config.settings.browser_path).modal()
        if not r:
            return

        def work() -> None:
            cid = r["cid"] if not r["create"] else cloud.ensure_path(m.cloud_path)
            self.config.confirm_mapping(m.id, cid)

        def done(_res) -> None:
            self.refresh()
            if then:
                then()

        self.win.run_bg("確認同步路徑", work, done)

    def edit(self, m, parent=None) -> None:
        r = dialogs.AddMappingDialog(self.win, m).modal()
        if not r:
            return
        moved = (r["local_path"] != m.local_path or r["cloud_path"] != m.cloud_path or r["account_id"] != m.account_id)
        for k, v in r.items():
            setattr(m, k, v)
        if moved:  # 路徑或帳號變了：必須重新核對
            m.confirmed, m.confirmed_at, m.cloud_cid = False, "", ""
            self.win.db.forget_cloud_dirs(m.id)
        self.plans.pop(m.id, None)
        self.config.save()
        self.refresh()
        if moved:
            messagebox.showinfo("需要重新核對", "路徑或帳號已變更，請重新「核對確認」。", parent=parent or self.win.root)

    def unconfirm(self, m) -> None:
        self.config.unconfirm_mapping(m.id)
        self.plans.pop(m.id, None)
        self.refresh()

    def toggle(self, m) -> None:
        m.enabled = not m.enabled
        self.config.save()
        self.refresh()

    def remove(self, m) -> None:
        if messagebox.askyesno("刪除同步", f"刪除同步「{m.title}」？（不會刪除任何檔案）", parent=self.win.root):
            w = self.win.sync_window
            if w is not None and w.winfo_exists() and w.mapping.id == m.id:
                w.destroy()
            self.config.remove_mapping(m.id)
            self.plans.pop(m.id, None)
            self.refresh()

    def open(self, mapping_id: str | None = None) -> None:
        m = self.config.get_mapping(mapping_id) if mapping_id else self.selected()
        if m is not None:
            self.win.open_sync_window(m)

    def open_cloud(self, m) -> None:
        c = self.win.engine.cloud_for(m)

        def work() -> str:
            cid = m.cloud_cid or c.resolve_path(m.cloud_path)
            if cid is None:
                raise ValueError(f"115 上找不到 {m.cloud_path}")
            return cid

        self.win.run_bg("開啟 115", work, lambda cid: self.win.open_cloud_path(m.account_id, cid))

    def show_in_explore(self, m) -> None:
        """在「瀏覽」頁左邊開 NAS 資料夾、右邊開 115 資料夾。"""
        ex = self.win.explore
        self.win.show_page("explore")
        ex.show_location(ex.local, text=m.local_path)
        src = ex.cloud_source(m.account_id)
        if src is not None:
            if ex.view == "one":
                ex.set_view("two")
            ex.show_location(src, text=m.cloud_path)

    # ------------------------------------------------------------ 匯入／匯出
    FIELDS = ("name", "local_path", "cloud_path", "direction", "delete_mode", "savedata", "include_patterns",
              "exclude_patterns", "min_size", "max_size", "skip_hidden")

    def export_syncs(self) -> None:
        if not self.config.mappings:
            messagebox.showinfo("匯出", "目前沒有同步可以匯出。", parent=self.win.root)
            return
        path = filedialog.asksaveasfilename(parent=self.win.root, title="匯出同步設定", defaultextension=".json",
                                            initialfile="NAS115Sync-同步設定.json", filetypes=[("同步設定", "*.json")])
        if not path:
            return
        accounts = {a.id: a.label for a in self.config.accounts}
        data = [dict({k: getattr(m, k) for k in self.FIELDS}, account=accounts.get(m.account_id, ""))
                for m in self.config.mappings]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"nas115sync_syncs": data}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("匯出失敗", str(e), parent=self.win.root)
            return
        self.win.set_status(f"已匯出 {len(data)} 組同步到 {path}")

    def import_syncs(self) -> None:
        path = filedialog.askopenfilename(parent=self.win.root, title="匯入同步設定",
                                          filetypes=[("同步設定", "*.json"), ("全部", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                items = json.load(f)["nas115sync_syncs"]
            if not isinstance(items, list):
                raise ValueError("格式不正確")
        except (OSError, ValueError, KeyError, TypeError) as e:
            messagebox.showerror("匯入失敗", f"這不是 NAS115Sync 匯出的同步設定：{e}", parent=self.win.root)
            return
        by_label = {a.label: a.id for a in self.config.accounts}
        existing = {(m.local_path, m.cloud_path, m.account_id) for m in self.config.mappings}
        n = 0
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("local_path"), str) or not isinstance(
                    it.get("cloud_path"), str) or not it["local_path"] or not it["cloud_path"]:
                continue
            aid = by_label.get(str(it.get("account", "")), "default")
            if (it["local_path"], it["cloud_path"], aid) in existing:
                continue
            opts = {k: it[k] for k in self.FIELDS[3:] if k in it}
            opts["name"] = str(it.get("name", ""))
            if opts.get("direction") not in DIRECTION_LABELS:
                opts.pop("direction", None)
            if opts.get("delete_mode") not in DELETE_LABELS:
                opts.pop("delete_mode", None)
            for k in ("include_patterns", "exclude_patterns"):
                opts[k] = [str(x) for x in opts.get(k, []) if isinstance(x, str)] if isinstance(opts.get(k), list) else []
            for k in ("min_size", "max_size"):
                opts[k] = int(opts[k]) if isinstance(opts.get(k), (int, float)) and opts[k] >= 0 else 0
            for k in ("savedata", "skip_hidden"):
                opts[k] = bool(opts.get(k))
            self.config.add_mapping(it["local_path"], it["cloud_path"], account_id=aid, **opts)
            n += 1
        self.refresh()
        messagebox.showinfo("匯入完成", f"已加入 {n} 組同步（狀態為「待核對」，要由本人核對路徑後才會同步）。",
                            parent=self.win.root)


DIRECTION_ICONS = {"upload": "to_right", "download": "to_left", "two_way": "compare"}


# =========================================================================== 排程
class SchedulerPage(ttk.Frame):
    """排程工作：在指定時間自動比較／同步（本程式要在執行中）。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.config = win, win.config
        bar = Bar(self)
        bar.pack(fill="x")
        self.btn_new = bar.button("plus", "新增工作", self.add, "新增排程工作")
        self.btn_edit = bar.button("wrench", "編輯…", self.edit)
        bar.sep()
        self.btn_run = bar.button("start", "立即執行", self.run_now, "現在就執行選取的排程工作")
        self.btn_toggle = bar.button("pause", "啟用／停用", self.toggle)
        self.btn_report = bar.button("log", "上次報告", self.show_report, "查看上次執行的結果")
        bar.sep()
        self.btn_delete = bar.button("delete", "刪除", self.delete)
        theme.line(self).pack(fill="x")
        self.tree = make_tree(self, [("status", "狀態", 90), ("action", "操作", 130), ("when", "排程", 170),
                                     ("next", "下次執行", 140), ("last", "上次執行", 150), ("result", "結果", 320)],
                              first=("名稱", 220), selectmode="browse")
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.bind("<Double-Button-1>", lambda e: self.edit())
        self.tree.bind("<Button-3>", self._menu)
        self.hint = ttk.Label(self, text="排程在本程式執行中才會動作；可在「選項 → 一般」勾選開機自動啟動。",
                              style="Chrome.Secondary.TLabel", padding=(px(8), px(3)))
        self.hint.pack(side="bottom", fill="x")

    def on_show(self) -> None:
        self.refresh()

    @property
    def schedules(self) -> list:
        return self.config.settings.schedules

    @staticmethod
    def title(s: dict) -> str:
        return s.get("name") or ("完整比對＋同步" if s.get("action") == "sync" else "只比對")

    def refresh(self) -> None:
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for s in self.schedules:
            on = s.get("enabled", True)
            nxt = next_run(s)
            scope = f"{len(s['mapping_ids'])} 組同步" if s.get("mapping_ids") else "所有同步"
            if s.get("last_run"):
                result = ("✔ " if s.get("last_ok", True) else "✘ ") + str(s.get("last_report", "")).split("\n")[0]
            else:
                result = ""
            self.tree.insert("", "end", iid=s["id"], text=" " + self.title(s),
                             image=theme.icon("calendar" if on else "paused"),
                             values=("啟用" if on else "停用",
                                     ("同步" if s.get("action") == "sync" else "只比對") + f"（{scope}）",
                                     describe_when(s), fmt_date(nxt) if nxt else "—",
                                     fmt_time(s.get("last_run")) or "尚未執行", result))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])

    def selected(self) -> dict | None:
        sel = self.tree.selection()
        return next((s for s in self.schedules if sel and s["id"] == sel[0]), None)

    def add(self, mapping_ids=None) -> None:
        r = dialogs.ScheduleDialog(self.win, None, mapping_ids).modal()
        if r:
            s = dialogs.schedule_from(r)
            self.schedules.append(s)
            self.config.save()
            self.win.refresh_all()
            if self.tree.exists(s["id"]):
                self.tree.selection_set(s["id"])

    def edit(self) -> None:
        s = self.selected()
        if s is None:
            return
        r = dialogs.ScheduleDialog(self.win, s).modal()
        if r:
            s.update(r)
            self.config.save()
            self.win.refresh_all()

    def delete(self) -> None:
        s = self.selected()
        if s is not None and messagebox.askyesno("刪除排程", f"刪除排程工作「{self.title(s)}」？", parent=self.win.root):
            self.schedules.remove(s)
            self.config.save()
            self.win.refresh_all()

    def toggle(self) -> None:
        s = self.selected()
        if s is not None:
            s["enabled"] = not s.get("enabled", True)
            self.config.save()
            self.win.refresh_all()

    def run_now(self) -> None:
        s = self.selected()
        if s is not None and self.win.ready_to_sync():
            self.win.run_bg("排程工作", lambda: self.win.scheduler.run_schedule(s), lambda _r: self.refresh(),
                            batch=("sync", "排程：" + self.title(s), "", ""), mapping_ids=list(s.get("mapping_ids") or [
                                m.id for m in self.config.active_mappings()]))

    def show_report(self) -> None:
        s = self.selected()
        if s is None:
            return
        if not s.get("last_run"):
            messagebox.showinfo("上次報告", "這個排程工作還沒有執行過。", parent=self.win.root)
            return
        messagebox.showinfo("上次報告", f"{self.title(s)}\n執行時間：{fmt_time(s.get('last_run'))}\n\n"
                                    f"{s.get('last_report', '')}", parent=self.win.root)

    def menu_items(self) -> list:
        has = self.selected() is not None
        return [{"label": "新增工作…", "icon": "plus", "command": self.add},
                {"label": "編輯…", "icon": "wrench", "enabled": has, "command": self.edit},
                None,
                {"label": "立即執行", "icon": "start", "enabled": has, "command": self.run_now},
                {"label": "啟用／停用", "icon": "pause", "enabled": has, "command": self.toggle},
                {"label": "上次報告", "icon": "log", "enabled": has, "command": self.show_report},
                None,
                {"label": "刪除", "icon": "delete", "enabled": has, "command": self.delete}]

    def _menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        popup(self.tree, self.menu_items(), event.x_root, event.y_root)


# =========================================================================== 待核對
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
    widget.tag_configure("title", font="UIBig", spacing3=px(4))
    widget.tag_configure("section", font="UIBold", spacing1=px(8), spacing3=px(2))
    widget.tag_configure("mono", font="UIMono")
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


class ReviewsPage(ttk.Frame):
    """需要本人判斷的項目：同名、相同 MD5／SHA-1、遊戲存檔、衝突與刪除。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.config, self.db = win, win.config, win.db
        self.items: dict[str, object] = {}
        bar = Bar(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="類型").pack(side="left", padx=(px(4), px(6)))
        self.kind_filter = ttk.Combobox(bar, state="readonly", width=28,
                                        values=["全部"] + [KIND_LABELS[k] for k in DECISIONS])
        self.kind_filter.set("全部")
        self.kind_filter.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.kind_filter.pack(side="left", padx=(0, px(8)))
        self.btn_refresh = bar.button("refresh", "重新整理", self.refresh)
        self.btn_all = bar.button("check_on", "全選此類型", self.select_all, "選取所有同類型的項目，一次處理")
        self.count = ttk.Label(bar, text="", style="Secondary.TLabel")
        self.count.pack(side="right", padx=px(6))
        theme.line(self).pack(fill="x")

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        self.tree = make_tree(left, [("kind", "類型", 150), ("sync", "同步", 120)], first=("路徑", 300))
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show())
        pane.add(left, weight=2)
        right = ttk.Frame(pane, padding=(px(8), px(6)))
        self.detail = scrolled_text(right, height=16, wrap="char", state="disabled")
        self.detail.frame.pack(fill="both", expand=True)
        self.proc_frame = ttk.Frame(right)
        ttk.Label(self.proc_frame, text="遊戲執行檔（選填，逗號分隔，執行中不上傳）").pack(anchor="w")
        self.proc_var = tk.StringVar()
        ttk.Entry(self.proc_frame, textvariable=self.proc_var).pack(fill="x", pady=(px(3), 0))
        self.decision_bar = ttk.Frame(right)
        self.decision_bar.pack(fill="x", pady=(px(8), 0))
        nav = ttk.Frame(right)
        nav.pack(fill="x", pady=(px(4), 0))
        self.btn_open_local = tool_button(nav, "computer", "開啟 NAS 位置", self.open_local)
        self.btn_open_local.pack(side="left")
        self.btn_open_cloud = tool_button(nav, "globe", "在 115 瀏覽器開啟", self.open_cloud)
        self.btn_open_cloud.pack(side="left", padx=(px(4), 0))
        pane.add(right, weight=3)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        label = self.kind_filter.get()
        kind = next((k for k, v in KIND_LABELS.items() if v == label), None)
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        for rv in self.db.list_reviews("pending", kind=kind):
            iid = str(rv.id)
            self.items[iid] = rv
            m = self.config.get_mapping(rv.mapping_id)
            self.tree.insert("", "end", iid=iid, text=" " + (rv.rel_path or "（整個同步）"),
                             image=theme.icon("question"),
                             values=(KIND_LABELS.get(rv.kind, rv.kind), m.title if m else ""))
        self.count.configure(text=f"{len(self.items)} 項待核對" if self.items else "沒有待核對的項目")
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        else:
            self._show()

    def select_all(self) -> None:
        sel = self.selected()
        kind = sel[0].kind if sel else None
        self.tree.selection_set([i for i, rv in self.items.items() if kind is None or rv.kind == kind])

    def select_path(self, mapping_id: str, rel_path: str) -> None:
        """（從同步視窗跳過來）選取指定檔案的待核對項目。"""
        key = norm_key(rel_path)
        for iid, rv in self.items.items():
            if rv.mapping_id == mapping_id and norm_key(rv.rel_path or "") == key:
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return

    def selected(self) -> list:
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def _show(self) -> None:
        for w in self.decision_bar.winfo_children():
            w.destroy()
        self.proc_frame.pack_forget()
        sel = self.selected()
        if not sel:
            set_text(self.detail, "請在左側選取一個待核對項目。" if self.items else
                     "沒有待核對的項目。\n\n比較或即時同步時，遇到同檔名、相同 MD5／SHA-1、遊戲存檔、兩邊都改過或檔案被刪除，"
                     "會列在這裡由本人決定。")
            return
        show_review_text(self.detail, review_text(sel[0]) + (f"\n\n（共選取 {len(sel)} 項）" if len(sel) > 1 else ""))
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
            self.proc_frame.pack(fill="x", pady=(px(8), 0), before=self.decision_bar)
        for i, (decision, label) in enumerate(opts):
            ttk.Button(self.decision_bar, text=label, style="Accent.TButton" if i == 0 and decision not in DANGEROUS
                       else "TButton", command=lambda d=decision, lb=label: self.decide(d, lb)).grid(
                row=i // 2, column=i % 2, sticky="we", padx=(0, px(6)), pady=(0, px(6)))
        for c in range(2):
            self.decision_bar.columnconfigure(c, weight=1, uniform="decision")

    def decide(self, decision: str, label: str) -> None:
        sel = self.selected()
        if not sel:
            return
        if decision in DANGEROUS and not messagebox.askyesno(
                "請確認", f"確定對 {len(sel)} 個項目執行「{label}」？\n（115 刪除的檔案會進入 115 回收站）",
                parent=self.win.root):
            return
        procs = [p.strip() for p in self.proc_var.get().split(",") if p.strip()]
        extra = {"process_names": procs} if procs else {}
        ids = [rv.id for rv in sel]
        engine = self.win.engine

        def work():
            ok, errors = 0, []
            for i, rid in enumerate(ids, 1):
                if engine.cancel_event.is_set():
                    raise Cancelled("已取消")
                try:
                    engine.apply_review(rid, decision, extra)
                    ok += 1
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    errors.append(f"#{rid}：{e}")
                self.win.progress_cb("處理核對項目", i, len(ids), str(rid))
            return ok, errors

        def done(res) -> None:
            ok, errors = res
            msg = f"完成 {ok} 項"
            if errors:
                msg += f"，失敗 {len(errors)} 項：\n" + "\n".join(errors[:15])
                messagebox.showwarning("處理結果", msg, parent=self.win.root)
            else:
                self.win.set_status(msg)

        self.win.run_bg(f"「{label}」", work, done)

    def open_local(self) -> None:
        sel = self.selected()
        if sel:
            d = sel[0].details
            path = (d.get("local") or {}).get("path") or d.get("local_dir") or d.get("local_root")
            if path:
                open_local(path)

    def open_cloud(self) -> None:
        sel = self.selected()
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
            self.win.open_cloud_path(m.account_id, cid)


# =========================================================================== 遊戲存檔
class SavedataPage(ttk.Frame):
    """像 Steam 雲存檔：遊戲寫完存檔、程序結束後自動上傳到 115，並刪除 115 上的舊版。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win, self.config, self.db = win, win.config, win.db
        self.items: dict[str, tuple] = {}
        bar = Bar(self, "遊戲存檔")
        bar.pack(fill="x")
        self.btn_approve = bar.button("ok_circle", "核可自動同步", lambda: self.action("approve"),
                                      "像 Steam 雲存檔一樣自動上傳新存檔")
        self.btn_sync = bar.button("upload", "立即同步", self.sync_now, "現在就把選取的存檔上傳到 115")
        self.btn_procs = bar.button("gamepad", "設定遊戲程序…", self.set_procs, "遊戲執行中不上傳，結束後才上傳")
        bar.sep()
        self.btn_normal = bar.button("file", "當一般檔案", lambda: self.action("normal"))
        self.btn_exclude = bar.button("skipped", "不同步", lambda: self.action("exclude"))
        bar.sep()
        self.btn_open = bar.button("computer", "開啟 NAS 位置", self.open_local)
        theme.line(self).pack(fill="x")
        self.tree = make_tree(self, [("sync", "同步", 180), ("state", "狀態", 200), ("proc", "遊戲程序", 180),
                                     ("last", "上次上傳", 150)], first=("存檔資料夾", 340))
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.bind("<Button-3>", self._menu)
        ttk.Label(self, text="比較或即時同步時偵測到的存檔資料夾會列在這裡；第一次要由本人核可，之後自動同步。",
                  style="Chrome.Secondary.TLabel", padding=(px(8), px(3))).pack(side="bottom", fill="x")

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        for g in self.db.list_groups():
            m = self.config.get_mapping(g.mapping_id)
            if not m:
                continue
            iid = f"{g.mapping_id}|{g.rel_key}"
            self.items[iid] = (m, g)
            self.tree.insert("", "end", iid=iid, text=" " + (g.rel_dir or "（同步的根目錄）"),
                             image=theme.icon("file_save" if g.status == "approved" else "folder"),
                             values=(m.title, GROUP_STATUS.get(g.status, g.status), ", ".join(g.process_names),
                                     fmt_time(g.last_upload)))
        self.tree.selection_set([i for i in sel if self.tree.exists(i)])

    def selected(self) -> list:
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def action(self, decision: str) -> None:
        groups = self.selected()
        if not groups:
            messagebox.showinfo("請選取", "請先選取存檔資料夾", parent=self.win.root)
            return

        def work() -> None:
            for m, g in groups:
                self.win.engine.change_savedata_group(m, g.rel_dir, decision)

        self.win.run_bg("變更存檔設定", work, lambda _: self.refresh())

    def set_procs(self) -> None:
        groups = self.selected()
        if not groups:
            messagebox.showinfo("請選取", "請先選取存檔資料夾", parent=self.win.root)
            return
        cur = ", ".join(groups[0][1].process_names)
        s = simpledialog.askstring("遊戲程序", "遊戲執行檔名稱（逗號分隔，例如 eldenring.exe）。\n"
                                             "這些程序執行中時不會上傳存檔，等遊戲結束才上傳：",
                                   initialvalue=cur, parent=self.win.root)
        if s is None:
            return
        for m, g in groups:
            self.db.upsert_group(m.id, g.rel_dir, process_names=[p.strip() for p in s.split(",") if p.strip()])
        self.refresh()

    def sync_now(self) -> None:
        groups = [(m, g) for m, g in self.selected() if g.status == "approved"]
        if not groups:
            messagebox.showinfo("無法同步", "請選取「自動同步」狀態的存檔資料夾", parent=self.win.root)
            return
        self.win.run_bg("同步存檔", lambda: [self.win.engine.sync_savedata_group(m, g.rel_dir) for m, g in groups],
                        lambda _: self.refresh(), batch=("upload", "同步存檔", "、".join(g.rel_dir for _m, g in groups), ""),
                        mapping_ids=[m.id for m, _g in groups])

    def open_local(self) -> None:
        for m, g in self.selected()[:1]:
            open_local(local_join(m.local_path, g.rel_dir))

    def menu_items(self) -> list:
        has = bool(self.selected())
        return [{"label": "核可自動同步", "icon": "ok_circle", "enabled": has, "command": lambda: self.action("approve")},
                {"label": "立即同步", "icon": "upload", "enabled": has, "command": self.sync_now},
                {"label": "設定遊戲程序…", "icon": "gamepad", "enabled": has, "command": self.set_procs},
                None,
                {"label": "當一般檔案", "icon": "file", "enabled": has, "command": lambda: self.action("normal")},
                {"label": "不同步", "icon": "skipped", "enabled": has, "command": lambda: self.action("exclude")},
                None,
                {"label": "開啟 NAS 位置", "icon": "computer", "enabled": has, "command": self.open_local}]

    def _menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        popup(self.tree, self.menu_items(), event.x_root, event.y_root)


# =========================================================================== 重複檔案
class DuplicatesPage(ttk.Frame):
    """依 SHA-1 找出 NAS 與 115 上的重複檔案（同一邊有 2 份以上）。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win = win
        self.items: dict[str, dict] = {}
        bar = Bar(self, "重複檔案")
        bar.pack(fill="x")
        self.btn_refresh = bar.button("refresh", "分析", self.refresh, "用最近一次比較建立的索引找出重複檔案")
        self.btn_rescan = bar.button("compare", "先完整比較再分析", self.rescan)
        bar.sep()
        self.btn_delete = bar.button("delete", "刪除選取的 115 副本", self.delete_selected, "會進入 115 回收站")
        self.btn_open = bar.button("open", "開啟位置", self.open_selected)
        theme.line(self).pack(fill="x")
        self.summary = ttk.Label(self, text="按「分析」依 SHA-1 找出內容相同的檔案。", style="Chrome.TLabel",
                                 padding=(px(8), px(3)))
        self.summary.pack(side="bottom", fill="x")
        self.tree = make_tree(self, [("where", "位置", 80), ("size", "大小", 100), ("info", "說明", 340)],
                              first=("SHA-1 ／ 路徑", 560))
        self.tree.frame.pack(fill="both", expand=True)

    def refresh(self) -> None:
        def done(groups) -> None:
            self.tree.delete(*self.tree.get_children())
            self.items.clear()
            for gi, g in enumerate(groups):
                parent = self.tree.insert("", "end", iid=f"g{gi}", text=" " + g["sha1"], open=False,
                                          image=theme.icon("duplicates"), values=(
                    "", human_size(g["size"]), f"NAS {g['nas_count']} 份、115 {g['cloud_count']} 份，"
                                               f"多佔 {human_size(g['wasted'])}"))
                for ii, it in enumerate(g["items"]):
                    iid = f"g{gi}i{ii}"
                    self.items[iid] = it
                    self.tree.insert(parent, "end", iid=iid, text=" " + it["path"],
                                     image=theme.icon("computer" if it["where"] == "NAS" else "cloud"),
                                     values=(it["where"], "", ""))
            total = sum(g["wasted"] for g in groups)
            self.summary.configure(text=f"共 {len(groups)} 組重複檔案，可省下約 {human_size(total)}。"
                                        "展開群組後可選取 115 上多餘的副本刪除（進 115 回收站）。")

        self.win.run_bg("分析重複檔案", lambda: manual.duplicate_report(self.win.engine), done)

    def rescan(self) -> None:
        if self.win.ready_to_sync():
            self.win.run_bg("完整比較", lambda: self.win.engine.analyze(progress=self.win.progress_cb),
                            lambda plans: (self.win.sync_page.show_plans(plans), self.refresh()))

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
                open_local(os.path.dirname(it["path"]) or it["path"])
                return
            if it and it["where"] == "115" and it.get("pid") and it.get("account_id"):
                self.win.open_cloud_path(it["account_id"], it["pid"])
                return


# =========================================================================== 紀錄
class LogPage(ttk.Frame):
    """執行紀錄與每一次上傳、下載、刪除的同步歷史。"""

    def __init__(self, parent, win):
        super().__init__(parent)
        self.win = win
        bar = Bar(self, "紀錄與同步歷史")
        bar.pack(fill="x")
        self.btn_refresh = bar.button("refresh", "重新整理", self.refresh_history)
        self.btn_clear = bar.button("broom", "清除畫面上的紀錄", self.clear_log)
        self.btn_folder = bar.button("folder", "開啟紀錄資料夾", self.open_folder)
        self.stats = ttk.Label(bar, text="", style="Secondary.TLabel")
        self.stats.pack(side="right", padx=px(6))
        theme.line(self).pack(fill="x")
        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)
        top = ttk.Frame(pane)
        self.text = scrolled_text(top, height=10, state="disabled", font="UIMono")
        self.text.frame.pack(fill="both", expand=True)
        self.text.tag_configure("warn", foreground=theme.P.warn)
        self.text.tag_configure("error", foreground=theme.P.danger)
        pane.add(top, weight=1)
        bottom = ttk.Frame(pane)
        self.tree = make_tree(bottom, [("action", "動作", 110), ("how", "方式", 90), ("size", "大小", 90),
                                       ("ok", "結果", 60), ("path", "路徑", 380), ("detail", "說明", 250)],
                              first=("時間", 170))
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.tag_configure("fail", foreground=theme.P.danger)
        pane.add(bottom, weight=1)

    def on_show(self) -> None:
        self.refresh_history()

    def append(self, line: str, level: int) -> None:
        t = self.text
        t.configure(state="normal")
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else ""
        t.insert("end", line + "\n", tag)
        if int(t.index("end-1c").split(".")[0]) > 3000:
            t.delete("1.0", "500.0")
        t.see("end")
        t.configure(state="disabled")

    def clear_log(self) -> None:
        set_text(self.text, "")

    def open_folder(self) -> None:
        from ..config import app_home

        open_local(str(app_home()))

    def refresh_history(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for h in self.win.db.recent_history(500):
            action = str(h["action"])
            how = ("秒傳" if h["instant"] else "實際上傳") if action.startswith("upload") else \
                ("從本機複製" if h["instant"] else "") if action.startswith("download") else ""
            ok = bool(h["ok"])
            self.tree.insert("", "end", text=" " + fmt_time(h["ts"]), image=theme.icon("ok" if ok else "error"),
                             values=(HISTORY_ACTIONS.get(action, action), how, human_size(h["size"]) if h["size"] else "",
                                     "成功" if ok else "失敗", h["rel_path"], h["detail"] or ""),
                             tags=() if ok else ("fail",))
        st = self.win.db.upload_stats()
        self.stats.configure(
            text=f"秒傳 {st.get('instant_n') or 0} 次（省下 {human_size(st.get('instant_bytes') or 0)}）・"
                 f"實際上傳 {st.get('real_n') or 0} 次（{human_size(st.get('real_bytes') or 0)}）・"
                 f"下載 {st.get('down_n') or 0} 次")
