"""介面圖示：全部以程式繪製（向量圖形 → 反鋸齒點陣圖），不使用任何外部圖檔或字型。

每個圖示以 24×24 的設計座標描述（線段、折線、圓、圓弧、圓角矩形、多邊形），依螢幕縮放比例即時算成帶透明度的
點陣圖，所以在 100%～300% 縮放下都一樣銳利；淺色／深色主題各有一組顏色。
"""
from __future__ import annotations

import base64
import math
import struct
import zlib

GRID = 24.0

# 顏色代號 → (淺色主題, 深色主題)
COLORS = {
    "a": ("#1787CF", "#4DB2F2"),   # 圖示主色（藍色線條）
    "l": ("#D3EAFA", "#173F5C"),   # 淺藍填色
    "t": ("#404040", "#E4E4E4"),   # 深色線條
    "g": ("#8C8C8C", "#9C9C9C"),   # 灰
    "w": ("#FFFFFF", "#2D2D2D"),   # 紙張
    "y": ("#FFD969", "#E9BE4C"),   # 資料夾
    "y2": ("#E7B43C", "#B98A22"),  # 資料夾深色邊
    "gr": ("#2BA84A", "#45C767"),  # 綠（完成、下載）
    "r": ("#D93025", "#F2665C"),   # 紅（刪除、錯誤）
    "o": ("#EE8A17", "#F5A742"),   # 橘（待核對、警告）
    "p": ("#8457C7", "#A988E0"),   # 紫
    "k": ("#000000", "#FFFFFF"),   # 純黑／白
    "W": ("#FFFFFF", "#FFFFFF"),   # 一律白色（放在色塊上的符號）
}


# --------------------------------------------------------------------------- 圖形描述
def L(x1, y1, x2, y2, w=1.8, c="a"):
    return ("PL", [(x1, y1), (x2, y2)], w, c, False)


def PL(pts, w=1.8, c="a", closed=False):
    return ("PL", list(pts), w, c, closed)


def P(pts, c="a"):
    return ("P", list(pts), c)


def C(cx, cy, r, c="a"):
    return ("C", (cx, cy, r), c)


def O(cx, cy, r, w=1.8, c="a"):  # noqa: E743 - 圓環
    return ("O", (cx, cy, r), w, c)


def R(x0, y0, x1, y1, rad=0.0, c="a"):
    return ("R", (x0, y0, x1, y1, rad), c)


def B(x0, y0, x1, y1, rad=0.0, w=1.8, c="a"):
    return ("B", (x0, y0, x1, y1, rad), w, c)


def A(cx, cy, r, a0, a1, w=1.8, c="a"):
    """圓弧：角度以度計，0° 在右邊、順時針增加（螢幕座標 y 向下）。"""
    return ("A", (cx, cy, r, a0, a1), w, c)


def U(shapes, fill=None, stroke=None, w=1.8):
    """多個圓／圓角矩形的聯集（例如雲朵），可填色與描邊。shapes：("C", cx, cy, r) 或 ("R", x0, y0, x1, y1, rad)。"""
    return ("U", list(shapes), fill, stroke, w)


def S(cx, cy, r, a0, a1, c="a"):
    """扇形（圓餅圖）。"""
    return ("S", (cx, cy, r, a0, a1), c)


def clip(prim, x0, y0, x1, y1):
    return ("CLIP", (x0, y0, x1, y1), prim)


def head(tx, ty, angle, size=4.6, w=1.8, c="a"):
    """箭頭的頭（兩條線）：尖端在 (tx, ty)，朝 angle（度）方向。"""
    a = math.radians(angle)
    out = []
    for side in (150, -150):
        b = a + math.radians(side)
        out.append((tx + size * math.cos(b), ty + size * math.sin(b)))
    return PL([out[0], (tx, ty), out[1]], w, c)


def tri(tx, ty, angle, size, c="a"):
    """實心三角形箭頭：尖端在 (tx, ty)，朝 angle 方向。"""
    a = math.radians(angle)
    pts = [(tx, ty)]
    for side in (150, -150):
        b = a + math.radians(side)
        pts.append((tx + size * math.cos(b), ty + size * math.sin(b)))
    return P(pts, c)


def arrow(x1, y1, x2, y2, w=1.8, c="a", size=4.6):
    ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
    return [L(x1, y1, x2, y2, w, c), head(x2, y2, ang, size, w, c)]


def _flat(items):
    out = []
    for it in items:
        if isinstance(it, list):
            out.extend(_flat(it))
        else:
            out.append(it)
    return out


def _star(cx, cy, r_out, r_in):
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(-90 + i * 36)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _gear(c="a", w=1.8):
    out = [O(12, 12, 6.0, w, c), O(12, 12, 2.4, w * 0.9, c)]
    for i in range(8):
        a = math.radians(i * 45 + 22.5)
        out.append(L(12 + 6.6 * math.cos(a), 12 + 6.6 * math.sin(a), 12 + 9.3 * math.cos(a), 12 + 9.3 * math.sin(a),
                     w * 1.5, c))
    return out


def _page(fill="w", edge="g", w=1.5):
    """一張紙（右上角折角）。"""
    outline = [(6, 2.8), (14, 2.8), (19, 7.8), (19, 21.2), (6, 21.2)]
    return [P(outline, fill), PL(outline, w, edge, True), PL([(14, 2.8), (14, 7.8), (19, 7.8)], w, edge)]


def _folder(body="y", tab="y2"):
    return [R(2.5, 4.5, 11, 10, 1.6, tab), R(2.5, 6.8, 21.5, 20, 1.8, tab), R(2.5, 8.6, 21.5, 20, 1.8, body)]


def _typed(color, extra=()):
    """檔案類型圖示：紙張 + 一條色帶。"""
    return _page() + [R(3.5, 12.2, 15.5, 18.2, 1.2, color)] + list(extra)


def _cloud(fill="a", stroke=None):
    return U([("C", 8.2, 14.5, 4.6), ("C", 12.8, 10.6, 5.6), ("C", 17.4, 14.8, 4.0), ("R", 8.2, 13.5, 17.4, 19.1, 1)],
             fill, stroke, 1.6)


def _monitor(c="a", w=1.8):
    return [B(3, 4.5, 21, 16.5, 1.6, w, c), L(12, 16.5, 12, 19.6, w, c), L(7.5, 20.2, 16.5, 20.2, w, c)]


def _person(c="a", w=1.8):
    return [O(12, 8, 3.7, w, c), clip(B(4.8, 14.2, 19.2, 27, 7, w, c), 0, 0, 24, 20.6)]


def _check(c="gr", w=2.4, dx=0.0, dy=0.0, k=1.0):
    return PL([(5.5 * k + dx, 12.5 * k + dy), (10 * k + dx, 17 * k + dy), (18.5 * k + dx, 7.5 * k + dy)], w, c)


def _sync(c="a", w=1.9, r=7.3):
    out = [A(12, 12, r, 200, 345, w, c), A(12, 12, r, 20, 165, w, c)]
    for ang in (345, 165):  # 箭頭放在弧的尾端，沿切線方向
        a = math.radians(ang)
        out.append(tri(12 + r * math.cos(a) + 1.6 * math.cos(a + math.pi / 2),
                       12 + r * math.sin(a) + 1.6 * math.sin(a + math.pi / 2), ang + 90, 5.4, c))
    return out


def _qmark(c="W", w=1.9, cx=12.0, cy=12.0, k=1.0):
    pts = [(-2.8, -2.6), (-2.3, -4.2), (-1.0, -5.2), (0.4, -5.4), (1.9, -4.9), (2.8, -3.6), (2.6, -2.0),
           (1.4, -0.8), (0.3, 0.2), (0.0, 1.9)]
    return [PL([(cx + x * k, cy + y * k) for x, y in pts], w, c), C(cx, cy + 5.0 * k, 1.25 * k, c)]


ICONS: dict[str, list] = {
    # ---------------------------------------------------------------- 主工具列（32px）
    "explore": [R(3, 4, 21, 20, 2, "l"), B(3, 4, 21, 20, 2, 1.7), L(3.5, 8.5, 20.5, 8.5, 1.5), L(12, 8.5, 12, 20, 1.5),
                R(5.2, 11, 9.8, 12.6, 0.5), R(5.2, 14.2, 9.8, 15.8, 0.5), R(14.2, 11, 18.8, 12.6, 0.5)],
    "accounts": _person() + [_cloud("l", "a")] if False else [O(10, 7.6, 3.6, 1.8), clip(B(3, 13.6, 17, 26, 7, 1.8), 0, 0, 24, 20.4),
                                                              U([("C", 15.6, 17.2, 2.6), ("C", 18.4, 15.2, 3.0),
                                                                 ("C", 20.6, 17.6, 2.2), ("R", 15.6, 16.6, 20.6, 19.8, 0.6)],
                                                                "w", "a", 1.5)],
    "search": [C(10.5, 10.5, 6.4, "l"), O(10.5, 10.5, 6.4, 1.9), L(15.4, 15.4, 20.6, 20.6, 2.6)],
    "sync": _sync(),
    "scheduler": [R(3.5, 5, 20.5, 20.8, 2, "l"), B(3.5, 5, 20.5, 20.8, 2, 1.7), L(3.8, 9.6, 20.2, 9.6, 1.5),
                  L(8, 3, 8, 6.6, 2.0), L(16, 3, 16, 6.6, 2.0), PL([(12, 12), (12, 15.4), (14.6, 16.8)], 1.6)],
    "options": [C(12, 12, 6.0, "l")] + _gear(),
    "view": [R(3, 4.5, 21, 16.5, 1.6, "l")] + _monitor(),
    "reviews": [R(5, 4.6, 19, 21, 2, "w"), B(5, 4.6, 19, 21, 2, 1.7), R(9, 3, 15, 6.6, 1.2), _check("o", 2.2, 0.4, 2.6, 0.92)],
    "tools": [R(3, 8, 21, 20, 2, "l"), B(3, 8, 21, 20, 2, 1.7), PL([(9, 8), (9, 5.2), (15, 5.2), (15, 8)], 1.7),
              L(3.5, 13.5, 20.5, 13.5, 1.5), R(10.6, 12, 13.4, 15.2, 0.6)],
    "help": [C(12, 12, 9.6)] + _qmark(),
    # ---------------------------------------------------------------- 導覽
    "back": arrow(19, 12, 5, 12, 1.9, "t", 5.6),
    "forward": arrow(5, 12, 19, 12, 1.9, "t", 5.6),
    "up": arrow(12, 19, 12, 5, 1.9, "t", 5.6),
    "refresh": [A(12, 12, 7.0, -50, 250, 1.9, "t"), tri(12 + 7.0 * math.cos(math.radians(-50)) + 1.4,
                                                       12 + 7.0 * math.sin(math.radians(-50)) - 2.6, 60, 5.6, "t")],
    "chev_right": [PL([(9, 6.5), (14.5, 12), (9, 17.5)], 1.6, "g")],
    "chev_left": [PL([(15, 6.5), (9.5, 12), (15, 17.5)], 1.6, "g")],
    "chev_down": [PL([(6.5, 9.5), (12, 15), (17.5, 9.5)], 1.7, "t")],
    "chev_more": [PL([(6, 6.5), (11, 12), (6, 17.5)], 1.6, "g"), PL([(12.5, 6.5), (17.5, 12), (12.5, 17.5)], 1.6, "g")],
    "expand": [PL([(9.5, 7), (14.5, 12), (9.5, 17)], 1.5, "g")],
    "collapse": [PL([(7, 9.5), (12, 14.5), (17, 9.5)], 1.5, "t")],
    "sort_up": [P([(12, 8), (17, 15), (7, 15)], "g")],
    "sort_down": [P([(12, 16), (17, 9), (7, 9)], "g")],
    "menu_down": [P([(12, 15.5), (16.5, 10), (7.5, 10)], "t")],
    "star": [PL(_star(12, 12.6, 8.6, 3.6), 1.5, "t", True)],
    "star_on": [P(_star(12, 12.6, 8.6, 3.6), "y"), PL(_star(12, 12.6, 8.6, 3.6), 1.4, "y2", True)],
    "close": [L(6.5, 6.5, 17.5, 17.5, 1.7, "t"), L(17.5, 6.5, 6.5, 17.5, 1.7, "t")],
    "plus": [L(12, 4.5, 12, 19.5, 2.0), L(4.5, 12, 19.5, 12, 2.0)],
    "tri_right": [PL([(8, 4.5), (18, 12), (8, 19.5)], 1.7, "a", True)],
    "tri_left": [PL([(16, 4.5), (6, 12), (16, 19.5)], 1.7, "a", True)],
    "link": [A(8.2, 15.8, 4.6, 45, 315, 1.9, "g"), A(15.8, 8.2, 4.6, 225, 495, 1.9, "g"), L(9.6, 14.4, 14.4, 9.6, 1.9, "g")],
    "link_on": [A(8.2, 15.8, 4.6, 45, 315, 2.0), A(15.8, 8.2, 4.6, 225, 495, 2.0), L(9.6, 14.4, 14.4, 9.6, 2.0)],
    "sync_small": _sync("a", 2.0, 7.0),
    # ---------------------------------------------------------------- 面板工具列
    "tree": [B(3, 3.5, 10.5, 8.5, 0.8, 1.6), PL([(6.6, 8.5), (6.6, 18.6), (12.5, 18.6)], 1.5),
             L(6.6, 12.4, 12.5, 12.4, 1.5), B(12.5, 10, 21, 14.6, 0.8, 1.6), B(12.5, 16.2, 21, 20.8, 0.8, 1.6)],
    "newfolder": _folder() + [C(17.6, 17.2, 5.0, "w"), L(17.6, 14, 17.6, 20.4, 2.0, "gr"), L(14.4, 17.2, 20.8, 17.2, 2.0, "gr")],
    "eye": [PL([(2.5, 12), (5.5, 8.4), (9, 6.4), (12, 5.8), (15, 6.4), (18.5, 8.4), (21.5, 12), (18.5, 15.6), (15, 17.6),
                (12, 18.2), (9, 17.6), (5.5, 15.6)], 1.6, "t", True), C(12, 12, 3.3, "a")],
    "compare": arrow(4, 8.5, 20, 8.5, 1.9, "a", 4.4) + arrow(20, 15.5, 4, 15.5, 1.9, "gr", 4.4),
    "bolt": [P([(13.6, 2.2), (5.2, 13.6), (10.9, 13.6), (9.4, 21.8), (18.8, 9.8), (12.9, 9.8)], "o")],
    "list": [C(5, 6.5, 1.5, "t"), C(5, 12, 1.5, "t"), C(5, 17.5, 1.5, "t"), L(9, 6.5, 20, 6.5, 1.7, "t"),
             L(9, 12, 20, 12, 1.7, "t"), L(9, 17.5, 20, 17.5, 1.7, "t")],
    "conflict": _page() + [C(16.6, 16.8, 5.4, "o")] + [L(16.6, 13.6, 16.6, 17.6, 1.9, "W"), C(16.6, 20, 1.1, "W")],
    "filter": [PL([(4, 5), (20, 5), (14, 12.6), (14, 19), (10, 21), (10, 12.6)], 1.7, "t", True)],
    # ---------------------------------------------------------------- 位置與檔案
    "computer": [R(3, 4.5, 21, 16.5, 1.6, "l")] + _monitor(),
    "cloud": [_cloud("a")],
    "cloud_off": [_cloud("w", "g")],
    "drive": [R(3, 8, 21, 17.5, 2, "l"), B(3, 8, 21, 17.5, 2, 1.6, "g"), C(17.2, 12.8, 1.3, "gr"), L(6, 12.8, 12, 12.8, 1.6, "g")],
    "nas": [R(4.5, 3, 19.5, 21, 2, "l"), B(4.5, 3, 19.5, 21, 2, 1.6), L(7.5, 7.5, 16.5, 7.5, 1.6), L(7.5, 11.5, 16.5, 11.5, 1.6),
            C(15.2, 17, 1.3, "gr"), C(11.4, 17, 1.3, "a")],
    "folder": _folder(),
    "folder_link": _folder() + [C(17.4, 16.6, 4.8, "w")] + arrow(14.6, 19.2, 19.6, 14.2, 1.7, "a", 3.6),
    "file": _page(),
    "file_image": _typed("gr"),
    "file_video": _typed("p"),
    "file_audio": _typed("o"),
    "file_archive": _typed("y2"),
    "file_doc": _typed("a"),
    "file_pdf": _typed("r"),
    "file_exe": _typed("g"),
    "file_save": _typed("gr", [C(15.4, 15.2, 0, "gr")]),
    # ---------------------------------------------------------------- 操作
    "open": [PL([(11, 5.5), (5, 5.5), (5, 19), (18.5, 19), (18.5, 13)], 1.7, "t")] + arrow(11.5, 12.5, 19.5, 4.5, 1.8, "a", 4.6),
    "cut": [O(7, 17.4, 2.6, 1.6, "t"), O(17, 17.4, 2.6, 1.6, "t"), L(8.6, 15.4, 16.8, 4.5, 1.7, "t"), L(15.4, 15.4, 7.2, 4.5, 1.7, "t")],
    "copy": [PL([(15.5, 7.5), (15.5, 4), (4.5, 4), (4.5, 16.5), (8, 16.5)], 1.6, "t"), R(8.5, 8, 19.5, 20.5, 1.4, "w"),
             B(8.5, 8, 19.5, 20.5, 1.4, 1.6)],
    "paste": [R(5, 5, 19, 21, 2, "w"), B(5, 5, 19, 21, 2, 1.6, "t"), R(9, 3.2, 15, 7, 1.2), L(8.5, 12, 15.5, 12, 1.5, "g"),
              L(8.5, 16, 15.5, 16, 1.5, "g")],
    "rename": [PL([(4.8, 19.2), (5.2, 15.4), (15.4, 5.2), (18.8, 8.6), (8.6, 18.8)], 1.6, "a", True), L(13.2, 7.4, 16.6, 10.8, 1.5)],
    "delete": [L(4.5, 7, 19.5, 7, 1.7, "r"), PL([(9.4, 7), (9.4, 4.4), (14.6, 4.4), (14.6, 7)], 1.6, "r"),
               PL([(6.4, 7), (7.4, 20.6), (16.6, 20.6), (17.6, 7)], 1.7, "r"), L(10.2, 10.4, 10.2, 17.2, 1.5, "r"),
               L(13.8, 10.4, 13.8, 17.2, 1.5, "r")],
    "info": [O(12, 12, 9, 1.7), C(12, 7.6, 1.3), L(12, 11, 12, 17, 2.1)],
    "globe": [O(12, 12, 9, 1.6), L(3.2, 12, 20.8, 12, 1.4), PL([(12, 3), (8.6, 7), (8, 12), (8.6, 17), (12, 21)], 1.4),
              PL([(12, 3), (15.4, 7), (16, 12), (15.4, 17), (12, 21)], 1.4)],
    "move": _folder() + [C(17.4, 16.6, 4.8, "w")] + arrow(13.6, 16.6, 20.6, 16.6, 1.8, "a", 3.4),
    "upload": arrow(12, 16.5, 12, 4, 2.0, "a", 5.6) + [L(4.5, 20.2, 19.5, 20.2, 2.0)],
    "download": arrow(12, 4, 12, 16.5, 2.0, "gr", 5.6) + [L(4.5, 20.2, 19.5, 20.2, 2.0, "gr")],
    "export": [PL([(8, 9.5), (4.5, 9.5), (4.5, 20), (16.5, 20), (16.5, 16.5)], 1.7, "t")] + arrow(9, 12.5, 20, 12.5, 1.8, "a", 4.4),
    "import": [PL([(13, 5), (20, 5), (20, 19.5), (13, 19.5)], 1.7, "t")] + arrow(3.5, 12.2, 14, 12.2, 1.8, "a", 4.4),
    "save": [B(4, 4, 20, 20, 1.6, 1.7), R(8, 4, 15.5, 8.6, 0.4), B(7.4, 12.6, 16.6, 20, 0.6, 1.5)],
    "start": [C(12, 12, 9.2, "gr"), P([(9.6, 7.4), (17.2, 12), (9.6, 16.6)], "W")],
    "play": [P([(7, 4.5), (19.5, 12), (7, 19.5)], "gr")],
    "pause": [R(6, 5, 10.4, 19, 1), R(13.6, 5, 18, 19, 1)],
    "stop": [R(5.5, 5.5, 18.5, 18.5, 1.6, "r")],
    "broom": [L(18.5, 3.5, 11.5, 12.5, 2.0, "t"), P([(8.4, 10.6), (14.6, 14.6), (11, 21.4), (3.4, 18)], "o")],
    "wrench": [L(4.8, 19.2, 12.6, 11.4, 3.2, "g"), C(15.6, 8.4, 5.2, "g"), P([(15.2, 8.8), (17.6, 2.6), (21.6, 6.6)], "w"),
               C(4.8, 19.2, 0.9, "w")],
    "connect": [O(12, 12, 9, 1.6)] + arrow(6.8, 12, 16.6, 12, 1.8, "a", 4.2),
    "logout": [PL([(13, 4.5), (5, 4.5), (5, 19.5), (13, 19.5)], 1.7, "t")] + arrow(10, 12, 20.4, 12, 1.8, "r", 4.4),
    "qr": [B(3.6, 3.6, 10.2, 10.2, 0.6, 1.6, "t"), B(13.8, 3.6, 20.4, 10.2, 0.6, 1.6, "t"), B(3.6, 13.8, 10.2, 20.4, 0.6, 1.6, "t"),
           R(5.8, 5.8, 8, 8, 0, "t"), R(16, 5.8, 18.2, 8, 0, "t"), R(5.8, 16, 8, 18.2, 0, "t"), R(13.6, 13.6, 16.4, 16.4, 0, "t"),
           R(18, 18, 20.6, 20.6, 0, "t"), R(18, 13.6, 20.6, 15.4, 0, "t"), R(13.6, 18.6, 15.4, 20.6, 0, "t")],
    "shield": [PL([(12, 2.8), (19.6, 5.8), (19.6, 11.6), (17.4, 16.6), (12, 21.2), (6.6, 16.6), (4.4, 11.6), (4.4, 5.8)], 1.7, "a", True),
               _check("gr", 2.1, 2.2, 2.0, 0.82)],
    "user": _person(),
    "gamepad": [R(2.5, 7.5, 21.5, 18.5, 5.4, "l"), B(2.5, 7.5, 21.5, 18.5, 5.4, 1.6), L(6.2, 13, 10.6, 13, 1.7), L(8.4, 10.8, 8.4, 15.2, 1.7),
                C(15.4, 14.4, 1.25, "r"), C(18, 11.6, 1.25, "gr")],
    "duplicates": [B(8.6, 3.6, 19.4, 15.6, 1.4, 1.6, "o"), R(4.6, 8.4, 15.4, 20.4, 1.4, "w"), B(4.6, 8.4, 15.4, 20.4, 1.4, 1.6, "o"),
                   L(7.4, 12.6, 12.6, 12.6, 1.4, "o"), L(7.4, 16.2, 12.6, 16.2, 1.4, "o")],
    "log": _page() + [L(9, 11, 16, 11, 1.4), L(9, 14.2, 16, 14.2, 1.4), L(9, 17.4, 13.6, 17.4, 1.4)],
    "calendar": [B(3.5, 5, 20.5, 20.8, 2, 1.6), L(3.8, 9.6, 20.2, 9.6, 1.5), L(8, 3, 8, 6.6, 1.9), L(16, 3, 16, 6.6, 1.9),
                 R(7, 12.4, 9.6, 14.8, 0.4), R(10.8, 12.4, 13.4, 14.8, 0.4), R(14.6, 12.4, 17.2, 14.8, 0.4), R(7, 16.2, 9.6, 18.6, 0.4)],
    "clock": [O(12, 12, 9, 1.7), PL([(12, 6.8), (12, 12.2), (15.6, 14.4)], 1.8)],
    "mail": [B(3, 5.5, 21, 18.5, 1.6, 1.6), PL([(3.6, 6.6), (12, 13), (20.4, 6.6)], 1.6)],
    "proxy": [B(3, 4, 13, 10.5, 1, 1.6), B(11, 13.5, 21, 20, 1, 1.6), PL([(8, 10.5), (8, 16.8), (11, 16.8)], 1.6)],
    # ---------------------------------------------------------------- 狀態
    "ok": [_check("gr", 2.5)],
    "ok_circle": [C(12, 12, 9.4, "gr"), _check("W", 2.3, 1.9, 1.9, 0.84)],
    "running": [C(12, 12, 6.2, "gr")],
    "queued": [O(12, 12, 6.2, 1.8, "g")],
    "paused": [R(7, 6, 10.6, 18, 0.8, "o"), R(13.4, 6, 17, 18, 0.8, "o")],
    "error": [C(12, 12, 9.4, "r"), L(8.4, 8.4, 15.6, 15.6, 2.2, "W"), L(15.6, 8.4, 8.4, 15.6, 2.2, "W")],
    "warning": [P([(12, 2.6), (22, 20.4), (2, 20.4)], "o"), L(12, 9, 12, 14.6, 2.0, "W"), C(12, 17.6, 1.2, "W")],
    "skipped": [O(12, 12, 8.4, 1.8, "g"), L(6.2, 17.8, 17.8, 6.2, 1.8, "g")],
    "question": [C(12, 12, 9.6, "o")] + _qmark("W"),
    "dot_on": [C(12, 12, 5.4, "gr")],
    "dot_off": [C(12, 12, 5.4, "g")],
    # ---------------------------------------------------------------- 同步比對的動作
    "to_right": arrow(3.5, 12, 20, 12, 2.2, "a", 5.4),
    "to_left": arrow(20.5, 12, 4, 12, 2.2, "gr", 5.4),
    "equal": [L(5.5, 9, 18.5, 9, 2.2, "g"), L(5.5, 15, 18.5, 15, 2.2, "g")],
    "noteq": [L(5.5, 9, 18.5, 9, 2.2, "o"), L(5.5, 15, 18.5, 15, 2.2, "o"), L(15.8, 4.6, 8.2, 19.4, 2.0, "o")],
    "del_right": arrow(3.5, 12, 12.5, 12, 1.9, "r", 4.0) + [L(15.4, 8.2, 21, 15.8, 2.0, "r"), L(21, 8.2, 15.4, 15.8, 2.0, "r")],
    "del_left": arrow(20.5, 12, 11.5, 12, 1.9, "r", 4.0) + [L(3, 8.2, 8.6, 15.8, 2.0, "r"), L(8.6, 8.2, 3, 15.8, 2.0, "r")],
    "only_left": [C(6.5, 12, 3.6, "g"), L(13, 12, 20, 12, 1.6, "g")],
    "only_right": [C(17.5, 12, 3.6, "g"), L(4, 12, 11, 12, 1.6, "g")],
    # ---------------------------------------------------------------- 勾選框與單選
    "check_off": [R(4.5, 4.5, 19.5, 19.5, 1.4, "w"), B(4.5, 4.5, 19.5, 19.5, 1.4, 1.4, "t")],
    "check_on": [R(4.5, 4.5, 19.5, 19.5, 1.4, "a"), _check("W", 2.3, 0.0, 0.0, 1.0)],
    "radio_off": [C(12, 12, 7.6, "w"), O(12, 12, 7.6, 1.4, "t")],
    "radio_on": [C(12, 12, 7.6, "w"), O(12, 12, 7.6, 1.6), C(12, 12, 3.8)],
}
ICONS = {k: _flat(v) for k, v in ICONS.items()}

# 副檔名 → 檔案圖示
_EXT_ICONS = {
    "file_image": "jpg jpeg png gif bmp webp heic heif tif tiff raw cr2 nef arw dng svg ico psd",
    "file_video": "mp4 mkv avi mov wmv flv webm m4v ts m2ts mpg mpeg rmvb rm 3gp vob iso",
    "file_audio": "mp3 flac wav aac m4a ogg wma ape opus aiff dsf",
    "file_archive": "zip rar 7z tar gz bz2 xz tgz cab zst",
    "file_doc": "doc docx xls xlsx ppt pptx txt md rtf csv odt ods odp epub mobi json xml html htm ini log",
    "file_pdf": "pdf",
    "file_exe": "exe msi bat cmd ps1 dll sys apk app dmg sh",
    "file_save": "sav save sl2 ess srm rvdata2 rpgsave state",
}
EXT_ICON = {ext: name for name, exts in _EXT_ICONS.items() for ext in exts.split()}


def file_icon_name(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return EXT_ICON.get(ext, "file")


# --------------------------------------------------------------------------- 距離函數（設計座標）
def _sd_segment(x, y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / den))
    return math.hypot(x - ax - t * dx, y - ay - t * dy)


def _sd_rrect(x, y, x0, y0, x1, y1, r):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
    qx = abs(x - cx) - ((x1 - x0) / 2 - r)
    qy = abs(y - cy) - ((y1 - y0) / 2 - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _sd_polygon(x, y, pts):
    d = float("inf")
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        d = min(d, _sd_segment(x, y, xi, yi, xj, yj))
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return -d if inside else d


def _sd_arc(x, y, cx, cy, r, a0, a1):
    ang = math.degrees(math.atan2(y - cy, x - cx))
    span = a1 - a0
    rel = (ang - a0) % 360.0
    if rel <= span:
        return abs(math.hypot(x - cx, y - cy) - r)
    e0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    e1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    return min(math.hypot(x - e0[0], y - e0[1]), math.hypot(x - e1[0], y - e1[1]))


def _dist(prim, x, y, min_w):
    """回傳 (帶號距離, 顏色代號)。距離 <= 0 表示在圖形內。"""
    kind = prim[0]
    if kind == "PL":
        _, pts, w, c, closed = prim
        d = float("inf")
        n = len(pts)
        for i in range(n - 1 + (1 if closed else 0)):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            d = min(d, _sd_segment(x, y, ax, ay, bx, by))
        return d - max(w, min_w) / 2, c
    if kind == "P":
        return _sd_polygon(x, y, prim[1]), prim[2]
    if kind == "C":
        cx, cy, r = prim[1]
        return math.hypot(x - cx, y - cy) - r, prim[2]
    if kind == "O":
        cx, cy, r = prim[1]
        return abs(math.hypot(x - cx, y - cy) - r) - max(prim[2], min_w) / 2, prim[3]
    if kind == "R":
        return _sd_rrect(x, y, *prim[1]), prim[2]
    if kind == "B":
        return abs(_sd_rrect(x, y, *prim[1])) - max(prim[2], min_w) / 2, prim[3]
    if kind == "A":
        return _sd_arc(x, y, *prim[1]) - max(prim[2], min_w) / 2, prim[3]
    if kind == "S":
        cx, cy, r, a0, a1 = prim[1]
        d = math.hypot(x - cx, y - cy) - r
        rel = (math.degrees(math.atan2(y - cy, x - cx)) - a0) % 360.0
        return (d if rel <= (a1 - a0) else max(d, 0.6)), prim[2]
    raise ValueError(kind)


def _union_dist(shapes, x, y):
    d = float("inf")
    for s in shapes:
        if s[0] == "C":
            d = min(d, math.hypot(x - s[1], y - s[2]) - s[3])
        else:
            d = min(d, _sd_rrect(x, y, *s[1:6]))
    return d


def _bbox(prim, pad):
    kind = prim[0]
    if kind == "CLIP":
        x0, y0, x1, y1 = prim[1]
        bx0, by0, bx1, by1 = _bbox(prim[2], pad)
        return max(x0, bx0), max(y0, by0), min(x1, bx1), min(y1, by1)
    if kind in ("PL", "P"):
        xs, ys = [p[0] for p in prim[1]], [p[1] for p in prim[1]]
        w = prim[2] if kind == "PL" else 0
        return min(xs) - w - pad, min(ys) - w - pad, max(xs) + w + pad, max(ys) + w + pad
    if kind in ("C", "O", "A", "S"):
        cx, cy, r = prim[1][:3]
        w = prim[2] if kind in ("O", "A") else 0
        return cx - r - w - pad, cy - r - w - pad, cx + r + w + pad, cy + r + w + pad
    if kind in ("R", "B"):
        x0, y0, x1, y1 = prim[1][:4]
        w = prim[2] if kind == "B" else 0
        return x0 - w - pad, y0 - w - pad, x1 + w + pad, y1 + w + pad
    return 0.0, 0.0, GRID, GRID  # U


def _hex(c: str):
    c = c.lstrip("#")
    return int(c[0:2], 16) / 255, int(c[2:4], 16) / 255, int(c[4:6], 16) / 255


def _over(dst, color, alpha):
    if alpha <= 0:
        return dst
    r, g, b, a = dst
    out_a = alpha + a * (1 - alpha)
    return ((color[0] * alpha + r * a * (1 - alpha)) / out_a, (color[1] * alpha + g * a * (1 - alpha)) / out_a,
            (color[2] * alpha + b * a * (1 - alpha)) / out_a, out_a)


def raster(prims, size: int, dark: bool = False, tint: str | None = None, opacity: float = 1.0):
    """把圖示畫成 size×size 的像素（每個像素為 (r, g, b, a)，0~1）。tint：全部改用這個顏色（停用狀態）。"""
    s = size / GRID
    min_w = 1.15 / s  # 小尺寸時線條至少約 1 像素粗
    pad = 1.0 / s
    pal = {k: _hex(tint if tint else v[1 if dark else 0]) for k, v in COLORS.items()}
    rows = [[(0.0, 0.0, 0.0, 0.0)] * size for _ in range(size)]
    for prim in prims:
        x0, y0, x1, y1 = _bbox(prim, pad)
        i0, i1 = max(0, int(x0 * s)), min(size, int(math.ceil(x1 * s)) + 1)
        j0, j1 = max(0, int(y0 * s)), min(size, int(math.ceil(y1 * s)) + 1)
        cl = None
        if prim[0] == "CLIP":
            cl, prim = prim[1], prim[2]
        for j in range(j0, j1):
            y = (j + 0.5) / s
            row = rows[j]
            for i in range(i0, i1):
                x = (i + 0.5) / s
                if cl and not (cl[0] <= x <= cl[2] and cl[1] <= y <= cl[3]):
                    continue
                if prim[0] == "U":
                    _, shapes, fill, stroke, w = prim
                    d = _union_dist(shapes, x, y)
                    if fill:
                        a = max(0.0, min(1.0, 0.5 - d * s))
                        if a > 0:
                            row[i] = _over(row[i], pal[fill], a * opacity)
                    if stroke:
                        a = max(0.0, min(1.0, 0.5 - (abs(d) - max(w, min_w) / 2) * s))
                        if a > 0:
                            row[i] = _over(row[i], pal[stroke], a * opacity)
                    continue
                d, c = _dist(prim, x, y, min_w)
                a = max(0.0, min(1.0, 0.5 - d * s))
                if a > 0:
                    row[i] = _over(row[i], pal[c], a * opacity)
    return rows


def png_bytes(rows) -> bytes:
    h, w = len(rows), len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((round(r * 255), round(g * 255), round(b * 255), round(a * 255)))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 1)) + chunk(b"IEND", b""))


def pie(fraction: float):
    """可用空間的小圓餅圖：fraction = 已使用的比例。"""
    fraction = max(0.0, min(1.0, fraction))
    out = [C(12, 12, 8.6, "w"), O(12, 12, 8.6, 1.5, "a")]
    if fraction > 0.004:
        out.insert(1, S(12, 12, 8.6, -90, -90 + 360 * fraction, "a"))
    return out


# --------------------------------------------------------------------------- Tk 圖片（有快取）
_cache: dict = {}


def get(name: str, size: int, dark: bool = False, disabled: bool = False):
    """回傳 tk.PhotoImage；同一個圖示、大小與主題只畫一次。"""
    import tkinter as tk

    key = (name, size, dark, disabled)
    img = _cache.get(key)
    if img is None:
        if name.startswith("pie:"):
            prims = _flat(pie(int(name[4:]) / 20))
        else:
            prims = ICONS[name]
        tint = ("#5C5C5C" if dark else "#B4B4B4") if disabled else None
        rows = raster(prims, size, dark, tint)
        img = tk.PhotoImage(data=base64.b64encode(png_bytes(rows)).decode("ascii"), format="png")
        _cache[key] = img
    return img


def blank(size: int):
    import tkinter as tk

    key = ("", size)
    if key not in _cache:
        rows = [[(0.0, 0.0, 0.0, 0.0)] * size for _ in range(size)]
        _cache[key] = tk.PhotoImage(data=base64.b64encode(png_bytes(rows)).decode("ascii"), format="png")
    return _cache[key]


def reset() -> None:
    """Tk 視窗關閉後舊的圖片不能再用（測試會重複建立視窗）。"""
    _cache.clear()
