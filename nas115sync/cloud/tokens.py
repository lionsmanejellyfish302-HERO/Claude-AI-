"""登入資訊（115 cookies）的保存：Windows 上以 DPAPI 加密，只有目前的 Windows 使用者能解開。"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI：以目前 Windows 使用者身分加密，別的帳號或電腦無法解開。"""
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class TokenStore:
    PREFIX = b"DPAPI:"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        raw = self.path.read_bytes()
        try:
            if raw.startswith(self.PREFIX):
                raw = _dpapi(base64.b64decode(raw[len(self.PREFIX):]), protect=False)
            return json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("無法讀取 115 登入資訊（需重新登入）：%s", e)
            return {}

    def save(self, data: dict) -> None:
        if not data:
            self.clear()
            return
        raw = json.dumps(data).encode("utf-8")
        if sys.platform == "win32":
            # 加密失敗就不保存：登入資訊等同帳號密碼，絕不以明文寫入磁碟
            raw = self.PREFIX + base64.b64encode(_dpapi(raw, protect=True))
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 非 Windows：只有自己可讀
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        for p in (self.path, self.path.with_suffix(".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
