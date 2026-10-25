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
