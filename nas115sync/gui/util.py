"""GUI 共用工具。"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
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


def scrolled_text(parent, height: int = 10, wrap: str = "word", **kw) -> tk.Text:
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, wrap=wrap, **kw)
    sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
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
                     selectmode: str = "extended") -> ttk.Treeview:
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                        selectmode=selectmode)
    for cid, label, width in columns:
        tree.heading(cid, text=label)
        tree.column(cid, width=width, stretch=width >= 200)
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    tree.frame = frame  # type: ignore[attr-defined]
    return tree
