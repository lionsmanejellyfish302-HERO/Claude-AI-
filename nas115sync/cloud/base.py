"""雲端客戶端介面。Open115Client（正式）與 FakeCloud（測試/示範）都實作此介面。"""
from __future__ import annotations

import abc
import threading
from dataclasses import dataclass
from typing import Callable, Iterator

from ..hashing import FileHashes
from ..paths import cloud_parts, name_key, nfc, rel_join
from ..transfers import TransferControl


class CloudError(Exception):
    def __init__(self, message: str, code: int | str | None = None):
        super().__init__(message if code is None else f"[{code}] {message}")
        self.code = code


class AuthError(CloudError):
    pass


class NotFoundError(CloudError):
    pass


@dataclass
class CloudEntry:
    fid: str
    name: str
    is_dir: bool
    pid: str = ""
    size: int = 0
    sha1: str = ""
    pick_code: str = ""
    mtime: int = 0


@dataclass
class UploadResult:
    fid: str
    name: str
    sha1: str
    size: int
    instant: bool  # True = 秒傳成功
    pick_code: str = ""


class SessionStore:
    """斷點續傳資訊的保存介面（引擎以資料庫實作；預設不保存）。"""

    def get(self, key: str) -> dict | None:
        return None

    def put(self, key: str, data: dict) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


class CloudClient(abc.ABC):
    root_cid = "0"

    def __init__(self):
        self._mkdir_lock = threading.Lock()

    @abc.abstractmethod
    def list_dir(self, cid: str) -> list[CloudEntry]: ...

    @abc.abstractmethod
    def get_entry(self, fid: str) -> CloudEntry | None: ...

    @abc.abstractmethod
    def mkdir(self, pid: str, name: str) -> str: ...

    @abc.abstractmethod
    def delete(self, fids: list[str], parent_id: str = "") -> None: ...

    @abc.abstractmethod
    def rename(self, fid: str, new_name: str) -> None: ...

    @abc.abstractmethod
    def move(self, fids: list[str], to_cid: str) -> None: ...

    @abc.abstractmethod
    def try_instant(self, local_path: str, cid: str, name: str, hashes: FileHashes) -> UploadResult | None:
        """只嘗試秒傳；秒傳不成功回傳 None，不實際上傳。"""

    @abc.abstractmethod
    def upload(self, local_path: str, cid: str, name: str, hashes: FileHashes,
               control: TransferControl | None = None, sessions: SessionStore | None = None) -> UploadResult:
        """先嘗試秒傳，不成功再實際上傳（大檔可斷點續傳）。"""

    @abc.abstractmethod
    def download(self, fid: str, pick_code: str, dest_path: str, control: TransferControl | None = None) -> None:
        """下載到 dest_path；dest_path 已有部分內容時從其大小處續傳。"""

    def search(self, keyword: str, cid: str = "0", limit: int = 500) -> list[CloudEntry]:
        """以檔名搜尋（cid 底下）。"""
        raise CloudError("此雲端不支援搜尋")

    def user_label(self) -> str:
        return ""

    def web_url(self, cid: str) -> str:
        return f"https://115.com/?cid={cid}&offset=0&mode=wangpan"

    # ------------------------------------------------------------ 共用輔助
    def find_child(self, cid: str, name: str, want_dir: bool | None = None) -> CloudEntry | None:
        entries = [e for e in self.list_dir(cid) if want_dir is None or e.is_dir == want_dir]
        exact = [e for e in entries if nfc(e.name) == nfc(name)]
        if exact:
            return exact[0]
        loose = [e for e in entries if name_key(e.name) == name_key(name)]
        return loose[0] if loose else None

    def resolve_path(self, path: str) -> str | None:
        cid = self.root_cid
        for part in cloud_parts(path):
            child = self.find_child(cid, part, want_dir=True)
            if child is None:
                return None
            cid = child.fid
        return cid

    def ensure_child_dir(self, pid: str, name: str) -> str:
        with self._mkdir_lock:
            existing = self.find_child(pid, name, want_dir=True)
            if existing:
                return existing.fid
            return self.mkdir(pid, name)

    def ensure_path(self, path: str) -> str:
        cid = self.root_cid
        for part in cloud_parts(path):
            cid = self.ensure_child_dir(cid, part)
        return cid

    def walk(self, cid: str, rel: str = "", skip_dir: Callable[[str], bool] | None = None,
             cancel: threading.Event | None = None) -> Iterator[tuple[str, str, list[CloudEntry]]]:
        """深度優先走訪：yield (相對路徑, cid, 該資料夾內容)。"""
        stack = [(rel, cid)]
        while stack:
            if cancel is not None and cancel.is_set():
                return
            cur_rel, cur_cid = stack.pop()
            entries = self.list_dir(cur_cid)
            yield cur_rel, cur_cid, entries
            for e in reversed(entries):
                if e.is_dir:
                    child_rel = rel_join(cur_rel, e.name)
                    if skip_dir is None or not skip_dir(child_rel):
                        stack.append((child_rel, e.fid))
