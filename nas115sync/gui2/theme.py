"""介面主題：比照 Air Explorer 的 Windows 桌面程式外觀。

白色內容區、淺灰色的狀態列與對話框、1 像素細框線、藍色線條圖示、淺藍色選取與工具列按鈕，
另有一套深色主題。所有尺寸以「邏輯像素」表示，依螢幕縮放比例換算（px()），高解析度螢幕上一樣清楚。
"""
from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

from . import icons


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str             # 內容區（清單、頁面）
    chrome: str         # 狀態列、分頁列、對話框
    line: str           # 細分隔線、面板外框
    border: str         # 按鈕外框
    field_border: str   # 輸入框外框
    text: str
    text2: str          # 次要文字
    text3: str          # 停用
    accent: str
    link: str
    sel: str            # 選取（有焦點）
    sel_line: str
    sel_inactive: str   # 選取（沒有焦點）
    hover: str
    tool_on: str        # 工具列：目前頁面／按下的切換鈕
    tool_hover: str
    special: str        # 工具列的醒目按鈕（待核對）
    button: str
    button_hover: str
    button_press: str
    field: str
    field_ro: str       # 下拉選單（只能選）
    ok: str
    warn: str
    danger: str
    progress: str       # 已完成的進度（綠）
    progress_run: str   # 進行中的進度（藍）
    trough: str
    thumb: str          # 捲軸滑塊
    thumb_hover: str
    menu_sel: str


LIGHT = Palette(
    dark=False, bg="#FFFFFF", chrome="#F0F0F0", line="#D9D9D9", border="#ADADAD", field_border="#7A7A7A",
    text="#000000", text2="#5C5C5C", text3="#A0A0A0", accent="#0078D7", link="#0066CC",
    sel="#CCE8FF", sel_line="#99D1FF", sel_inactive="#D9D9D9", hover="#E5F3FF",
    tool_on="#CCE4F7", tool_hover="#E5F1FB", special="#D9D9D9",
    button="#E1E1E1", button_hover="#E5F1FB", button_press="#CCE4F7", field="#FFFFFF", field_ro="#E1E1E1",
    ok="#107C10", warn="#B35900", danger="#C42B1C", progress="#2BC253", progress_run="#8CC4EE", trough="#E6E6E6",
    thumb="#CDCDCD", thumb_hover="#A6A6A6", menu_sel="#91C9F7")
DARK = Palette(
    dark=True, bg="#141414", chrome="#202020", line="#3C3C3C", border="#5E5E5E", field_border="#707070",
    text="#F2F2F2", text2="#ABABAB", text3="#6B6B6B", accent="#3AA0F3", link="#63B5FF",
    sel="#0F4C7D", sel_line="#1F74B8", sel_inactive="#383838", hover="#1D3447",
    tool_on="#17466D", tool_hover="#23323F", special="#3A3A3A",
    button="#333333", button_hover="#3A4A57", button_press="#17466D", field="#1C1C1C", field_ro="#333333",
    ok="#6CCB5F", warn="#F2A93B", danger="#FF7B72", progress="#2BB24C", progress_run="#2F78AD", trough="#2E2E2E",
    thumb="#4F4F4F", thumb_hover="#7A7A7A", menu_sel="#0F4C7D")

P: Palette = LIGHT
_scale = 1.0
_font_objs: dict = {}
ROW = 22      # 清單列高（build 之後依字型調整）
TOOL = 32     # 主工具列圖示
SMALL = 16    # 一般圖示


def px(n: float) -> int:
    """邏輯像素 → 實際像素（依螢幕縮放）。"""
    return max(1, round(n * _scale)) if n else 0


def icon(name: str, size: int = SMALL, disabled: bool = False) -> tk.PhotoImage:
    """圖示（size 為邏輯像素）。"""
    return icons.get(name, px(size), P.dark, disabled)


def icon_pair(name: str, size: int = SMALL) -> tuple:
    """給 ttk 按鈕的 image 選項：一般狀態與停用狀態各一張。"""
    return (icon(name, size), "disabled", icon(name, size, True))


def file_icon(name: str, is_dir: bool) -> tk.PhotoImage:
    return icon("folder" if is_dir else icons.file_icon_name(name))


# --------------------------------------------------------------------------- 外觀判斷
def system_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve_mode(mode: str) -> bool:
    return {"dark": True, "light": False}.get(mode, system_prefers_dark())


def apply_titlebar(win: tk.Misc) -> None:
    """Windows 10/11：標題列跟著深色／淺色。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1 if P.dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新／舊版 Windows）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError):
        pass


# --------------------------------------------------------------------------- 字型
def _pick(families: set[str], *names: str) -> str | None:
    return next((n for n in names if n in families), None)


def _fonts(root: tk.Misc) -> None:
    fams = set(tkfont.families(root))
    ui = _pick(fams, "Microsoft JhengHei UI", "Microsoft JhengHei", "PingFang TC", "Noto Sans CJK TC", "Noto Sans TC",
               "WenQuanYi Zen Hei", "Segoe UI") or "TkDefaultFont"
    mono = _pick(fams, "Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono") or "TkFixedFont"
    spec = {
        "UI": (ui, 9, "normal", 0), "UIBold": (ui, 9, "bold", 0), "UISmall": (ui, 8, "normal", 0),
        "UILink": (ui, 9, "normal", 1), "UIBig": (ui, 11, "bold", 0), "UITitle": (ui, 13, "normal", 0),
        "UIMono": (mono, 9, "normal", 0),
    }
    for name, (family, size, weight, underline) in spec.items():
        try:
            tkfont.nametofont(name, root).configure(family=family, size=size, weight=weight, underline=underline)
        except tk.TclError:
            _font_objs[name] = tkfont.Font(root, name=name, family=family, size=size, weight=weight,
                                           underline=underline)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name, root).configure(family=ui, size=9)
        except tk.TclError:
            pass


def font(name: str = "UI") -> tkfont.Font:
    return tkfont.nametofont(name)


# --------------------------------------------------------------------------- ttk 樣式
def _image_element(style: ttk.Style, name: str, normal, *states, **kw) -> None:
    try:
        style.element_create(name, "image", normal, *states, **kw)
    except tk.TclError:  # 已建立過（同一個程式重複套用主題）
        pass


def build(root: tk.Tk, mode: str = "auto") -> Palette:
    """套用主題到整個程式；回傳使用中的配色。"""
    global P, _scale, ROW
    P = DARK if resolve_mode(mode) else LIGHT
    c = P
    try:
        _scale = max(1.0, root.winfo_fpixels("1i") / 96)
    except tk.TclError:
        _scale = 1.0
    icons.reset()
    _fonts(root)
    ROW = max(px(22), font("UI").metrics("linespace") + px(6))
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=c.bg)

    style.configure(".", background=c.bg, foreground=c.text, font="UI", bordercolor=c.line, darkcolor=c.bg,
                    lightcolor=c.bg, troughcolor=c.trough, focuscolor=c.accent, fieldbackground=c.field,
                    selectbackground=c.accent, selectforeground="#FFFFFF", insertcolor=c.text)
    style.map(".", foreground=[("disabled", c.text3)])

    # 底色：內容區（白）、狀態列與對話框（淺灰）、選取（淺藍）
    grounds = (("", c.bg), ("Chrome.", c.chrome), ("Sel.", c.sel), ("Hover.", c.hover))
    for prefix, ground in grounds:
        style.configure(f"{prefix}TFrame", background=ground)
        for kind, fg, fnt in (("TLabel", c.text, "UI"), ("Secondary.TLabel", c.text2, "UI"),
                              ("Bold.TLabel", c.text, "UIBold"), ("Small.TLabel", c.text2, "UISmall"),
                              ("Link.TLabel", c.link, "UILink"), ("Warning.TLabel", c.warn, "UI"),
                              ("Danger.TLabel", c.danger, "UI"), ("Success.TLabel", c.ok, "UI"),
                              ("Big.TLabel", c.text, "UIBig"), ("Title.TLabel", c.text, "UITitle"),
                              ("Mono.TLabel", c.text, "UIMono")):
            style.configure(f"{prefix}{kind}", background=ground, foreground=fg, font=fnt)
    style.configure("Line.TFrame", background=c.line)

    # ---- 按鈕（Windows 樣式：灰底細框，滑過變淺藍）
    flat = [("Button.border", {"sticky": "nswe", "border": "1", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]
    style.layout("TButton", flat)
    style.configure("TButton", background=c.button, foreground=c.text, bordercolor=c.border, lightcolor=c.button,
                    darkcolor=c.button, padding=(px(10), px(3)), width=-8, anchor="center")
    states = [("disabled", c.chrome), ("pressed", c.button_press), ("active", c.button_hover)]
    style.map("TButton", background=states, lightcolor=states, darkcolor=states,
              bordercolor=[("disabled", c.line), ("pressed", c.accent), ("active", c.accent), ("focus", c.accent)],
              foreground=[("disabled", c.text3)])
    style.configure("Accent.TButton", bordercolor=c.accent)  # 預設按鈕：藍色外框
    style.configure("Compact.TButton", width=0, padding=(px(6), px(2)))
    style.configure("Wide.TButton", padding=(px(10), px(5)))

    # ---- 工具列按鈕：平面，滑過或選取時才有淺藍底與藍框
    def tool(name: str, ground: str, on: bool = False, pad=(4, 2), fnt: str = "UI") -> None:
        fill = c.tool_on if on else ground
        edge = c.accent if on else ground
        style.layout(name, flat)
        style.configure(name, background=fill, bordercolor=edge, lightcolor=fill, darkcolor=fill, width=0,
                        padding=(px(pad[0]), px(pad[1])), font=fnt, anchor="center")
        st = [("disabled", ground), ("pressed", c.tool_on), ("active", c.tool_on if on else c.tool_hover)]
        style.map(name, background=st, lightcolor=st, darkcolor=st,
                  bordercolor=[("disabled", ground), ("pressed", c.accent), ("active", c.accent)],
                  foreground=[("disabled", c.text3)])

    tool("Tool.TButton", c.bg)
    tool("On.Tool.TButton", c.bg, on=True)
    tool("Chrome.Tool.TButton", c.chrome)
    tool("On.Chrome.Tool.TButton", c.chrome, on=True)
    tool("Main.Tool.TButton", c.bg, pad=(6, 4))
    tool("On.Main.Tool.TButton", c.bg, on=True, pad=(6, 4))
    tool("Special.Main.Tool.TButton", c.special, pad=(6, 4))
    style.map("Special.Main.Tool.TButton",
              background=[("pressed", c.tool_on), ("active", c.tool_hover)],
              lightcolor=[("pressed", c.tool_on), ("active", c.tool_hover)],
              darkcolor=[("pressed", c.tool_on), ("active", c.tool_hover)],
              bordercolor=[("pressed", c.accent), ("active", c.accent)])
    tool("Sel.Tool.TButton", c.sel)
    tool("Gutter.Tool.TButton", c.bg, pad=(3, 5))

    # ---- 輸入框與下拉選單
    style.configure("TEntry", fieldbackground=c.field, foreground=c.text, bordercolor=c.field_border,
                    lightcolor=c.field, darkcolor=c.field, padding=(px(3), px(2)), insertcolor=c.text)
    style.map("TEntry", bordercolor=[("focus", c.accent), ("disabled", c.line)],
              lightcolor=[("focus", c.field), ("disabled", c.chrome), ("readonly", c.chrome)],
              darkcolor=[("disabled", c.chrome), ("readonly", c.chrome)],
              fieldbackground=[("disabled", c.chrome), ("readonly", c.chrome)])
    style.configure("Flat.TEntry", bordercolor=c.field, lightcolor=c.field, darkcolor=c.field)  # 位址列裡面的輸入
    style.map("Flat.TEntry", bordercolor=[("focus", c.field)])
    _image_element(style, "AE.Combo.arrow", icon("chev_down", 14), ("disabled", icon("chev_down", 14, True)),
                   width=px(18), sticky="")
    style.layout("TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
        ("AE.Combo.arrow", {"side": "right", "sticky": ""}),
        ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
            ("Combobox.textarea", {"sticky": "nswe"})]})]})])
    for name in ("TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=c.field, foreground=c.text, background=c.field_ro,
                        bordercolor=c.field_border, lightcolor=c.field, darkcolor=c.field, arrowcolor=c.text,
                        arrowsize=px(12), padding=(px(3), px(2)), insertcolor=c.text,
                        selectbackground=c.accent, selectforeground="#FFFFFF")
    style.map("TCombobox",
              fieldbackground=[("readonly", "active", c.button_hover), ("readonly", c.field_ro), ("disabled", c.chrome)],
              lightcolor=[("readonly", "active", c.button_hover), ("readonly", c.field_ro), ("disabled", c.chrome)],
              darkcolor=[("readonly", "active", c.button_hover), ("readonly", c.field_ro), ("disabled", c.chrome)],
              background=[("active", c.button_hover), ("disabled", c.chrome)],
              bordercolor=[("focus", c.accent), ("active", c.accent), ("readonly", c.border), ("disabled", c.line)],
              selectbackground=[("readonly", c.field_ro)], selectforeground=[("readonly", c.text)],
              foreground=[("disabled", c.text3)], arrowcolor=[("disabled", c.text3)])
    style.map("TSpinbox", bordercolor=[("focus", c.accent)], arrowcolor=[("disabled", c.text3)],
              fieldbackground=[("disabled", c.chrome)])
    root.option_add("*TCombobox*Listbox.background", c.field)
    root.option_add("*TCombobox*Listbox.foreground", c.text)
    root.option_add("*TCombobox*Listbox.selectBackground", c.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
    root.option_add("*TCombobox*Listbox.font", "UI")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # ---- 勾選框、單選鈕（自繪圖示）
    box, gap = 15, 5
    for kind, off, on in (("check", "check_off", "check_on"), ("radio", "radio_off", "radio_on")):
        _image_element(style, f"AE.{kind}", icon(off, box), ("selected", "disabled", icon(on, box, True)),
                       ("disabled", icon(off, box, True)), ("selected", icon(on, box)),
                       width=px(box + gap), sticky="w")
    for widget, el in (("TCheckbutton", "AE.check"), ("TRadiobutton", "AE.radio")):
        style.layout(widget, [(f"{widget[1:]}.padding", {"sticky": "nswe", "children": [
            (el, {"side": "left", "sticky": ""}), (f"{widget[1:]}.label", {"side": "left", "sticky": "nswe"})]})])
        for prefix, ground in grounds[:2]:
            style.configure(f"{prefix}{widget}", background=ground, foreground=c.text, padding=(0, px(2)))
            style.map(f"{prefix}{widget}", background=[("active", ground)], foreground=[("disabled", c.text3)])

    # ---- 清單（檔案清單、資料夾樹）
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview", background=c.bg, fieldbackground=c.bg, foreground=c.text, rowheight=ROW, font="UI",
                    borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", "!focus", c.sel_inactive), ("selected", c.sel)],
              foreground=[("selected", c.text), ("disabled", c.text3)])
    style.configure("Treeview.Heading", background=c.bg, foreground=c.text, font="UI", relief="flat",
                    bordercolor=c.line, lightcolor=c.bg, darkcolor=c.bg, padding=(px(6), px(3)))
    style.map("Treeview.Heading", background=[("pressed", c.sel), ("active", c.hover)],
              lightcolor=[("pressed", c.sel), ("active", c.hover)], darkcolor=[("pressed", c.sel), ("active", c.hover)])
    _image_element(style, "AE.Treeitem.indicator", icon("expand"), ("user1", "!user2", icon("collapse")),
                   ("user2", icons.blank(px(SMALL))), width=px(SMALL), sticky="w")
    style.layout("Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
        ("AE.Treeitem.indicator", {"side": "left", "sticky": ""}), ("Treeitem.image", {"side": "left", "sticky": ""}),
        ("Treeitem.text", {"side": "left", "sticky": ""})]})])
    style.configure("Treeview.Item", padding=(px(2), 0))

    # ---- 捲軸：需要捲動時是 Windows 樣式（含上下箭頭）；不需要時只剩與底色相同的軌道
    for orient, sticky, a1, a2 in (("Vertical", "ns", "uparrow", "downarrow"), ("Horizontal", "we", "leftarrow", "rightarrow")):
        name = f"{orient}.TScrollbar"
        style.configure(name, background=c.thumb, troughcolor=c.chrome, bordercolor=c.chrome, lightcolor=c.thumb,
                        darkcolor=c.thumb, arrowcolor=c.text2, arrowsize=px(15), gripcount=0, relief="flat")
        st = [("disabled", c.chrome), ("pressed", c.text2), ("active", c.thumb_hover)]
        style.map(name, background=st, lightcolor=st, darkcolor=st, arrowcolor=[("disabled", c.text3)])
        style.layout(f"Idle.{name}", [(f"{orient}.Scrollbar.trough", {"sticky": sticky})])
        style.configure(f"Idle.{name}", troughcolor=c.bg, bordercolor=c.bg, lightcolor=c.bg, darkcolor=c.bg)

    # ---- 分頁（對話框內）
    style.configure("TNotebook", background=c.chrome, bordercolor=c.line, lightcolor=c.chrome, darkcolor=c.chrome,
                    tabmargins=(px(2), px(2), px(2), 0))
    style.configure("TNotebook.Tab", background=c.chrome, foreground=c.text, bordercolor=c.line, lightcolor=c.chrome,
                    darkcolor=c.chrome, padding=(px(8), px(2)))
    style.map("TNotebook.Tab", background=[("selected", c.bg)], lightcolor=[("selected", c.bg)],
              darkcolor=[("selected", c.bg)], expand=[("selected", (1, 1, 1, 0))])

    # ---- 進度條、分隔、分組框
    style.configure("TProgressbar", background=c.progress, troughcolor=c.trough, bordercolor=c.border,
                    lightcolor=c.progress, darkcolor=c.progress, thickness=px(15))
    style.configure("TPanedwindow", background=c.bg)
    style.configure("Sash", sashthickness=px(5), gripcount=0, background=c.bg, bordercolor=c.bg, lightcolor=c.bg,
                    darkcolor=c.bg)
    for prefix, ground in grounds[:2]:
        style.configure(f"{prefix}TLabelframe", background=ground, bordercolor=c.line, lightcolor=ground,
                        darkcolor=ground, borderwidth=2, relief="groove", padding=px(8))
        style.configure(f"{prefix}TLabelframe.Label", background=ground, foreground=c.text, font="UI")
    style.configure("TSeparator", background=c.line)

    # ---- 傳統 Tk 元件（文字方塊、選單、畫布）
    for key, value in (("*Text.background", c.field), ("*Text.foreground", c.text), ("*Text.insertBackground", c.text),
                       ("*Text.selectBackground", c.accent), ("*Text.selectForeground", "#FFFFFF"),
                       ("*Text.relief", "flat"), ("*Text.borderWidth", 0), ("*Text.highlightThickness", 1),
                       ("*Text.highlightBackground", c.line), ("*Text.highlightColor", c.accent), ("*Text.font", "UI"),
                       ("*Canvas.background", c.bg), ("*Canvas.highlightThickness", 0),
                       ("*Menu.background", c.bg if not c.dark else c.chrome), ("*Menu.foreground", c.text),
                       ("*Menu.activeBackground", c.menu_sel), ("*Menu.activeForeground", c.text),
                       ("*Menu.disabledForeground", c.text3), ("*Menu.relief", "flat"), ("*Menu.borderWidth", 1),
                       ("*Menu.activeBorderWidth", 0), ("*Menu.font", "UI"), ("*Menu.tearOff", 0),
                       ("*Toplevel.background", c.chrome), ("*Listbox.font", "UI")):
        root.option_add(key, value)
    return c


def line(parent, vertical: bool = False, color: str | None = None) -> tk.Frame:
    """1 像素的分隔線。"""
    kw = {"width": 1} if vertical else {"height": 1}
    return tk.Frame(parent, background=color or P.line, borderwidth=0, highlightthickness=0, **kw)


def boxed(parent, **kw) -> tk.Frame:
    """有 1 像素外框的容器（面板、卡片）。"""
    kw.setdefault("background", P.bg)
    return tk.Frame(parent, highlightthickness=1, highlightbackground=P.line, highlightcolor=P.line, borderwidth=0, **kw)
