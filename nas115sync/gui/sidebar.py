"""側邊欄導覽（類似 macOS「系統設定」與 Finder）：單色圖示、圓角膠囊選取、待核對數量、即時同步狀態。"""
from __future__ import annotations

import tkinter as tk

from . import theme
from .theme import ROW, SIDEBAR, XL, XS, XXL, L, M, S, px


class Sidebar(tk.Canvas):
    def __init__(self, parent, on_select, title: str = "NAS115Sync", subtitle: str = "NAS ⇄ 115 網盤"):
        p = theme.P
        self.w = px(SIDEBAR)
        super().__init__(parent, width=self.w, background=p.sidebar, highlightthickness=0, borderwidth=0)
        self.on_select = on_select
        self.items: list[dict] = []
        self.selected: str | None = None
        self.hover: str | None = None
        pill_w, self.pill_h = self.w - 2 * px(S + XS), px(ROW) - px(2)
        self.img_sel = theme.round_rect(pill_w, self.pill_h, px(S), p.nav_selected)
        self.img_hover = theme.round_rect(pill_w, self.pill_h, px(S), p.nav_hover)
        self.create_text(px(L), px(XL), text=title, anchor="w", font="NSHeadline", fill=p.text)
        self.create_text(px(L), px(XL) + px(L), text=subtitle, anchor="w", font="NSCaption", fill=p.text2)
        self.next_y = px(XXL) + px(XL)
        # 底部：即時同步狀態
        self.dot = self.create_oval(0, 0, 0, 0, fill=p.text3, outline="")
        self.status = self.create_text(0, 0, text="", anchor="w", font="NSCaption", fill=p.text2)
        self.bind("<Configure>", self._place_footer)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)

    def add(self, key: str, title: str, icon: str) -> None:
        p = theme.P
        y = self.next_y
        cy = y + px(ROW) // 2
        bg = self.create_image(px(S + XS), y + px(1), image=self.img_sel, anchor="nw", state="hidden")
        ic = self.create_text(px(L), cy, text=theme.icon(icon) if theme.has_icons() else "", anchor="w",
                              font="NSIcon", fill=p.accent)
        x_text = px(L) + px(XL) if theme.has_icons() else px(L)  # 圖示欄寬 34，文字起點 55（費氏數列）
        tx = self.create_text(x_text, cy, text=title, anchor="w", font="NSBody", fill=p.text)
        badge = self.create_text(self.w - px(L), cy, text="", anchor="e", font="NSCaption", fill=p.text2)
        self.items.append({"key": key, "y": y, "bg": bg, "icon": ic, "text": tx, "badge": badge})
        self.next_y += px(ROW) + px(2)

    def _item_at(self, y: int):
        return next((it for it in self.items if it["y"] <= y < it["y"] + px(ROW)), None)

    def _refresh(self) -> None:
        for it in self.items:
            if it["key"] == self.selected:
                self.itemconfigure(it["bg"], image=self.img_sel, state="normal")
            elif it["key"] == self.hover:
                self.itemconfigure(it["bg"], image=self.img_hover, state="normal")
            else:
                self.itemconfigure(it["bg"], state="hidden")

    def _set_hover(self, key) -> None:
        if key != self.hover:
            self.hover = key
            self._refresh()
            self.configure(cursor="hand2" if key else "")

    def _motion(self, event) -> None:
        it = self._item_at(event.y)
        self._set_hover(it["key"] if it else None)

    def _click(self, event) -> None:
        it = self._item_at(event.y)
        if it:
            self.on_select(it["key"])

    def select(self, key: str) -> None:
        self.selected = key
        self._refresh()

    def set_badge(self, key: str, text: str) -> None:
        for it in self.items:
            if it["key"] == key:
                self.itemconfigure(it["badge"], text=text)

    def set_status(self, text: str, on: bool) -> None:
        p = theme.P
        self.itemconfigure(self.status, text=text)
        self.itemconfigure(self.dot, fill=p.green if on else p.text3)

    def _place_footer(self, event=None) -> None:
        h = self.winfo_height()
        cy = h - px(L) - px(M) // 2
        r = px(4)
        self.coords(self.dot, px(L), cy - r, px(L) + 2 * r, cy + r)
        self.coords(self.status, px(L) + 2 * r + px(S), cy)
