"""檔案雜湊：一次讀取同時算出 MD5、SHA-1 與 115 秒傳用的前 128KB SHA-1（preid）。"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Callable

from .paths import long_path

PREID_SIZE = 128 * 1024
CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class FileHashes:
    size: int
    md5: str  # 小寫 hex
    sha1: str  # 大寫 hex（115 使用大寫）
    preid: str  # 前 128KB 的 SHA-1，大寫


class HashCancelled(Exception):
    pass


def hash_file(
    path: str,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> FileHashes:
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    pre = hashlib.sha1()
    pre_left = PREID_SIZE
    size = 0
    with open(long_path(path), "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise HashCancelled(path)
            buf = f.read(CHUNK)
            if not buf:
                break
            md5.update(buf)
            sha1.update(buf)
            if pre_left > 0:
                head = buf[:pre_left]
                pre.update(head)
                pre_left -= len(head)
            size += len(buf)
            if progress:
                progress(size)
    return FileHashes(size, md5.hexdigest(), sha1.hexdigest().upper(), pre.hexdigest().upper())


def hash_bytes(data: bytes) -> FileHashes:
    return FileHashes(
        len(data),
        hashlib.md5(data).hexdigest(),
        hashlib.sha1(data).hexdigest().upper(),
        hashlib.sha1(data[:PREID_SIZE]).hexdigest().upper(),
    )


def sha1_range(path: str, start: int, end: int) -> str:
    """115 二次驗證（sign_check "start-end"）：計算 [start, end]（含兩端）的 SHA-1。"""
    if end < start:
        raise ValueError(f"invalid range {start}-{end}")
    h = hashlib.sha1()
    left = end - start + 1
    with open(long_path(path), "rb") as f:
        f.seek(start)
        while left > 0:
            buf = f.read(min(CHUNK, left))
            if not buf:
                break
            h.update(buf)
            left -= len(buf)
    return h.hexdigest().upper()
