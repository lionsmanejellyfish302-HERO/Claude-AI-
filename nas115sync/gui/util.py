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


def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass


def setup_fonts(root: tk.Tk) -> None:
    from tkinter import font

    families = set(font.families(root))
    for fam in ("Microsoft JhengHei UI", "Microsoft JhengHei", "Noto Sans CJK TC", "Noto Sans TC",
                "PingFang TC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei"):
        if fam in families:
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                font.nametofont(name).configure(family=fam, size=10)
            break


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
    """內容放得下時隱藏滑塊（只畫軌道），需要捲動時才出現。"""

    def __init__(self, parent, style: str = "Vertical.TScrollbar", **kw):
        self._styles = (style, "Idle." + style)
        super().__init__(parent, style=self._styles[1], **kw)

    def set(self, first, last) -> None:
        idle = float(first) <= 0.0 and float(last) >= 1.0
        style = self._styles[1 if idle else 0]
        if str(self.cget("style")) != style:
            self.configure(style=style)
        super().set(first, last)


class Flow(ttk.Frame):
    """會自動換行的按鈕列：視窗變窄時換到下一行，不會把按鈕擠出畫面。按鈕須以此框為父元件再 add()。"""

    def __init__(self, parent, gap: int = 8, **kw):
        from .theme import px

        super().__init__(parent, **kw)
        self._items: list = []
        self._gap = px(gap)
        self._busy = self._pending = False
        self.bind("<Configure>", lambda e: self._relayout())

    def add(self, *widgets) -> None:
        self._items.extend(widgets)
        for w in widgets:  # 按鈕文字改變（例如「開始／停止即時同步」）時重新排列
            w.bind("<Configure>", lambda e: self._schedule(), add="+")
        self._relayout()

    def _schedule(self) -> None:
        if not self._pending:
            self._pending = True
            self.after_idle(self._run_pending)

    def _run_pending(self) -> None:
        self._pending = False
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


def scrolled_text(parent, height: int = 10, wrap: str = "word", **kw) -> tk.Text:
    from .theme import S, px

    frame = ttk.Frame(parent)
    kw.setdefault("padx", px(S))
    kw.setdefault("pady", px(S))
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


def tree_with_scroll(parent, columns: list[tuple[str, str, int]], height: int = 15,
                     selectmode: str = "extended", icon_col: tuple[str, int] | None = None) -> ttk.Treeview:
    """清單；icon_col=(標題, 寬度) 時第一欄可放圖示與名稱（Finder 風格）。"""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="tree headings" if icon_col else "headings",
                        height=height, selectmode=selectmode)
    if icon_col:
        tree.heading("#0", text=icon_col[0], anchor="w")
        tree.column("#0", width=icon_col[1], stretch=True)
    for cid, label, width in columns:  # 與 Finder 一樣靠左對齊
        tree.heading(cid, text=label, anchor="w")
        tree.column(cid, width=width, stretch=width >= 200, anchor="w")
    sb = AutoScrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.frame = frame  # type: ignore[attr-defined]
    return tree
