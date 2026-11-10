"""比照 Air Explorer 的小元件：工具列按鈕、切換鈕、面板的分頁列、可點擊的路徑列。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme
from .theme import px
from .util import ellipsis, tip


def tool_button(parent, icon: str | None = None, text: str = "", command=None, hint: str = "",
                style: str = "Tool.TButton", size: int = theme.SMALL, **kw) -> ttk.Button:
    """工具列按鈕：圖示（可加文字）。hint 是滑鼠停留時的說明。"""
    opts: dict = {"style": style, "command": command, "takefocus": 0}
    if icon:
        opts["image"] = theme.icon_pair(icon, size)
        opts["compound"] = "left"
    if text:
        opts["text"] = text
    b = ttk.Button(parent, **opts, **kw)
    if hint:
        tip(b, hint)
    return b


class Toggle(ttk.Button):
    """按下去會保持「開啟」狀態的工具列按鈕（淺藍底、藍框）。與 tk.BooleanVar 連動。"""

    def __init__(self, parent, variable: tk.BooleanVar, icon: str | None = None, text: str = "", command=None,
                 hint: str = "", style: str = "Tool.TButton", icon_on: str | None = None):
        self.var, self._cmd, self._base = variable, command, style
        self._icons = (icon, icon_on or icon)
        opts: dict = {"style": style, "command": self._press, "takefocus": 0}
        if icon:
            opts.update(image=theme.icon_pair(icon), compound="left")
        if text:
            opts["text"] = text
        super().__init__(parent, **opts)
        if hint:
            tip(self, hint)
        self._trace = variable.trace_add("write", lambda *_: self._show())
        self.bind("<Destroy>", self._untrace, add="+")
        self._show()

    def _untrace(self, _event=None) -> None:
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _press(self) -> None:
        self.var.set(not self.var.get())
        if self._cmd:
            self._cmd()

    def _show(self) -> None:
        if not self.winfo_exists():
            return
        on = bool(self.var.get())
        self.configure(style=("On." if on else "") + self._base)
        if self._icons[0]:
            self.configure(image=theme.icon_pair(self._icons[1 if on else 0]))


class TabStrip(tk.Frame):
    """面板上方的分頁列：每個分頁是一個位置（本機或某個 115 帳號），最右邊的「＋」新增分頁。"""

    def __init__(self, parent, on_select, on_close, on_add):
        super().__init__(parent, background=theme.P.bg, borderwidth=0, highlightthickness=0)
        self.on_select, self.on_close, self.on_add = on_select, on_close, on_add
        self.tabs: list[dict] = []
        self.active = -1
        self._row = tk.Frame(self, background=theme.P.bg)
        self._row.pack(side="left", fill="y")
        self.plus = tool_button(self, "plus", command=on_add, hint="新增分頁")
        self.plus.pack(side="left", padx=(px(2), 0), pady=(px(1), 0))

    def add(self, title: str, icon: str) -> int:
        p = theme.P
        frame = tk.Frame(self._row, background=p.bg, highlightthickness=1, highlightbackground=p.bg, borderwidth=0)
        label = tk.Label(frame, text=" " + title, image=theme.icon(icon), compound="left", background=p.bg,
                         foreground=p.text2, font="UI", padx=px(6), pady=px(3))
        close = tk.Label(frame, image=theme.icon("close", 12), background=p.bg, padx=px(4), cursor="hand2")
        label.pack(side="left")
        tab = {"frame": frame, "label": label, "close": close, "title": title, "icon": icon}
        self.tabs.append(tab)
        frame.pack(side="left", padx=(0, px(1)))
        for w in (frame, label):
            w.bind("<Button-1>", lambda e, t=tab: self.on_select(self.tabs.index(t)))
            w.bind("<Button-2>", lambda e, t=tab: self.on_close(self.tabs.index(t)))
        close.bind("<Button-1>", lambda e, t=tab: self.on_close(self.tabs.index(t)))
        return len(self.tabs) - 1

    def set(self, index: int, title: str | None = None, icon: str | None = None) -> None:
        tab = self.tabs[index]
        if title is not None:
            tab["title"] = title
        if icon is not None:
            tab["icon"] = icon
        shown = tab["title"] if len(tab["title"]) <= 22 else tab["title"][:21] + "…"
        tab["label"].configure(text=" " + shown, image=theme.icon(tab["icon"]))

    def remove(self, index: int) -> None:
        self.tabs.pop(index)["frame"].destroy()
        if self.active >= len(self.tabs):
            self.active = len(self.tabs) - 1

    def select(self, index: int) -> None:
        p = theme.P
        self.active = index
        for i, tab in enumerate(self.tabs):
            on = i == index
            tab["frame"].configure(highlightbackground=p.line if on else p.bg)
            tab["label"].configure(foreground=p.text if on else p.text2)
            if on and len(self.tabs) > 1:
                tab["close"].pack(side="left")
            else:
                tab["close"].pack_forget()


class Breadcrumb(tk.Frame):
    """位址列裡的路徑：每一層都可以點；點空白處（或按 F4）改成直接輸入路徑。"""

    def __init__(self, parent, on_go, on_text):
        super().__init__(parent, background=theme.P.field, borderwidth=0, highlightthickness=0, height=theme.ROW)
        self.on_go, self.on_text = on_go, on_text
        self.crumbs: list[tuple[str, object]] = []
        self.text = ""
        self._labels: list[tk.Widget] = []
        self._var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self._var, style="Flat.TEntry")
        self.entry.bind("<Return>", self._commit)
        self.entry.bind("<Escape>", lambda e: self._show_crumbs())
        self.entry.bind("<FocusOut>", lambda e: self._show_crumbs())
        self.bind("<Button-1>", lambda e: self.edit())
        self.bind("<Configure>", lambda e: self._layout())
        self.pack_propagate(False)
        self._editing = False

    def set(self, crumbs: list[tuple[str, object]], text: str) -> None:
        self.crumbs, self.text = list(crumbs), text
        if not self._editing:
            self._layout()

    def edit(self) -> None:
        self._editing = True
        for w in self._labels:
            w.destroy()
        self._labels = []
        self._var.set(self.text)
        self.entry.pack(fill="both", expand=True)
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        self.entry.icursor("end")

    def _commit(self, _event=None) -> None:
        text = self._var.get().strip()
        self._show_crumbs()
        if text:
            self.on_text(text)

    def _show_crumbs(self) -> None:
        if self._editing:
            self._editing = False
            self.entry.pack_forget()
            self._layout()

    def _layout(self) -> None:
        if self._editing or not self.winfo_exists():
            return
        p = theme.P
        for w in self._labels:
            w.destroy()
        self._labels = []
        fnt = theme.font("UI")
        avail = max(px(40), self.winfo_width() - px(8))
        sep_w = px(theme.SMALL)
        # 從最後一層往前放，放不下的前幾層以「«」代替（與 Air Explorer 相同）
        shown: list[tuple[str, object]] = []
        used = 0
        for label, place in reversed(self.crumbs):
            w = fnt.measure(label) + px(8) + sep_w
            if shown and used + w > avail - px(18):
                break
            shown.insert(0, (label, place))
            used += w
        if len(shown) < len(self.crumbs):
            more = tk.Label(self, image=theme.icon("chev_more"), background=p.field)
            more.pack(side="left")
            self._labels.append(more)
        for i, (label, place) in enumerate(shown):
            if i or len(shown) < len(self.crumbs):
                sep = tk.Label(self, image=theme.icon("chev_right"), background=p.field)
                sep.pack(side="left")
                sep.bind("<Button-1>", lambda e: self.edit())
                self._labels.append(sep)
            text = ellipsis(label, fnt, max(px(30), avail - px(30)))
            lb = tk.Label(self, text=text, background=p.field, foreground=p.text, font="UI", padx=px(3), cursor="hand2")
            lb.pack(side="left")
            lb.bind("<Button-1>", lambda e, pl=place: self.on_go(pl))
            lb.bind("<Enter>", lambda e, w=lb: w.configure(background=p.hover))
            lb.bind("<Leave>", lambda e, w=lb: w.configure(background=p.field))
            self._labels.append(lb)


class PageHost(ttk.Frame):
    """一次只顯示一頁（主工具列切換頁面）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.pages: dict[str, tk.Widget] = {}
        self.current: str | None = None

    def add(self, name: str, page: tk.Widget) -> None:
        self.pages[name] = page

    def show(self, name: str) -> None:
        if name == self.current:
            return
        if self.current is not None:
            self.pages[self.current].pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        self.current = name


def labeled(parent, text: str, widget_factory, style: str = "TLabel", pad: int = 6):
    """「標籤 + 元件」一列。回傳 (列, 元件)。"""
    row = ttk.Frame(parent, style=style.replace("TLabel", "TFrame"))
    ttk.Label(row, text=text, style=style).pack(side="left", padx=(0, px(pad)))
    w = widget_factory(row)
    w.pack(side="left", fill="x", expand=True)
    return row, w
