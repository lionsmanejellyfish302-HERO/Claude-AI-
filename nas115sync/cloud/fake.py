"""模擬 115 網盤（記憶體＋可選 JSON 保存），用於自動測試與「示範模式」（不需 115 帳號即可試用）。

known_sha1 模擬 115 全站已存在的檔案：上傳 SHA-1 在其中就會「秒傳」。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from ..hashing import FileHashes, hash_bytes
from ..paths import cloud_parts, long_path, name_key, nfc
from ..transfers import TransferControl
from .base import CloudChange, CloudClient, CloudEntry, CloudError, NotFoundError, SessionStore, UploadResult


class FakeCloud(CloudClient):
    def __init__(self, path: str | Path | None = None, rename_duplicates: bool = False):
        super().__init__()
        self.path = Path(path) if path else None
        self.rename_duplicates = rename_duplicates
        # 檔案內容（依 SHA-1）：示範模式存於磁碟，測試時存於記憶體，供下載使用
        self.blob_dir = self.path.with_name(self.path.stem + "_blobs") if self.path else None
        self._blobs: dict[str, bytes] = {}
        self.download_count = 0
        self.nodes: dict[str, dict] = {"0": {"fid": "0", "pid": "", "name": "", "is_dir": True}}
        self.known_sha1: set[str] = set()
        self.instant_count = 0
        self.real_upload_count = 0
        self.api_calls = 0
        self.events: list[dict] = []  # 模擬 115「操作紀錄」
        self._next = 1000
        self._lock = threading.RLock()
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.nodes = data["nodes"]
            self.known_sha1 = set(data.get("known_sha1", []))
            self._next = data.get("next", 1000)

    # ------------------------------------------------------------ 內部
    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"nodes": self.nodes, "known_sha1": sorted(self.known_sha1),
                                       "next": self._next}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)

    def _event(self, kind: int, n: dict) -> None:
        self.events.append({"id": len(self.events) + 1, "type": kind, "fid": n["fid"], "pid": n["pid"],
                            "name": n["name"]})

    def _new_id(self) -> str:
        self._next += 1
        return str(self._next)

    def _dir(self, cid: str) -> dict:
        n = self.nodes.get(str(cid))
        if not n or not n["is_dir"]:
            raise NotFoundError(f"資料夾不存在 cid={cid}")
        return n

    def _children(self, cid: str) -> list[dict]:
        return [n for n in self.nodes.values() if n["pid"] == str(cid) and n["fid"] != "0"]

    @staticmethod
    def _entry(n: dict) -> CloudEntry:
        return CloudEntry(fid=n["fid"], name=n["name"], is_dir=n["is_dir"], pid=n["pid"],
                          size=n.get("size", 0), sha1=n.get("sha1", ""), pick_code="pc" + n["fid"],
                          mtime=n.get("mtime", 0))

    def _put_blob(self, sha1: str, data: bytes) -> None:
        if self.blob_dir:
            self.blob_dir.mkdir(exist_ok=True)
            (self.blob_dir / sha1.upper()).write_bytes(data)
        else:
            self._blobs[sha1.upper()] = data

    def _get_blob(self, sha1: str) -> bytes | None:
        if self.blob_dir and (self.blob_dir / sha1.upper()).exists():
            return (self.blob_dir / sha1.upper()).read_bytes()
        return self._blobs.get(sha1.upper())

    def _add_file(self, cid: str, name: str, h: FileHashes) -> dict:
        self._dir(cid)
        final = name
        if self.rename_duplicates:
            names = {c["name"] for c in self._children(cid)}
            stem, ext = os.path.splitext(name)
            i = 1
            while final in names:
                final = f"{stem}({i}){ext}"
                i += 1
        n = {"fid": self._new_id(), "pid": str(cid), "name": nfc(final), "is_dir": False,
             "size": h.size, "sha1": h.sha1.upper(), "mtime": int(time.time())}
        self.nodes[n["fid"]] = n
        self.known_sha1.add(h.sha1.upper())
        self._event(2, n)
        self._save()
        return n

    # ------------------------------------------------------------ CloudClient
    def user_label(self) -> str:
        return "示範模式（模擬 115）"

    def list_dir(self, cid: str) -> list[CloudEntry]:
        with self._lock:
            self.api_calls += 1
            self._dir(cid)
            return [self._entry(n) for n in sorted(self._children(cid), key=lambda n: n["name"])]

    def get_entry(self, fid: str) -> CloudEntry | None:
        with self._lock:
            n = self.nodes.get(str(fid))
            return self._entry(n) if n else None

    def mkdir(self, pid: str, name: str) -> str:
        with self._lock:
            self.api_calls += 1
            self._dir(pid)
            if any(c["is_dir"] and c["name"] == name for c in self._children(pid)):
                raise CloudError("資料夾已存在", 20004)
            n = {"fid": self._new_id(), "pid": str(pid), "name": nfc(name), "is_dir": True}
            self.nodes[n["fid"]] = n
            self._event(17, n)
            self._save()
            return n["fid"]

    def delete(self, fids: list[str], parent_id: str = "") -> None:
        with self._lock:
            self.api_calls += 1
            todo = [str(f) for f in fids]
            while todo:
                fid = todo.pop()
                if fid in self.nodes and fid != "0":
                    todo.extend(c["fid"] for c in self._children(fid))
                    self._event(22, self.nodes.pop(fid))
            self._save()

    def rename(self, fid: str, new_name: str) -> None:
        with self._lock:
            self.api_calls += 1
            if str(fid) not in self.nodes:
                raise NotFoundError(f"檔案不存在 {fid}")
            self.nodes[str(fid)]["name"] = nfc(new_name)
            self._event(24, self.nodes[str(fid)])
            self._save()

    def move(self, fids: list[str], to_cid: str) -> None:
        with self._lock:
            self.api_calls += 1
            self._dir(to_cid)
            for fid in fids:
                if str(fid) not in self.nodes:
                    raise NotFoundError(f"檔案不存在 {fid}")
                self.nodes[str(fid)]["pid"] = str(to_cid)
                self._event(6, self.nodes[str(fid)])
            self._save()

    def try_instant(self, local_path: str, cid: str, name: str, hashes: FileHashes) -> UploadResult | None:
        with self._lock:
            self.api_calls += 1
            if hashes.sha1.upper() not in self.known_sha1:
                return None
            n = self._add_file(cid, name, hashes)
            self.instant_count += 1
            return UploadResult(n["fid"], n["name"], hashes.sha1, hashes.size, True, "pc" + n["fid"])

    def upload(self, local_path: str, cid: str, name: str, hashes: FileHashes,
               control: TransferControl | None = None, sessions: SessionStore | None = None) -> UploadResult:
        res = self.try_instant(local_path, cid, name, hashes)
        if res:
            return res
        with open(long_path(local_path), "rb") as f:
            data = f.read()
        if hash_bytes(data).sha1 != hashes.sha1.upper():
            raise CloudError("上傳過程中檔案內容已改變（SHA-1 不符）")
        if control is not None:
            for i in range(0, len(data), 64 * 1024):
                control.advance(len(data[i:i + 64 * 1024]))
        with self._lock:
            self._put_blob(hashes.sha1, data)
            n = self._add_file(cid, name, hashes)
            self.real_upload_count += 1
        return UploadResult(n["fid"], n["name"], hashes.sha1, hashes.size, False, "pc" + n["fid"])

    def download(self, fid: str, pick_code: str, dest_path: str, control: TransferControl | None = None) -> None:
        with self._lock:
            self.api_calls += 1
            n = self.nodes.get(str(fid))
            if not n or n["is_dir"]:
                raise NotFoundError(f"檔案不存在 {fid}")
            data = self._get_blob(n["sha1"])
        if data is None:
            raise CloudError("模擬 115 沒有此檔案的內容")
        lp = long_path(dest_path)
        offset = os.path.getsize(lp) if os.path.exists(lp) else 0
        with open(lp, "ab" if offset else "wb") as f:
            if control is not None:
                control.set_done(offset)
            for i in range(offset, len(data), 64 * 1024):
                chunk = data[i:i + 64 * 1024]
                if control is not None:
                    control.advance(len(chunk))
                f.write(chunk)
        self.download_count += 1

    def _under(self, cid: str) -> set[str]:
        under = {str(cid)}
        changed = True
        while changed:  # cid 底下所有資料夾
            changed = False
            for n in self.nodes.values():
                if n["is_dir"] and n["pid"] in under and n["fid"] not in under:
                    under.add(n["fid"])
                    changed = True
        return under

    def changes(self, cursor: str | None) -> tuple[list[CloudChange], str]:
        with self._lock:
            self.api_calls += 1
            last = int(cursor or 0) if cursor is not None else len(self.events)
            new = [CloudChange(e["fid"], e["pid"], str(e["type"]), e["name"]) for e in self.events if e["id"] > last]
            return ([] if cursor is None else new), str(len(self.events))

    def recent_files(self, cid: str, limit: int = 50) -> list[CloudEntry]:
        with self._lock:
            self.api_calls += 1
            under = self._under(cid)
            files = [n for n in self.nodes.values() if not n["is_dir"] and n["pid"] in under]
            files.sort(key=lambda n: (n.get("mtime", 0), int(n["fid"])), reverse=True)
            return [self._entry(n) for n in files[:limit]]

    def search(self, keyword: str, cid: str = "0", limit: int = 500) -> list[CloudEntry]:
        with self._lock:
            self.api_calls += 1
            under = self._under(cid)
            kw = name_key(keyword)
            hits = [n for n in self.nodes.values() if n["fid"] != "0" and n["pid"] in under and kw in name_key(n["name"])]
            return [self._entry(n) for n in hits[:limit]]

    # ------------------------------------------------------------ 測試輔助
    def put_file(self, path: str, content: bytes) -> str:
        parts = cloud_parts(path)
        cid = self.ensure_path("/" + "/".join(parts[:-1]))
        with self._lock:
            h = hash_bytes(content)
            self._put_blob(h.sha1, content)
            return self._add_file(cid, parts[-1], h)["fid"]

    def files_in(self, path: str) -> dict[str, CloudEntry]:
        cid = self.resolve_path(path)
        if cid is None:
            return {}
        return {e.name: e for e in self.list_dir(cid) if not e.is_dir}
