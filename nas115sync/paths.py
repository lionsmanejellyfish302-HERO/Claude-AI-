"""路徑工具：比對用正規化、Windows 長路徑、雲端路徑、忽略規則。"""
from __future__ import annotations

import fnmatch
import os
import sys
import unicodedata


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def norm_key(rel_path: str) -> str:
    """比對用的鍵：統一斜線、Unicode NFC、不分大小寫（Windows/SMB 不分大小寫）。"""
    s = rel_path.replace("\\", "/").strip("/")
    return nfc(s).casefold()


def name_key(name: str) -> str:
    return nfc(name).casefold()


def rel_join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def rel_parent(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""


def rel_name(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[-1]


def local_join(root: str, rel_path: str) -> str:
    if not rel_path:
        return root
    return os.path.join(root, *rel_path.split("/"))


_BAD_CHARS = frozenset('<>:"/\\|?*') | frozenset(chr(i) for i in range(32))
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})


def unsafe_name(name: str) -> str | None:
    """115 上的名稱不能安全地當成 NAS（Windows）上的檔名時，回傳原因；可以就回傳 None。

    擋下：. 與 ..（會跳到上層資料夾）、路徑分隔符號與磁碟代號（會寫到別的地方）、
    冒號（NTFS 隱藏資料流）、Windows 保留裝置名稱、結尾空白或句點（會與別的檔案變成同名）。
    """
    if not name or name in (".", ".."):
        return "名稱是空的或是「.」「..」"
    if any(c in _BAD_CHARS for c in name):
        return "名稱含有 Windows 不允許的字元（< > : \" / \\ | ? * 或控制字元）"
    if name[-1] in " .":
        return "名稱結尾是空白或句點"
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED:
        return "名稱是 Windows 保留的裝置名稱"
    return None


def _plain(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.realpath(path)).rstrip("\\/")


def is_inside(root: str, path: str) -> bool:
    """path 實際上（解析捷徑、符號連結、junction 之後）仍位於 root 底下。"""
    r, p = _plain(long_path(root)), _plain(long_path(path))
    return p == r or p.startswith(r + os.sep)


def long_path(path: str) -> str:
    """Windows 超過 260 字元的路徑需加 \\\\?\\ 前綴才能存取。"""
    if sys.platform != "win32" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if len(path) < 240:
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def cloud_norm(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/" + "/".join(parts)


def cloud_parts(path: str) -> list[str]:
    return [p for p in cloud_norm(path).split("/") if p]


def cloud_join(*parts: str) -> str:
    return cloud_norm("/".join(parts))


class IgnoreMatcher:
    def __init__(self, patterns):
        self.patterns = [name_key(p) for p in patterns if p]

    def match_name(self, name: str) -> bool:
        n = name_key(name)
        return any(fnmatch.fnmatchcase(n, p) for p in self.patterns)

    def match_rel(self, rel_path: str) -> bool:
        return any(self.match_name(part) for part in rel_path.split("/") if part)


FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x2, 0x4


class MappingFilter:
    """每個路徑對應自己的篩選條件。

    規則含「/」時比對整個相對路徑，否則比對檔名；exclude 的檔名規則也會排除同名資料夾底下的所有檔案。
    """

    def __init__(self, m):
        self._sig = self.signature(m)
        self.include = [name_key(p.strip()) for p in m.include_patterns if p.strip()]
        self.exclude = [name_key(p.strip()) for p in m.exclude_patterns if p.strip()]
        self.min_size = int(m.min_size or 0)
        self.max_size = int(m.max_size or 0)
        self.skip_hidden = bool(m.skip_hidden)

    @staticmethod
    def signature(m) -> tuple:
        return (tuple(m.include_patterns), tuple(m.exclude_patterns), m.min_size, m.max_size, m.skip_hidden)

    def same_as(self, m) -> bool:
        return self._sig == self.signature(m)

    @staticmethod
    def _hit(patterns: list[str], rel_key: str, parts: list[str]) -> bool:
        for p in patterns:
            if "/" in p:
                if fnmatch.fnmatchcase(rel_key, p.strip("/")):
                    return True
            elif any(fnmatch.fnmatchcase(part, p) for part in parts):
                return True
        return False

    def excludes_dir(self, rel_dir: str) -> bool:
        key = norm_key(rel_dir)
        return bool(self.exclude) and self._hit(self.exclude, key, key.split("/"))

    def accepts_file(self, rel_path: str, size: int | None = None) -> bool:
        key = norm_key(rel_path)
        parts = key.split("/")
        if self.exclude and self._hit(self.exclude, key, parts):
            return False
        if self.skip_hidden and any(p.startswith(".") for p in parts):  # 115 端沒有隱藏屬性，以 . 開頭判斷
            return False
        if self.include and not self._hit(self.include, key, parts[-1:]):
            return False
        if size is not None:
            if self.min_size and size < self.min_size:
                return False
            if self.max_size and size > self.max_size:
                return False
        return True

    def is_hidden(self, name: str, st=None) -> bool:
        if not self.skip_hidden:
            return False
        if name.startswith("."):
            return True
        attrs = getattr(st, "st_file_attributes", 0) if st is not None else 0
        return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
