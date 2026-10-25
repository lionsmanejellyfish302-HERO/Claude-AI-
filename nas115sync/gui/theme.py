"""介面主題：依 Apple 人機介面設計準則的美學（清晰、留白、圓角、柔和陰影、系統藍強調色、淺色／深色），
所有比例、間距與字級依黃金比例 φ 決定。

- 視窗 1294×800、最小 1000×618：長寬比 = φ
- 側欄 : 內容 = 1 : φ³；兩欄卡片、清單與詳細內容 = 0.382 : 0.618（1/φ² : 1/φ）
- 間距採費氏數列 3、5、8、13、21、34、55（相鄰兩數的比值趨近 φ）
- 字級以 √φ 為級距：8、10、13、16、21、26 pt（每隔一級正好 ×φ）
- 按鈕、輸入框高度與清單列高一律 34（費氏數列）；大按鈕 44（Apple 建議的最小點按尺寸）
- 圓角：輸入框 6、按鈕 8、卡片 13

圓角、陰影與勾選框都以程式即時繪製成帶透明度的 PNG（不需要任何圖檔），依螢幕縮放比例產生，
在高解析度螢幕上一樣銳利。
"""
from __future__ import annotations

import base64
import math
import struct
import sys
import tkinter as tk
import zlib
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

PHI = (1 + 5 ** 0.5) / 2
XXS, XS, S, M, L, XL, XXL = 3, 5, 8, 13, 21, 34, 55  # 費氏數列
TYPE = {"caption": 8, "body": 10, "headline": 13, "title": 16, "title2": 21, "large": 26}
WINDOW = (1294, 800)  # 1294 / 800 ≈ φ
MIN_WINDOW = (1000, 618)  # 1000 / 618 ≈ φ
SIDEBAR = round(WINDOW[0] / (1 + PHI ** 3))  # 側欄 : 內容 = 1 : φ³
MINOR, MAJOR = 1 / PHI ** 2, 1 / PHI  # 0.382 : 0.618
ROW = XL  # 清單列高
R_BUTTON, R_CARD, R_FIELD = S, M, 6
# 九宮格圖片中間可平鋪區的大小：Tk 以「平鋪」而非拉伸填滿中間，太小會造成成千上萬次繪圖而極慢
TILE, TILE_SMALL = 64, 32


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str          # 視窗（內容區）背景
    sidebar: str
    card: str
    separator: str
    border: str
    text: str
    text2: str       # 次要文字
    text3: str       # 第三層（停用、提示）
    accent: str
    accent_hover: str
    accent_pressed: str
    on_accent: str
    green: str
    orange: str
    red: str
    warn_text: str
    ok_text: str
    danger_text: str
    field: str
    button: str
    button_hover: str
    button_pressed: str
    nav_hover: str
    nav_selected: str
    shadow: float    # 陰影不透明度


LIGHT = Palette(
    dark=False, bg="#F5F5F7", sidebar="#EBEBF0", card="#FFFFFF", separator="#E5E5EA", border="#D2D2D7",
    text="#1D1D1F", text2="#6E6E73", text3="#AEAEB2", accent="#007AFF", accent_hover="#1A87FF",
    accent_pressed="#0062CC", on_accent="#FFFFFF", green="#34C759", orange="#FF9500", red="#FF3B30",
    warn_text="#C93400", ok_text="#248A3D", danger_text="#D70015", field="#FFFFFF", button="#FFFFFF",
    button_hover="#F7F7F9", button_pressed="#E8E8ED", nav_hover="#DFDFE5", nav_selected="#D3D3DA", shadow=0.07)
DARK = Palette(
    dark=True, bg="#1C1C1E", sidebar="#252528", card="#2C2C2E", separator="#38383A", border="#48484A",
    text="#F5F5F7", text2="#98989D", text3="#636366", accent="#0A84FF", accent_hover="#3395FF",
    accent_pressed="#0070E0", on_accent="#FFFFFF", green="#30D158", orange="#FF9F0A", red="#FF453A",
    warn_text="#FF9F0A", ok_text="#30D158", danger_text="#FF453A", field="#1C1C1E", button="#3A3A3C",
    button_hover="#444446", button_pressed="#2C2C2E", nav_hover="#323236", nav_selected="#3D3D41", shadow=0.35)

# 單色系統圖示（Windows 11 Segoe Fluent Icons／Windows 10 Segoe MDL2 Assets，與 SF Symbols 同為線條單色風格）
_GLYPHS = {
    "home": "", "folder": "", "transfer": "", "link": "", "flag": "",
    "game": "", "copy": "", "history": "", "settings": "", "up": "",
    "refresh": "", "search": "", "add": "", "remove": "",
}
_FALLBACK = {"up": "↑", "refresh": "⟳", "search": "搜尋", "add": "＋", "remove": "－"}

# 目前套用中的主題（build 之後才有值）
P: Palette = LIGHT
_scale = 1.0
_icon_family: str | None = None
_images: list = []  # 保留圖片參照，避免被回收
_font_objs: dict = {}  # 保留字型物件：被回收時 Tk 會刪除同名字型


def px(n: float) -> int:
    """邏輯像素 → 實際像素（依螢幕縮放）。"""
    return max(1, round(n * _scale))


def icon(name: str) -> str:
    return _GLYPHS[name] if _icon_family else _FALLBACK.get(name, "•")


def has_icons() -> bool:
    return _icon_family is not None


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


# --------------------------------------------------------------------------- 圖形（帶透明度的 PNG）
def _rgb(c: str) -> tuple[float, float, float]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _png(w: int, h: int, pixels: list[list[tuple[float, float, float, float]]]) -> bytes:
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((round(r * 255), round(g * 255), round(b * 255), round(a * 255)))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _sd_round_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> float:
    """點到圓角矩形邊緣的帶號距離（內部為負）。"""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    qx = abs(x - cx) - ((x1 - x0) / 2 - r)
    qy = abs(y - cy) - ((y1 - y0) / 2 - r)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def _sd_segment(x, y, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


def _over(dst, color, alpha):
    """把 color（不透明度 alpha）疊到 dst（直通 alpha）上。"""
    if alpha <= 0:
        return dst
    r, g, b, a = dst
    out_a = alpha + a * (1 - alpha)
    if out_a <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    mix = [(c * alpha + d * a * (1 - alpha)) / out_a for c, d in zip(color, (r, g, b))]
    return (mix[0], mix[1], mix[2], out_a)


def _photo(w: int, h: int, pixels) -> tk.PhotoImage:
    img = tk.PhotoImage(data=base64.b64encode(_png(w, h, pixels)).decode("ascii"), format="png")
    _images.append(img)
    return img


def round_rect(w: int, h: int, radius: int, fill: str | None, border: str | None = None, border_width: int = 1,
               margin: int = 0, shadow: float = 0.0, shadow_blur: int = 0, shadow_dy: int = 0,
               fill_alpha: float = 1.0, ring: str | None = None, ring_width: int = 0) -> tk.PhotoImage:
    """圓角矩形（可含外框、柔和陰影、焦點光暈），邊緣反鋸齒。"""
    x0, y0, x1, y1 = margin, margin, w - margin, h - margin
    fc, bc = (_rgb(fill) if fill else None), (_rgb(border) if border else None)
    rc = _rgb(ring) if ring else None
    rows = []
    for j in range(h):
        row = []
        for i in range(w):
            x, y = i + 0.5, j + 0.5
            px_ = (0.0, 0.0, 0.0, 0.0)
            if shadow and shadow_blur:
                d = _sd_round_rect(x, y - shadow_dy, x0, y0, x1, y1, radius)
                a = shadow * max(0.0, min(1.0, 0.5 - d / shadow_blur)) ** 2
                px_ = _over(px_, (0.0, 0.0, 0.0), a)
            d = _sd_round_rect(x, y, x0, y0, x1, y1, radius)
            if rc and ring_width:
                cov = max(0.0, min(1.0, 0.5 - d + ring_width)) - max(0.0, min(1.0, 0.5 - d))
                px_ = _over(px_, rc, cov * 0.45)
            outer = max(0.0, min(1.0, 0.5 - d))
            if bc:
                px_ = _over(px_, bc, outer)
                inner = max(0.0, min(1.0, 0.5 - (d + border_width)))
                if fc:
                    px_ = _over(px_, fc, inner * fill_alpha)
            elif fc:
                px_ = _over(px_, fc, outer * fill_alpha)
            row.append(px_)
        rows.append(row)
    return _photo(w, h, rows)


def checkbox(size: int, checked: bool, disabled: bool = False, gap: int = 0) -> tk.PhotoImage:
    """圓角勾選框；右側留 gap 像素透明空間作為與文字的間距。"""
    r = max(2, size // 4)
    w, h = size + gap, size
    fc, bc = _rgb(P.accent if checked else P.field), (None if checked else _rgb(P.border))
    rows = []
    th = size / 8
    a, b, c = (size * 0.27, size * 0.52), (size * 0.43, size * 0.68), (size * 0.74, size * 0.34)
    alpha = 0.4 if disabled else 1.0
    for j in range(h):
        row = []
        for i in range(w):
            x, y = i + 0.5, j + 0.5
            d = _sd_round_rect(x, y, 0, 0, size, size, r)
            pix = (0.0, 0.0, 0.0, 0.0)
            if bc:
                pix = _over(pix, bc, max(0.0, min(1.0, 0.5 - d)) * alpha)
                pix = _over(pix, fc, max(0.0, min(1.0, 0.5 - (d + 1))))
            else:
                pix = _over(pix, fc, max(0.0, min(1.0, 0.5 - d)) * alpha)
                dc = min(_sd_segment(x, y, *a, *b), _sd_segment(x, y, *b, *c)) - th
                pix = _over(pix, (1.0, 1.0, 1.0), max(0.0, min(1.0, 0.5 - dc)) * (d < 0))
            row.append(pix)
        rows.append(row)
    return _photo(w, h, rows)


def chevron(size: int, color: str, margin: int = 0) -> tk.PhotoImage:
    """下拉選單的 ⌄ 箭頭。"""
    w, h = size + margin, size
    col = _rgb(color)
    a, b, c = (size * 0.28, size * 0.4), (size * 0.5, size * 0.62), (size * 0.72, size * 0.4)
    rows = []
    for j in range(h):
        row = []
        for i in range(w):
            x, y = i + 0.5, j + 0.5
            d = min(_sd_segment(x, y, *a, *b), _sd_segment(x, y, *b, *c)) - size / 14
            row.append(_over((0.0, 0.0, 0.0, 0.0), col, max(0.0, min(1.0, 0.5 - d))))
        rows.append(row)
    return _photo(w, h, rows)


def folder_icon(size: int) -> tk.PhotoImage:
    """Finder 風格的資料夾圖示。"""
    w = h = size
    tab_c, body_c = _rgb("#5AA9F5" if not P.dark else "#3B8EE0"), _rgb("#7CBCF8" if not P.dark else "#5AA2EA")
    rows = []
    for j in range(h):
        row = []
        for i in range(w):
            x, y = i + 0.5, j + 0.5
            pix = (0.0, 0.0, 0.0, 0.0)
            d_tab = _sd_round_rect(x, y, size * 0.06, size * 0.14, size * 0.5, size * 0.42, size * 0.08)
            pix = _over(pix, tab_c, max(0.0, min(1.0, 0.5 - d_tab)))
            d_back = _sd_round_rect(x, y, size * 0.06, size * 0.24, size * 0.94, size * 0.86, size * 0.1)
            pix = _over(pix, tab_c, max(0.0, min(1.0, 0.5 - d_back)))
            d_front = _sd_round_rect(x, y, size * 0.06, size * 0.34, size * 0.94, size * 0.86, size * 0.1)
            pix = _over(pix, body_c, max(0.0, min(1.0, 0.5 - d_front)))
            row.append(pix)
        rows.append(row)
    return _photo(w, h, rows)


def file_icon(size: int) -> tk.PhotoImage:
    w = h = size
    paper, edge, line = _rgb(P.card if not P.dark else "#3A3A3C"), _rgb(P.text3), _rgb(P.border)
    rows = []
    for j in range(h):
        row = []
        for i in range(w):
            x, y = i + 0.5, j + 0.5
            d = _sd_round_rect(x, y, size * 0.2, size * 0.06, size * 0.8, size * 0.94, size * 0.08)
            pix = _over((0.0, 0.0, 0.0, 0.0), edge, max(0.0, min(1.0, 0.5 - d)))
            pix = _over(pix, paper, max(0.0, min(1.0, 0.5 - (d + max(1.0, size / 16)))))
            for k in (0.38, 0.54, 0.7):
                dl = _sd_segment(x, y, size * 0.32, size * k, size * 0.68, size * k) - size / 32
                pix = _over(pix, line, max(0.0, min(1.0, 0.5 - dl)) * (d < 0))
            row.append(pix)
        rows.append(row)
    return _photo(w, h, rows)


# --------------------------------------------------------------------------- 字型
def _pick(families: set[str], *names: str) -> str | None:
    return next((n for n in names if n in families), None)


def _fonts(root: tk.Misc) -> None:
    global _icon_family
    fams = set(tkfont.families(root))
    ui = _pick(fams, "Microsoft JhengHei UI", "PingFang TC", "Noto Sans CJK TC", "Noto Sans TC",
               "WenQuanYi Zen Hei", "Segoe UI") or "TkDefaultFont"
    mono = _pick(fams, "Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono") or "TkFixedFont"
    _icon_family = _pick(fams, "Segoe Fluent Icons", "Segoe MDL2 Assets")
    spec = {
        "NSCaption": (ui, TYPE["caption"], "normal"), "NSBody": (ui, TYPE["body"], "normal"),
        "NSBodyBold": (ui, TYPE["body"], "bold"), "NSHeadline": (ui, TYPE["headline"], "bold"),
        "NSTitle": (ui, TYPE["title"], "bold"), "NSTitle2": (ui, TYPE["title2"], "bold"),
        "NSLarge": (ui, TYPE["large"], "bold"), "NSMono": (mono, TYPE["body"] - 1, "normal"),
        "NSIcon": (_icon_family or ui, TYPE["body"] + 1, "normal"),
        "NSIconLarge": (_icon_family or ui, TYPE["headline"], "normal"),
    }
    for name, (family, size, weight) in spec.items():
        try:
            tkfont.nametofont(name, root).configure(family=family, size=size, weight=weight)
        except tk.TclError:
            _font_objs[name] = tkfont.Font(root, name=name, family=family, size=size, weight=weight)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name, root).configure(family=ui, size=TYPE["body"])
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- ttk 樣式
def _element(style: ttk.Style, name: str, normal, states: list, border, sticky: str = "nsew",
             width: int = 0, height: int = 0, padding=None) -> None:
    """圖片元素。border 是不拉伸的邊緣；padding 是內容與圖片邊緣的距離（預設同 border）；
    width／height 是最小尺寸：0 表示只看內容，-1 表示用圖片本身的大小。"""
    extra = {} if padding is None else {"padding": padding}
    try:
        style.element_create(name, "image", normal, *states, border=border, sticky=sticky,
                             width=width, height=height, **extra)
    except tk.TclError:  # 已建立過（重複套用主題）
        pass


def build(root: tk.Tk, mode: str = "auto") -> Palette:
    """套用主題到整個程式；回傳使用中的配色。"""
    global P, _scale
    P = DARK if resolve_mode(mode) else LIGHT
    try:
        _scale = max(1.0, root.winfo_fpixels("1i") / 96)
    except tk.TclError:
        _scale = 1.0
    _fonts(root)
    style = ttk.Style(root)
    style.theme_use("clam")
    c = P

    # 底色與文字
    style.configure(".", background=c.card, foreground=c.text, font="NSBody", bordercolor=c.border,
                    lightcolor=c.card, darkcolor=c.card, troughcolor=c.separator, focuscolor=c.accent,
                    selectbackground=c.accent, selectforeground=c.on_accent, insertcolor=c.text,
                    fieldbackground=c.field, arrowcolor=c.text2, relief="flat")
    style.map(".", foreground=[("disabled", c.text3)])
    for name, bg in (("TFrame", c.card), ("Page.TFrame", c.bg), ("Sidebar.TFrame", c.sidebar)):
        style.configure(name, background=bg)
    labels = {
        "TLabel": (c.card, c.text, "NSBody"), "Secondary.TLabel": (c.card, c.text2, "NSBody"),
        "Caption.TLabel": (c.card, c.text2, "NSCaption"), "Section.TLabel": (c.card, c.text, "NSHeadline"),
        "Metric.TLabel": (c.card, c.text, "NSTitle2"), "Warning.TLabel": (c.card, c.warn_text, "NSBody"),
        "Success.TLabel": (c.card, c.ok_text, "NSBodyBold"), "Danger.TLabel": (c.card, c.danger_text, "NSBodyBold"),
        "Strong.TLabel": (c.card, c.text, "NSBodyBold"),
        "PageTitle.TLabel": (c.bg, c.text, "NSLarge"), "PageSubtitle.TLabel": (c.bg, c.text2, "NSBody"),
        "Status.TLabel": (c.bg, c.text2, "NSCaption"), "PageWarning.TLabel": (c.bg, c.warn_text, "NSBody"),
        "PageSection.TLabel": (c.bg, c.text2, "NSBodyBold"),
    }
    for name, (bg, fg, fnt) in labels.items():
        style.configure(name, background=bg, foreground=fg, font=fnt)

    # 卡片：白色圓角、柔和陰影
    m = px(S)
    card = round_rect(2 * (px(R_CARD) + m) + TILE, 2 * (px(R_CARD) + m) + TILE, px(R_CARD), c.card,
                      c.separator if c.dark else None, margin=m, shadow=c.shadow, shadow_blur=px(S), shadow_dy=px(2))
    _element(style, "Card.border", card, [], border=px(R_CARD) + m + 1)
    style.layout("Card.TFrame", [("background", {"sticky": "nswe", "children": [
        ("Card.border", {"sticky": "nswe"})]})])
    style.configure("Card.TFrame", background=c.bg)

    # 按鈕：一般（白底細框）、強調（系統藍）、破壞性（紅字）、無框（文字連結）、圖示
    def bezel(fill, border=None, alpha=1.0):
        side = 2 * px(R_BUTTON) + 2 * px(1) + TILE_SMALL
        return round_rect(side, side, px(R_BUTTON), fill, border, margin=px(1), fill_alpha=alpha,
                          shadow=c.shadow * 0.8, shadow_blur=px(2), shadow_dy=px(1))

    b, inset = px(R_BUTTON) + px(1) + 1, px(1) + 1  # inset：外圍陰影留白 + 1 像素框線
    plain = [bezel(c.button, c.border), bezel(c.button_hover, c.border), bezel(c.button_pressed, c.border),
             bezel(c.bg, c.separator)]
    _element(style, "Plain.Button.border", plain[0],
             [("disabled", plain[3]), ("pressed", plain[2]), ("active", plain[1])], border=b, padding=inset)
    accent = [bezel(c.accent), bezel(c.accent_hover), bezel(c.accent_pressed), bezel(c.accent, alpha=0.38)]
    _element(style, "Accent.Button.border", accent[0],
             [("disabled", accent[3]), ("pressed", accent[2]), ("active", accent[1])], border=b, padding=inset)
    clear = round_rect(2 * px(R_BUTTON) + TILE_SMALL, 2 * px(R_BUTTON) + TILE_SMALL, px(R_BUTTON), None,
                       margin=px(1))
    hover = round_rect(2 * px(R_BUTTON) + TILE_SMALL, 2 * px(R_BUTTON) + TILE_SMALL, px(R_BUTTON), c.nav_hover,
                       margin=px(1))
    _element(style, "Clear.Button.border", clear, [("pressed", hover), ("active", hover)], border=b, padding=inset)

    def button_layout(border_el):
        return [("background", {"sticky": "nswe", "children": [(border_el, {"sticky": "nswe", "children": [
            ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})]})]

    # 控制項高度一律 34（費氏數列，與表格列高相同）；大按鈕 44（Apple 建議的最小點按尺寸）
    line = tkfont.nametofont("NSBody", root).metrics("linespace")
    pad_y = max(px(XXS), (px(ROW) - 2 - line) // 2)
    pad_y_large = max(px(XS), (px(44) - 2 - line) // 2)
    pad = (px(M), pad_y)
    for name, el, fg in (("TButton", "Plain.Button.border", c.text),
                         ("Accent.TButton", "Accent.Button.border", c.on_accent),
                         ("Destructive.TButton", "Plain.Button.border", c.danger_text),
                         ("Link.TButton", "Clear.Button.border", c.accent),
                         ("Icon.TButton", "Plain.Button.border", c.text)):
        style.layout(name, button_layout(el))
        style.configure(name, background=c.card, foreground=fg, padding=pad, anchor="center", font="NSBody",
                        width=0)
    style.configure("Accent.TButton", font="NSBodyBold")
    style.configure("Large.Accent.TButton", padding=(px(L), pad_y_large), font="NSBodyBold")
    style.layout("Large.Accent.TButton", button_layout("Accent.Button.border"))
    style.configure("Large.TButton", padding=(px(L), pad_y_large))
    style.layout("Large.TButton", button_layout("Plain.Button.border"))
    # 圖示按鈕為正方形
    icon_font = tkfont.nametofont("NSIcon", root)
    icon_w = max(icon_font.measure(icon("add")), icon_font.measure(icon("refresh")))
    icon_y = max(0, (px(ROW) - 2 - icon_font.metrics("linespace")) // 2)
    style.configure("Icon.TButton", font="NSIcon", padding=(max(0, (px(ROW) - 2 - icon_w) // 2), icon_y))
    style.configure("Link.TButton", padding=(px(S), pad_y))
    style.map("Accent.TButton", foreground=[("disabled", c.on_accent)])
    style.map("Large.Accent.TButton", foreground=[("disabled", c.on_accent)])
    style.map("TButton", foreground=[("disabled", c.text3)],
              # 圓角外的四個角永遠是卡片底色（clam 預設在停用／按下時改成灰色）
              background=[("disabled", c.card), ("pressed", c.card), ("active", c.card)])

    # 輸入框與下拉選單：圓角、聚焦時藍色光暈
    side = 2 * (px(R_FIELD) + px(3)) + TILE_SMALL

    def field(fill, border, ring=None):
        return round_rect(side, side, px(R_FIELD), fill, border, margin=px(3), ring=ring, ring_width=px(3))

    f_norm, f_focus = field(c.field, c.border), field(c.field, c.accent, ring=c.accent)
    f_dis = field(c.bg, c.separator)
    f_ro, f_ro_hover = field(c.button, c.border), field(c.button_hover, c.border)
    _element(style, "Rounded.field", f_norm, [("disabled", f_dis), ("focus", f_focus)],
             border=px(R_FIELD) + px(3) + 1, padding=px(3) + 1)
    _element(style, "Popup.field", f_ro, [("disabled", f_dis), ("focus", f_focus), ("active", f_ro_hover)],
             border=px(R_FIELD) + px(3) + 1, padding=px(3) + 1)
    style.layout("TEntry", [("background", {"sticky": "nswe", "children": [("Rounded.field", {
        "sticky": "nswe", "children": [("Entry.padding", {"sticky": "nswe", "children": [
            ("Entry.textarea", {"sticky": "nswe"})]})]})]})])
    style.configure("TEntry", background=c.card, padding=(px(S), pad_y), foreground=c.text)
    arrow, arrow_dis = chevron(px(M), c.text2, px(S)), chevron(px(M), c.text3, px(S))
    _element(style, "Chevron.downarrow", arrow, [("disabled", arrow_dis)], border=0, sticky="", width=-1, height=-1)
    for name, el in (("TCombobox", "Rounded.field"), ("Popup.TCombobox", "Popup.field")):
        style.layout(name, [("background", {"sticky": "nswe", "children": [(el, {"sticky": "nswe", "children": [
            ("Chevron.downarrow", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})]})])
        style.configure(name, background=c.card, padding=(px(S), pad_y, px(XXS), pad_y),
                        arrowsize=px(M), foreground=c.text, selectbackground=c.field, selectforeground=c.text)
        style.map(name, fieldbackground=[("readonly", c.field)], selectbackground=[("readonly", c.field)],
                  selectforeground=[("readonly", c.text)], foreground=[("disabled", c.text3)])
    root.option_add("*TCombobox*Listbox.background", c.card)
    root.option_add("*TCombobox*Listbox.foreground", c.text)
    root.option_add("*TCombobox*Listbox.selectBackground", c.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", c.on_accent)
    root.option_add("*TCombobox*Listbox.font", "NSBody")
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    # 勾選框
    box, gap = px(16), px(S)
    cb = [checkbox(box, False, gap=gap), checkbox(box, True, gap=gap), checkbox(box, False, True, gap),
          checkbox(box, True, True, gap)]
    _element(style, "Rounded.indicator", cb[0], [("disabled selected", cb[3]), ("disabled", cb[2]),
                                                 ("selected", cb[1])], border=0, sticky="",
             width=-1, height=-1)
    style.layout("TCheckbutton", [("background", {"sticky": "nswe", "children": [("Checkbutton.padding", {
        "sticky": "nswe", "children": [("Rounded.indicator", {"side": "left", "sticky": ""}),
                                       ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})]})])
    style.configure("TCheckbutton", background=c.card, padding=(0, px(XXS)))
    style.map("TCheckbutton", background=[("active", c.card)])

    # 清單（Finder 風格）：無框、列高 34、選取為系統藍
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Treeview", background=c.card, fieldbackground=c.card, foreground=c.text, rowheight=px(ROW),
                    borderwidth=0, font="NSBody", indent=px(M))
    style.map("Treeview", background=[("selected", c.accent)], foreground=[("selected", c.on_accent)])
    style.configure("Treeview.Heading", background=c.card, foreground=c.text2, font="NSCaption", relief="flat",
                    borderwidth=0, padding=(px(S), px(XS)), lightcolor=c.card, darkcolor=c.card, bordercolor=c.card)
    style.map("Treeview.Heading", background=[("active", c.card), ("pressed", c.card)],
              foreground=[("active", c.text)])

    # 細捲軸（圓角滑塊、無箭頭）
    thumb_w = px(S)
    tl = 2 * thumb_w + TILE_SMALL
    thumb = round_rect(thumb_w + 2, tl, thumb_w // 2, c.text3, margin=px(1), fill_alpha=0.55)
    thumb_h = round_rect(thumb_w + 2, tl, thumb_w // 2, c.text2, margin=px(1), fill_alpha=0.7)
    thumb_x = round_rect(tl, thumb_w + 2, thumb_w // 2, c.text3, margin=px(1), fill_alpha=0.55)
    thumb_xh = round_rect(tl, thumb_w + 2, thumb_w // 2, c.text2, margin=px(1), fill_alpha=0.7)
    # 切片的中間區域必須大於 0，否則 Tk 繪製時會無限迴圈
    _element(style, "Thin.Vertical.Scrollbar.thumb", thumb, [("active", thumb_h), ("pressed", thumb_h)],
             border=(0, thumb_w // 2 + 1, 0, thumb_w // 2 + 1), width=-1)
    _element(style, "Thin.Horizontal.Scrollbar.thumb", thumb_x, [("active", thumb_xh), ("pressed", thumb_xh)],
             border=(thumb_w // 2 + 1, 0, thumb_w // 2 + 1, 0), height=-1)
    style.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
        ("Thin.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.layout("Horizontal.TScrollbar", [("Horizontal.Scrollbar.trough", {"sticky": "we", "children": [
        ("Thin.Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("Vertical.TScrollbar", troughcolor=c.card, background=c.card, borderwidth=0,
                    width=thumb_w + 2, arrowsize=thumb_w + 2)
    style.configure("Horizontal.TScrollbar", troughcolor=c.card, background=c.card, borderwidth=0,
                    width=thumb_w + 2, arrowsize=thumb_w + 2)
    for name, trough in (("Vertical.TScrollbar", c.card), ("Horizontal.TScrollbar", c.card),
                         ("Page.Vertical.TScrollbar", c.bg)):  # 捲軸軌道與所在底色相同、不畫框線
        style.configure(name, troughcolor=trough, background=trough, bordercolor=trough, lightcolor=trough,
                        darkcolor=trough, gripcount=0)
    style.layout("Page.Vertical.TScrollbar", style.layout("Vertical.TScrollbar"))
    # 內容不需捲動時只畫軌道（與 macOS 一樣隱藏捲軸），寬度不變所以版面不會跳動
    for name in ("Idle.Vertical.TScrollbar", "Idle.Page.Vertical.TScrollbar"):
        style.layout(name, [("Vertical.Scrollbar.trough", {"sticky": "ns"})])

    # 進度條：細長圓角
    h = px(6)
    trough = round_rect(2 * h + TILE, h, h // 2, c.separator)
    bar = round_rect(2 * h + TILE, h, h // 2, c.accent)
    _element(style, "Rounded.Progressbar.trough", trough, [], border=(h // 2 + 1, 0, h // 2 + 1, 0), height=-1)
    _element(style, "Rounded.Progressbar.pbar", bar, [], border=(h // 2 + 1, 0, h // 2 + 1, 0), height=-1)
    style.layout("Horizontal.TProgressbar", [("Rounded.Progressbar.trough", {"sticky": "nswe", "children": [
        ("Rounded.Progressbar.pbar", {"side": "left", "sticky": "ns"})]})])
    style.configure("Horizontal.TProgressbar", thickness=h, background=c.card)

    # 其他
    style.configure("TPanedwindow", background=c.card)
    style.configure("Sash", sashthickness=px(M), gripcount=0, background=c.card, lightcolor=c.card,
                    darkcolor=c.card, bordercolor=c.card)
    style.layout("Hidden.TNotebook.Tab", [])
    style.configure("Hidden.TNotebook", background=c.bg, borderwidth=0, padding=0, tabmargins=0)
    style.layout("Hidden.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
    # 分組框（對話框內）：淡色細框的圓角分組，標題放在框外上方（類似 macOS 系統設定的分組）
    group = round_rect(2 * px(R_CARD) + TILE, 2 * px(R_CARD) + TILE, px(R_CARD), c.card, c.separator)
    _element(style, "Group.border", group, [], border=px(R_CARD) + 1, padding=1)
    style.layout("TLabelframe", [("Group.border", {"sticky": "nswe"})])
    style.configure("TLabelframe", background=c.card, labeloutside=True, labelmargins=(0, 0, 0, px(XS)))
    style.configure("TLabelframe.Label", background=c.card, foreground=c.text2, font="NSBodyBold")
    style.configure("TSeparator", background=c.separator)

    for opt, val in (("*Text.background", c.card), ("*Text.foreground", c.text), ("*Text.insertBackground", c.text),
                     ("*Text.selectBackground", c.accent), ("*Text.selectForeground", c.on_accent),
                     ("*Text.font", "NSBody"), ("*Text.relief", "flat"), ("*Text.borderWidth", 0),
                     ("*Text.highlightThickness", 0), ("*Toplevel.background", c.card),
                     ("*Canvas.highlightThickness", 0), ("*Menu.background", c.card),
                     ("*Menu.foreground", c.text), ("*Menu.activeBackground", c.accent),
                     ("*Menu.activeForeground", c.on_accent), ("*Menu.font", "NSBody")):
        root.option_add(opt, val)
    root.configure(background=c.bg)
    return P


def separator(parent) -> tk.Frame:
    """1 像素的淺色分隔線（ttk.Separator 在 clam 主題會畫成深灰色的凹線）。"""
    return tk.Frame(parent, height=1, background=P.separator, borderwidth=0, highlightthickness=0)


def card(parent, padding: int = L, **kw) -> ttk.Frame:
    """白色圓角卡片（含柔和陰影）；padding 是卡片內容與邊緣的距離（邏輯像素）。"""
    return ttk.Frame(parent, style="Card.TFrame", padding=px(S) + px(padding), **kw)  # px(S)：陰影留白
