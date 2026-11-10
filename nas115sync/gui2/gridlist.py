"""GridList：以畫布繪製的清單，只畫看得到的那幾列，所以幾十萬列也一樣流暢。

ttk.Treeview 的儲存格只能放文字；工作清單需要「進度條」、同步比對需要「中間一欄的動作圖示」，
因此這兩處用這個元件。支援：欄位標題與拖曳調整欄寬、圖示／文字／進度條三種儲存格、
一層的展開收合（工作 → 檔案）、單選／多選（Ctrl、Shift）、鍵盤上下移動、雙擊、右鍵選單、滑鼠滾輪。
"""
from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from . import theme
from .theme import px
from .util import AutoScrollbar, ellipsis


@dataclass
class Column:
    id: str
    title: str
    width: int               # 邏輯像素
    kind: str = "text"       # text | icontext | icon | progress
    anchor: str = "w"        # w | e | center
    stretch: bool = False    # 視窗變寬時由這一欄吸收多出來的寬度


class GridList(ttk.Frame):
    def __init__(self, parent, columns: list[Column], tree: bool = False, select: str = "extended",
                 on_open=None, on_menu=None, on_select=None, on_header=None, render=None, row_id=None):
        """render(row) -> {"cells": {欄位: 值}, "fg": 顏色, "bold": bool}；row_id(row) -> 可雜湊的識別值。
        沒有給 render／row_id 時，每一列本身就是這樣的 dict（另含 "id" 與可選的 "children"）。"""
        super().__init__(parent)
        self.columns = columns
        self.tree_mode, self.select_mode = tree, select
        self.on_open, self.on_menu, self.on_select, self.on_header = on_open, on_menu, on_select, on_header
        self._render = render or (lambda r: r)
        self._row_id = row_id or (lambda r: r["id"])
        self.rows: list = []
        self._flat: list = []           # 目前顯示的列：(row, 深度, 是否有子項目)
        self._open: set = set()
        self._sel: set = set()
        self._anchor = None
        self._cursor = None             # 鍵盤所在的列（識別值）
        self._top = 0                   # 捲動位置（像素）
        self._left = 0
        self._widths = [px(c.width) for c in columns]
        self._cache: dict = {}
        self._pending = False
        self._lazy = True
        self._drag = None
        self.sort: tuple[str, bool] | None = None
        self.row_h = theme.ROW
        self.head_h = theme.ROW + px(2)
        self.font = theme.font("UI")
        self.bold = theme.font("UIBold")

        self.canvas = tk.Canvas(self, background=theme.P.bg, highlightthickness=0, borderwidth=0, takefocus=1)
        self.vbar = AutoScrollbar(self, orient="vertical", command=self.yview)
        self.hbar = AutoScrollbar(self, orient="horizontal", command=self.xview)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        c = self.canvas
        c.bind("<Configure>", lambda e: self.refresh())
        c.bind("<Button-1>", self._click)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        c.bind("<Double-Button-1>", self._double)
        c.bind("<Button-3>", self._context)
        c.bind("<Motion>", self._motion)
        c.bind("<Key>", self._key)
        c.bind("<FocusIn>", lambda e: self.refresh())
        c.bind("<FocusOut>", lambda e: self.refresh())
        if sys.platform.startswith("linux"):
            c.bind("<Button-4>", lambda e: self.yview("scroll", -3, "units"))
            c.bind("<Button-5>", lambda e: self.yview("scroll", 3, "units"))
        else:
            c.bind("<MouseWheel>", lambda e: self.yview("scroll", -3 if e.delta > 0 else 3, "units"))

    # ------------------------------------------------------------ 資料
    def set_rows(self, rows: list, keep_scroll: bool = True) -> None:
        self.rows = rows
        self._reflow()
        if not keep_scroll:
            self._top = 0
        self.refresh()

    def _reflow(self) -> None:
        if not self.tree_mode:
            self._flat, self._lazy = self.rows, True  # 不另外複製：幾十萬列也不佔額外記憶體
        else:
            self._lazy = False
            flat = []
            for r in self.rows:
                kids = r.get("children") or []
                flat.append((r, 0, bool(kids)))
                if kids and self._row_id(r) in self._open:
                    flat.extend((k, 1, False) for k in kids)
            self._flat = flat
        ids = None
        if self._sel:  # 已不存在的列不再算選取
            ids = set(self.ids())
            self._sel &= ids
        if self._cursor is not None and ids is not None and self._cursor not in ids:
            self._cursor = None

    def _entry(self, i: int):
        return (self._flat[i], 0, False) if self._lazy else self._flat[i]

    def __len__(self) -> int:
        return len(self._flat)

    def ids(self) -> list:
        return [self._row_id(self._entry(i)[0]) for i in range(len(self._flat))]

    def index_of(self, rid) -> int | None:
        for i in range(len(self._flat)):
            if self._row_id(self._entry(i)[0]) == rid:
                return i
        return None

    def row(self, rid):
        i = self.index_of(rid)
        return None if i is None else self._entry(i)[0]

    def expand(self, rid, opened: bool = True) -> None:
        (self._open.add if opened else self._open.discard)(rid)
        self._reflow()
        self.refresh()

    def is_open(self, rid) -> bool:
        return rid in self._open

    # ------------------------------------------------------------ 選取
    def selection(self) -> list:
        order = self.ids() if len(self._sel) > 1 else list(self._sel)
        return [i for i in order if i in self._sel]

    def selected_rows(self) -> list:
        return [r for r in (self.row(i) for i in self.selection()) if r is not None]

    def selection_set(self, ids) -> None:
        self._sel = set(ids)
        self._anchor = self._cursor = next(iter(ids), None) if ids else None
        self.refresh()
        if self.on_select:
            self.on_select()

    def see(self, rid) -> None:
        i = self.index_of(rid)
        if i is None:
            return
        view = max(1, self.canvas.winfo_height() - self.head_h)
        if i * self.row_h < self._top:
            self._top = i * self.row_h
        elif (i + 1) * self.row_h > self._top + view:
            self._top = (i + 1) * self.row_h - view
        self.refresh()

    # ------------------------------------------------------------ 捲動
    def _content_h(self) -> int:
        return len(self._flat) * self.row_h

    def _content_w(self) -> int:
        return sum(self._widths)

    def yview(self, *args) -> None:
        view = max(1, self.canvas.winfo_height() - self.head_h)
        total = max(1, self._content_h())
        if args and args[0] == "moveto":
            self._top = float(args[1]) * total
        elif args and args[0] == "scroll":
            step = self.row_h if args[2] == "units" else view - self.row_h
            self._top += int(args[1]) * step
        self._top = int(max(0, min(self._top, total - view)))
        self.refresh()

    def xview(self, *args) -> None:
        view = max(1, self.canvas.winfo_width())
        total = max(1, self._content_w())
        if args and args[0] == "moveto":
            self._left = float(args[1]) * total
        elif args and args[0] == "scroll":
            self._left += int(args[1]) * (px(40) if args[2] == "units" else view)
        self._left = int(max(0, min(self._left, total - view)))
        self.refresh()

    # ------------------------------------------------------------ 繪製
    def refresh(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._draw)

    def redraw_now(self) -> None:
        self._pending = True
        self._draw()

    def _fit_widths(self) -> None:
        """讓 stretch 欄吸收多出來（或不足）的寬度。"""
        avail = self.canvas.winfo_width()
        for i, col in enumerate(self.columns):
            if col.stretch and avail > 1:
                others = sum(w for j, w in enumerate(self._widths) if j != i)
                self._widths[i] = max(px(col.width), avail - others)
                break

    def _draw(self) -> None:
        self._pending = False
        if not self.winfo_exists():
            return
        c, p = self.canvas, theme.P
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            return
        if self._drag is None:
            self._fit_widths()
        view = max(1, h - self.head_h)
        total_h, total_w = max(1, self._content_h()), max(1, self._content_w())
        self._top = int(max(0, min(self._top, total_h - view)))
        self._left = int(max(0, min(self._left, total_w - w)))
        c.delete("all")
        first = self._top // self.row_h
        last = min(len(self._flat), first + view // self.row_h + 2)
        pad = px(6)
        try:
            focused = c.focus_get() is c
        except KeyError:  # Tk 的下拉選單開啟時 focus_get 會找不到元件
            focused = False
        for i in range(first, last):
            row, depth, has_kids = self._entry(i)
            rid = self._row_id(row)
            data = self._render(row)
            y0 = self.head_h + i * self.row_h - self._top
            y1 = y0 + self.row_h
            if rid in self._sel:
                c.create_rectangle(0, y0, w, y1, fill=p.sel if focused else p.sel_inactive, outline="")
            elif data.get("bg"):
                c.create_rectangle(0, y0, w, y1, fill=data["bg"], outline="")
            fg = data.get("fg") or p.text
            fnt = self.bold if data.get("bold") else self.font
            cells = data.get("cells", {})
            x = -self._left
            for ci, col in enumerate(self.columns):
                cw = self._widths[ci]
                if x + cw > 0 and x < w:
                    cx0, avail = x + pad, cw - 2 * pad
                    if ci == 0 and self.tree_mode:
                        if has_kids:
                            c.create_image(x + px(2), (y0 + y1) // 2, anchor="w",
                                           image=theme.icon("collapse" if rid in self._open else "expand"))
                        cx0 += px(14) + depth * px(16)
                        avail -= px(14) + depth * px(16)
                    self._cell(col, cells.get(col.id), cx0, y0, avail, fg, fnt)
                x += cw
        # 欄位標題（最後畫，蓋在列上面）
        c.create_rectangle(0, 0, w, self.head_h, fill=p.bg, outline="")
        x = -self._left
        for ci, col in enumerate(self.columns):
            cw = self._widths[ci]
            if x + cw > 0 and x < w:
                title = ellipsis(col.title, self.font, cw - 2 * pad, self._cache)
                anchor = {"e": "e", "center": "center"}.get(col.anchor, "w")
                tx = x + cw - pad if anchor == "e" else x + cw // 2 if anchor == "center" else x + pad
                c.create_text(tx, self.head_h // 2, text=title, anchor=anchor, font=self.font, fill=p.text)
                if self.sort and self.sort[0] == col.id:  # 排序方向的小箭頭（欄位標題上緣中央）
                    cxm, a, b = x + cw // 2, px(2), px(6)
                    ya, yb = (a, b) if self.sort[1] else (b, a)
                    c.create_line(cxm - px(4), ya, cxm, yb, cxm + px(4), ya, fill=p.text2)
                c.create_line(x + cw - 1, px(3), x + cw - 1, self.head_h - px(3), fill=p.line)
            x += cw
        c.create_line(0, self.head_h - 1, w, self.head_h - 1, fill=p.line)
        self.vbar.set(self._top / total_h, min(1.0, (self._top + view) / total_h))
        self.hbar.set(self._left / total_w, min(1.0, (self._left + w) / total_w))

    def _cell(self, col: Column, value, x: int, y0: int, avail: int, fg: str, fnt) -> None:
        c, p = self.canvas, theme.P
        cy = y0 + self.row_h // 2
        if value is None or avail <= 0:
            return
        if col.kind == "icon":
            if value:
                c.create_image(x + avail // 2, cy, image=theme.icon(value), anchor="center")
            return
        if col.kind == "progress":
            frac, label, state = value
            if frac is None:
                if label:
                    c.create_text(x, cy, text=ellipsis(label, fnt, avail, self._cache), anchor="w", font=fnt, fill=fg)
                return
            y_top, y_bot = y0 + px(3), y0 + self.row_h - px(3)
            c.create_rectangle(x, y_top, x + avail, y_bot, fill=p.trough, outline=p.border)
            fill_w = int((avail - 2) * max(0.0, min(1.0, frac)))
            if fill_w > 0:
                color = {"done": p.progress, "fail": p.danger, "paused": p.warn}.get(state, p.progress_run)
                c.create_rectangle(x + 1, y_top + 1, x + 1 + fill_w, y_bot - 1, fill=color, outline="")
            c.create_text(x + avail // 2, cy, text=label, anchor="center", font=fnt, fill=p.text)
            return
        text = value
        if col.kind == "icontext":
            name, text = value
            if name:
                c.create_image(x, cy, image=theme.icon(name), anchor="w")
            x += px(20)
            avail -= px(20)
            if avail <= 0:
                return
        text = ellipsis(str(text), fnt, avail, self._cache)
        if col.anchor == "e":
            c.create_text(x + avail, cy, text=text, anchor="e", font=fnt, fill=fg)
        elif col.anchor == "center":
            c.create_text(x + avail // 2, cy, text=text, anchor="center", font=fnt, fill=fg)
        else:
            c.create_text(x, cy, text=text, anchor="w", font=fnt, fill=fg)

    # ------------------------------------------------------------ 滑鼠與鍵盤
    def _index_at(self, y: int) -> int | None:
        if y < self.head_h:
            return None
        i = (y - self.head_h + self._top) // self.row_h
        return i if 0 <= i < len(self._flat) else None

    def _sep_at(self, x: int) -> int | None:
        """滑鼠是否在某一欄右邊的分隔線上（可拖曳調整欄寬）。"""
        edge = -self._left
        for i, w in enumerate(self._widths):
            edge += w
            if abs(x - edge) <= px(4):
                return i
        return None

    def _col_at(self, x: int) -> int | None:
        edge = -self._left
        for i, w in enumerate(self._widths):
            if edge <= x < edge + w:
                return i
            edge += w
        return None

    def _motion(self, e) -> None:
        on_sep = e.y < self.head_h and self._sep_at(e.x) is not None
        self.canvas.configure(cursor="sb_h_double_arrow" if on_sep else "")

    def _click(self, e) -> None:
        self.canvas.focus_set()
        if e.y < self.head_h:
            sep = self._sep_at(e.x)
            if sep is not None:
                self._drag = (sep, e.x, self._widths[sep])
            elif self.on_header and (ci := self._col_at(e.x)) is not None:
                self.on_header(self.columns[ci].id)
            return
        i = self._index_at(e.y)
        if i is None:
            if self._sel:
                self.selection_set([])
            return
        row, depth, has_kids = self._entry(i)
        rid = self._row_id(row)
        if self.tree_mode and has_kids and e.x + self._left < px(18):
            self.expand(rid, rid not in self._open)
            return
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        if self.select_mode == "extended" and shift and self._anchor is not None:
            a = self.index_of(self._anchor)
            lo, hi = sorted((a if a is not None else i, i))
            self._sel = {self._row_id(self._entry(k)[0]) for k in range(lo, hi + 1)}
        elif self.select_mode == "extended" and ctrl:
            self._sel ^= {rid}
            self._anchor = rid
        else:
            self._sel = {rid}
            self._anchor = rid
        self._cursor = rid
        self.refresh()
        if self.on_select:
            self.on_select()

    def _drag_move(self, e) -> None:
        if self._drag:
            i, x0, w0 = self._drag
            self._widths[i] = max(px(30), w0 + e.x - x0)
            self.refresh()

    def _double(self, e) -> None:
        i = self._index_at(e.y)
        if i is None:
            return
        row, _depth, has_kids = self._entry(i)
        if self.tree_mode and has_kids and not self.on_open:
            self.expand(self._row_id(row), self._row_id(row) not in self._open)
        elif self.on_open:
            self.on_open(row)

    def _context(self, e) -> None:
        self.canvas.focus_set()
        i = self._index_at(e.y)
        row = None
        if i is not None:
            row = self._entry(i)[0]
            rid = self._row_id(row)
            if rid not in self._sel:
                self._sel = {rid}
                self._anchor = self._cursor = rid
                self.refresh()
                if self.on_select:
                    self.on_select()
        if self.on_menu:
            self.on_menu(e, row)

    def _key(self, e) -> None:
        n = len(self._flat)
        if not n:
            return
        if e.keysym.lower() == "a" and e.state & 0x4 and self.select_mode == "extended":
            self.selection_set(self.ids())
            return
        page = max(1, (self.canvas.winfo_height() - self.head_h) // self.row_h - 1)
        step = {"Up": -1, "Down": 1, "Prior": -page, "Next": page, "Home": -n, "End": n}.get(e.keysym)
        cur = self.index_of(self._cursor) if self._cursor is not None else None
        if step is None:
            if e.keysym == "Return" and cur is not None and self.on_open:
                self.on_open(self._entry(cur)[0])
            elif e.keysym in ("Right", "Left") and cur is not None and self.tree_mode:
                row, _d, has_kids = self._entry(cur)
                if has_kids:
                    self.expand(self._row_id(row), e.keysym == "Right")
            return
        i = max(0, min(n - 1, (cur if cur is not None else (-1 if step > 0 else n)) + step))
        rid = self._row_id(self._entry(i)[0])
        if e.state & 0x1 and self.select_mode == "extended" and self._anchor is not None:
            a = self.index_of(self._anchor)
            lo, hi = sorted((a if a is not None else i, i))
            self._sel = {self._row_id(self._entry(k)[0]) for k in range(lo, hi + 1)}
        else:
            self._sel = {rid}
            self._anchor = rid
        self._cursor = rid
        self.see(rid)
        if self.on_select:
            self.on_select()
