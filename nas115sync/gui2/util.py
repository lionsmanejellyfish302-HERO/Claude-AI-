"""GUI 共用工具。"""
from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


def human_size(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def fmt_date(ts: float | None) -> str:
    """清單用的日期時間（到分鐘）。"""
    if not ts:
        return ""
    return time.strftime("%Y/%m/%d %H:%M", time.localtime(ts))


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds == float("inf"):
        return ""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def open_local(path: str) -> None:
    """在檔案總管中開啟（檔案則選取它）。"""
    if sys.platform == "win32":
        if os.path.isfile(path):
            from ..savedata import system_exe

            subprocess.Popen([system_exe("explorer.exe"), "/select,", path])
        else:
            os.startfile(path if os.path.isdir(path) else os.path.dirname(path))  # type: ignore[attr-defined]
    else:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.Popen(["xdg-open", target])


def open_file(path: str) -> None:
    """用系統預設的程式開啟檔案。"""
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


class UiQueue:
    """背景執行緒把要在 Tk 主執行緒執行的函式丟進來。"""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.root.after(100, self._poll)

    def call(self, fn, *args) -> None:
        self.q.put((fn, args))

    def _poll(self) -> None:
        try:
            for _ in range(500):
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    logging.getLogger(__name__).exception("GUI 更新失敗")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


class QueueLogHandler(logging.Handler):
    def __init__(self, ui: UiQueue, sink):
        super().__init__(logging.INFO)
        self.ui = ui
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ui.call(self.sink, self.format(record), record.levelno)
        except Exception:  # noqa: BLE001
            pass


class AutoScrollbar(ttk.Scrollbar):
    """內容放得下時隱藏滑塊與箭頭（只剩與底色相同的軌道），需要捲動時才出現；寬度不變所以版面不會跳動。"""

    def __init__(self, parent, orient: str = "vertical", **kw):
        base = ("Vertical" if orient == "vertical" else "Horizontal") + ".TScrollbar"
        self._styles = (base, "Idle." + base)
        super().__init__(parent, orient=orient, style=self._styles[1], **kw)

    def set(self, first, last) -> None:
        idle = float(first) <= 0.0 and float(last) >= 1.0
        style = self._styles[1 if idle else 0]
        if str(self.cget("style")) != style:
            self.configure(style=style)
        super().set(first, last)


class Flow(ttk.Frame):
    """會自動換行的按鈕列：視窗變窄時換到下一行，不會把按鈕擠出畫面。按鈕須以此框為父元件再 add()。"""

    def __init__(self, parent, gap: int = 6, **kw):
        from .theme import px

        super().__init__(parent, **kw)
        self._items: list = []
        self._gap = px(gap)
        self._busy = self._pending = False
        self.bind("<Configure>", lambda e: self._relayout())

    def add(self, *widgets) -> None:
        self._items.extend(widgets)
        for w in widgets:  # 按鈕文字改變時重新排列
            w.bind("<Configure>", lambda e: self._schedule(), add="+")
        self._relayout()

    def _schedule(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._run_pending)

    def _run_pending(self) -> None:
        self._pending = False
        if self.winfo_exists():
            self._relayout()

    def _relayout(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            natural = sum(w.winfo_reqwidth() for w in self._items) + self._gap * max(0, len(self._items) - 1)
            # 不能只在 mapped 時才看實際寬度：版面常在顯示前就定案，之後不會再收到 <Configure>
            width = self.winfo_width() if self.winfo_width() > 1 else natural
            rows: list[list] = [[]]
            x = 0
            for w in self._items:
                ww = w.winfo_reqwidth()
                if x and x + ww > width:
                    rows.append([])
                    x = 0
                rows[-1].append(w)
                x += ww + self._gap
            y = 0
            for row in rows:
                row_h = max((w.winfo_reqheight() for w in row), default=0)
                x = 0
                for w in row:  # 同一列垂直置中
                    w.place(x=x, y=y + (row_h - w.winfo_reqheight()) // 2)
                    x += w.winfo_reqwidth() + self._gap
                y += row_h + self._gap
            height = max(0, y - self._gap)
            # 只要求最寬那顆按鈕的寬度，讓外層決定實際寬度（要求全部寬度會把視窗撐出畫面）
            widest = max((w.winfo_reqwidth() for w in self._items), default=1)
            if int(self.cget("height") or 0) != height or int(self.cget("width") or 0) != widest:
                self.configure(height=height, width=widest)
        finally:
            self._busy = False


_TOKENS = re.compile(r"[A-Za-z0-9_./\\:@%+#=~-]+|\s|.")
_NO_LINE_START = set("，。、；：！？）」』》〉】,.;:!?)")  # 不放在行首的標點（避頭點）
_NO_LINE_END = set("（「『《〈【(")  # 不放在行尾的標點（避尾點）


def flow_text(text: str, font: tkfont.Font, width: int) -> str:
    """依寬度斷行：中文可在任意字之間換行，英文單字與數字保持完整，標點不放行首。
    （Tk 只會在空白處換行，像「115 每種裝置…」這種中英混排會在 115 之後就整行斷開。）"""
    lines = []
    for para in text.split("\n"):
        line = ""
        for tok in _TOKENS.findall(para):
            if not line or tok in _NO_LINE_START or font.measure(line + tok) <= width:
                line += tok
            else:
                carry = ""
                while len(line) > 1 and line[-1] in _NO_LINE_END:
                    line, carry = line[:-1], line[-1] + carry
                lines.append(line.rstrip())
                line = carry + tok.lstrip()
        lines.append(line.rstrip())
    return "\n".join(lines)


def wrap_label(parent, text: str = "", style: str = "Secondary.TLabel", **kw) -> ttk.Label:
    """隨可用寬度自動換行的說明文字（用 pack(fill="x") 或 grid(sticky="ew") 擺放）。"""
    from .theme import px

    font = tkfont.nametofont(str(ttk.Style(parent).lookup(style, "font") or "TkDefaultFont"))
    label = ttk.Label(parent, text=flow_text(text, font, px(240)), style=style, justify="left", **kw)
    state = {"width": px(240)}

    def fit(event) -> None:
        if abs(state["width"] - event.width) > 2:
            state["width"] = max(px(120), event.width)
            label.configure(text=flow_text(text, font, state["width"]))

    label.bind("<Configure>", fit)
    return label


def ellipsis(text: str, font: tkfont.Font, width: int, cache: dict | None = None) -> str:
    """放不下時在結尾加上「…」。"""
    key = (text, width)
    if cache is not None and key in cache:
        return cache[key]
    out = text
    if width <= 0:
        out = ""
    elif font.measure(text) > width:
        lo, hi = 0, len(text)
        while lo < hi:  # 二分搜尋放得下的最長開頭
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid] + "…") <= width:
                lo = mid
            else:
                hi = mid - 1
        out = text[:lo] + "…" if lo > 0 else ""
    if cache is not None:
        if len(cache) > 5000:
            cache.clear()
        cache[key] = out
    return out


def scrolled_text(parent, height: int = 10, wrap: str = "word", **kw) -> tk.Text:
    from .theme import px

    frame = ttk.Frame(parent)
    kw.setdefault("padx", px(6))
    kw.setdefault("pady", px(4))
    text = tk.Text(frame, height=height, wrap=wrap, **kw)
    sb = AutoScrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    text.frame = frame  # type: ignore[attr-defined]
    return text


def set_text(text: tk.Text, content: str) -> None:
    text.configure(state="normal")
    text.delete("1.0", "end")
    text.insert("1.0", content)
    text.configure(state="disabled")


def make_tree(parent, columns: list[tuple[str, str, int]], height: int = 10, selectmode: str = "extended",
              first: tuple[str, int] | None = None, hscroll: bool = False, sortable: bool = False) -> ttk.Treeview:
    """清單。first=(標題, 寬度) 時第一欄可放圖示與名稱；sortable=True 時點欄位標題排序。
    回傳的 tree.frame 是要擺放的外框。"""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings" if first else "headings",
                        height=height, selectmode=selectmode)
    if first:
        tree.heading("#0", text=first[0], anchor="w")
        tree.column("#0", width=first[1], minwidth=60, stretch=True)
    for cid, label, width in columns:
        tree.heading(cid, text=label, anchor="w")
        tree.column(cid, width=width, minwidth=30, stretch=(not first and width >= 200), anchor="w")
    sb = AutoScrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    if hscroll:
        hb = AutoScrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(xscrollcommand=hb.set)
        hb.grid(row=1, column=0, sticky="ew")
    tree.frame = frame  # type: ignore[attr-defined]
    if sortable:
        tree.sort_state = (None, False)  # type: ignore[attr-defined]
        for cid in ["#0"] * bool(first) + [c[0] for c in columns]:
            tree.heading(cid, command=lambda c=cid: sort_tree(tree, c))
    return tree


def _sort_key(v: str):
    try:
        return (0, float(v.replace(",", "")), "")
    except ValueError:
        return (1, 0.0, v.casefold())


def sort_tree(tree: ttk.Treeview, col: str, reverse: bool | None = None) -> None:
    """依欄位排序（再點一次反向）。"""
    prev_col, prev_rev = getattr(tree, "sort_state", (None, False))
    if reverse is None:
        reverse = not prev_rev if prev_col == col else False
    keys = getattr(tree, "sort_keys", {})  # {(iid, 欄位): 排序用的值}，沒有時用顯示的文字
    items = []
    for iid in tree.get_children(""):
        raw = keys.get((iid, col))
        if raw is None:
            raw = _sort_key(tree.item(iid, "text") if col == "#0" else str(tree.set(iid, col)))
        items.append((raw, iid))
    items.sort(key=lambda x: x[0], reverse=reverse)
    for i, (_k, iid) in enumerate(items):
        tree.move(iid, "", i)
    tree.sort_state = (col, reverse)  # type: ignore[attr-defined]


class Tooltip:
    """滑鼠停在元件上時顯示的說明。"""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 500):
        self.widget, self.text, self.delay = widget, text, delay
        self._after = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _event=None) -> None:
        self._cancel()
        if self.text:
            self._after = self.widget.after(self.delay, self._show)

    def _leave(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        from . import theme

        self._after = None
        if self._tip is not None or not self.widget.winfo_exists():
            return
        try:
            x = self.widget.winfo_rootx() + theme.px(8)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + theme.px(4)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.configure(background=theme.P.border)
            tk.Label(tip, text=self.text, background="#FFFFE1" if not theme.P.dark else "#2B2B2B",
                     foreground=theme.P.text, font="UI", padx=theme.px(5), pady=theme.px(2), justify="left").pack(
                padx=1, pady=1)
            tip.wm_geometry(f"+{x}+{y}")
            self._tip = tip
        except tk.TclError:
            self._tip = None


def tip(widget: tk.Misc, text: str):
    Tooltip(widget, text)
    return widget


# --------------------------------------------------------------------------- 選單
def build_menu(parent: tk.Misc, items: list) -> tk.Menu:
    """items：None（分隔線）或 dict(label, command, icon, enabled, check, submenu)。"""
    from . import theme

    menu = tk.Menu(parent, tearoff=0)
    menu._images = []  # type: ignore[attr-defined]  # 保留圖片參照
    for it in items:
        if it is None:
            menu.add_separator()
            continue
        kw: dict = {"label": it["label"], "state": "normal" if it.get("enabled", True) else "disabled"}
        if it.get("icon"):
            img = theme.icon(it["icon"], disabled=not it.get("enabled", True))
            menu._images.append(img)  # type: ignore[attr-defined]
            kw.update(image=img, compound="left")
        if it.get("submenu") is not None:
            menu.add_cascade(menu=build_menu(menu, it["submenu"]), **kw)
        elif "check" in it:
            var = tk.BooleanVar(menu, value=bool(it["check"]))
            menu._images.append(var)  # type: ignore[attr-defined]
            menu.add_checkbutton(variable=var, command=it.get("command"), **kw)
        else:
            menu.add_command(command=it.get("command"), **kw)
    return menu


def popup(parent: tk.Misc, items: list, x: int, y: int) -> None:
    menu = build_menu(parent, items)
    try:
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()


def popup_below(widget: tk.Misc, items: list) -> None:
    """在按鈕正下方開啟選單（工具列的下拉按鈕）。"""
    popup(widget, items, widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())


def menu_item(items: list, label: str) -> dict:
    """（測試與鍵盤操作用）在選單項目中依名稱找一項，含子選單。"""
    for it in items:
        if it is None:
            continue
        if it["label"] == label:
            return it
        if it.get("submenu"):
            try:
                return menu_item(it["submenu"], label)
            except KeyError:
                pass
    raise KeyError(label)
