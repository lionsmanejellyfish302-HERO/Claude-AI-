"""SQLite 狀態資料庫：雜湊快取、NAS/雲端索引、同步基準、待核對清單、存檔群組、紀錄。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .hashing import FileHashes
from .paths import norm_key, rel_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_cache(
    path_key TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime_ns INTEGER,
    md5 TEXT, sha1 TEXT, preid TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS local_files(
    mapping_id TEXT, rel_key TEXT, rel_path TEXT, size INTEGER, mtime_ns INTEGER,
    md5 TEXT, sha1 TEXT, preid TEXT, PRIMARY KEY(mapping_id, rel_key));
CREATE INDEX IF NOT EXISTS ix_local_sha1 ON local_files(sha1);
CREATE INDEX IF NOT EXISTS ix_local_md5 ON local_files(md5);
CREATE TABLE IF NOT EXISTS cloud_files(
    mapping_id TEXT, fid TEXT, rel_key TEXT, rel_path TEXT, pid TEXT, size INTEGER,
    sha1 TEXT, pick_code TEXT, mtime INTEGER, name_key TEXT, PRIMARY KEY(mapping_id, fid));
CREATE INDEX IF NOT EXISTS ix_cloud_key ON cloud_files(mapping_id, rel_key);
CREATE INDEX IF NOT EXISTS ix_cloud_sha1 ON cloud_files(sha1);
CREATE INDEX IF NOT EXISTS ix_cloud_name ON cloud_files(mapping_id, name_key);
CREATE TABLE IF NOT EXISTS cloud_dirs(
    mapping_id TEXT, rel_key TEXT, rel_path TEXT, cid TEXT, PRIMARY KEY(mapping_id, rel_key));
CREATE TABLE IF NOT EXISTS sync_state(
    mapping_id TEXT, rel_key TEXT, rel_path TEXT, local_size INTEGER, local_mtime_ns INTEGER,
    local_md5 TEXT, local_sha1 TEXT, cloud_fid TEXT, cloud_sha1 TEXT, synced_at REAL,
    PRIMARY KEY(mapping_id, rel_key));
CREATE INDEX IF NOT EXISTS ix_state_md5 ON sync_state(local_md5);
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id TEXT, kind TEXT, rel_key TEXT,
    rel_path TEXT, status TEXT DEFAULT 'pending', decision TEXT, details TEXT,
    created_at REAL, resolved_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_pending
    ON reviews(mapping_id, kind, rel_key) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS ignores(
    mapping_id TEXT, rel_key TEXT, kind TEXT, rel_path TEXT, created_at REAL,
    PRIMARY KEY(mapping_id, rel_key, kind));
CREATE TABLE IF NOT EXISTS savedata_groups(
    mapping_id TEXT, rel_key TEXT, rel_dir TEXT, status TEXT, process_names TEXT,
    last_upload REAL, PRIMARY KEY(mapping_id, rel_key));
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mapping_id TEXT, action TEXT,
    rel_path TEXT, ok INTEGER, instant INTEGER, size INTEGER, detail TEXT);
"""


@dataclass
class LocalRow:
    mapping_id: str
    rel_key: str
    rel_path: str
    size: int
    mtime_ns: int
    md5: str
    sha1: str
    preid: str

    @property
    def hashes(self) -> FileHashes:
        return FileHashes(self.size, self.md5, self.sha1, self.preid)

    @property
    def name(self) -> str:
        return rel_name(self.rel_path)


@dataclass
class CloudRow:
    mapping_id: str
    fid: str
    rel_key: str
    rel_path: str
    pid: str
    size: int
    sha1: str
    pick_code: str
    mtime: int

    @property
    def name(self) -> str:
        return rel_name(self.rel_path)

    def brief(self) -> dict:
        return {"fid": self.fid, "pid": self.pid, "rel_path": self.rel_path, "size": self.size,
                "sha1": self.sha1, "mtime": self.mtime, "mapping_id": self.mapping_id}


@dataclass
class StateRow:
    mapping_id: str
    rel_key: str
    rel_path: str
    local_size: int
    local_mtime_ns: int
    local_md5: str
    local_sha1: str
    cloud_fid: str
    cloud_sha1: str
    synced_at: float


@dataclass
class Review:
    id: int
    mapping_id: str
    kind: str
    rel_key: str
    rel_path: str
    status: str
    decision: str | None
    details: dict = field(default_factory=dict)
    created_at: float = 0.0
    resolved_at: float | None = None


@dataclass
class SavedataGroup:
    mapping_id: str
    rel_key: str
    rel_dir: str
    status: str  # pending | approved | normal | exclude
    process_names: list = field(default_factory=list)
    last_upload: float | None = None


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._tx_depth = threading.local()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(args))

    def _all(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(args)).fetchone()

    def transaction(self):
        return _Tx(self)

    # ---------------------------------------------------------------- 雜湊快取
    def get_cached_hash(self, path: str, size: int, mtime_ns: int) -> FileHashes | None:
        r = self._one("SELECT * FROM hash_cache WHERE path_key=?", (norm_key(path),))
        if r and r["size"] == size and r["mtime_ns"] == mtime_ns and r["sha1"]:
            return FileHashes(r["size"], r["md5"], r["sha1"], r["preid"])
        return None

    def put_cached_hash(self, path: str, mtime_ns: int, h: FileHashes) -> None:
        self._exec(
            "INSERT OR REPLACE INTO hash_cache VALUES(?,?,?,?,?,?,?,?)",
            (norm_key(path), path, h.size, mtime_ns, h.md5, h.sha1, h.preid, time.time()),
        )

    # ---------------------------------------------------------------- NAS 索引
    def replace_local_index(self, mapping_id: str, rows: Iterable[LocalRow]) -> None:
        with self.transaction():
            self._exec("DELETE FROM local_files WHERE mapping_id=?", (mapping_id,))
            for r in rows:
                self._put_local(r)

    def _put_local(self, r: LocalRow) -> None:
        self._exec(
            "INSERT OR REPLACE INTO local_files VALUES(?,?,?,?,?,?,?,?)",
            (r.mapping_id, r.rel_key, r.rel_path, r.size, r.mtime_ns, r.md5, r.sha1, r.preid),
        )

    def put_local(self, r: LocalRow) -> None:
        self._put_local(r)

    def delete_local(self, mapping_id: str, rel_key: str) -> None:
        self._exec("DELETE FROM local_files WHERE mapping_id=? AND rel_key=?", (mapping_id, rel_key))

    def local_files(self, mapping_id: str) -> list[LocalRow]:
        return [LocalRow(**dict(r)) for r in
                self._all("SELECT * FROM local_files WHERE mapping_id=? ORDER BY rel_key", (mapping_id,))]

    def local_at(self, mapping_id: str, rel_key: str) -> LocalRow | None:
        r = self._one("SELECT * FROM local_files WHERE mapping_id=? AND rel_key=?", (mapping_id, rel_key))
        return LocalRow(**dict(r)) if r else None

    def local_by_hash(self, sha1: str = "", md5: str = "") -> list[LocalRow]:
        rows = self._all(
            "SELECT * FROM local_files WHERE (sha1=? AND sha1!='') OR (md5=? AND md5!='')",
            (sha1.upper(), md5.lower()),
        )
        return [LocalRow(**dict(r)) for r in rows]

    def local_under(self, mapping_id: str, rel_dir: str) -> list[LocalRow]:
        sql, args = _under("SELECT * FROM local_files WHERE mapping_id=?", mapping_id, rel_dir)
        return [LocalRow(**dict(r)) for r in self._all(sql, args)]

    def delete_local_under(self, mapping_id: str, rel_path: str) -> None:
        key = norm_key(rel_path)
        sql, args = _under("DELETE FROM local_files WHERE mapping_id=?", mapping_id, rel_path)
        with self.transaction():
            self._exec("DELETE FROM local_files WHERE mapping_id=? AND rel_key=?", (mapping_id, key))
            if key:
                self._exec(sql, args)

    # ---------------------------------------------------------------- 雲端索引
    def replace_cloud_index(self, mapping_id: str, files: Iterable[CloudRow],
                            dirs: Iterable[tuple[str, str]]) -> None:
        with self.transaction():
            self._exec("DELETE FROM cloud_files WHERE mapping_id=?", (mapping_id,))
            self._exec("DELETE FROM cloud_dirs WHERE mapping_id=?", (mapping_id,))
            for f in files:
                self.put_cloud_file(f)
            for rel_path, cid in dirs:
                self.set_cloud_dir(mapping_id, rel_path, cid)

    def put_cloud_file(self, f: CloudRow) -> None:
        self._exec(
            "INSERT OR REPLACE INTO cloud_files VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f.mapping_id, f.fid, f.rel_key, f.rel_path, f.pid, f.size, f.sha1.upper(),
             f.pick_code, f.mtime, norm_key(f.name)),
        )

    def remove_cloud_file(self, mapping_id: str, fid: str) -> None:
        self._exec("DELETE FROM cloud_files WHERE mapping_id=? AND fid=?", (mapping_id, fid))

    def replace_cloud_listing(self, mapping_id: str, rel_dir: str, cid: str, files: Iterable[CloudRow],
                              dirs: Iterable[tuple[str, str]], recursive: bool = False) -> None:
        """以雲端資料夾（或整個子樹）的最新列表取代索引中的對應部分。"""
        key = norm_key(rel_dir)
        with self.transaction():
            sub = {k: v for k, v in self.cloud_dirs(mapping_id).items() if _is_under(k, key)}
            if recursive:
                for k, (_, sub_cid) in sub.items():
                    self._exec("DELETE FROM cloud_files WHERE mapping_id=? AND pid=?", (mapping_id, sub_cid))
                    self._exec("DELETE FROM cloud_dirs WHERE mapping_id=? AND rel_key=?", (mapping_id, k))
            else:
                for k in sub:
                    if _parent_key(k) == key:
                        self._exec("DELETE FROM cloud_dirs WHERE mapping_id=? AND rel_key=?", (mapping_id, k))
            self._exec("DELETE FROM cloud_files WHERE mapping_id=? AND pid=?", (mapping_id, cid))
            self.set_cloud_dir(mapping_id, rel_dir, cid)
            for f in files:
                self.put_cloud_file(f)
            for rel_path, sub_cid in dirs:
                self.set_cloud_dir(mapping_id, rel_path, sub_cid)

    def forget_cloud_dir_prefix(self, mapping_id: str, rel_dir: str) -> None:
        key = norm_key(rel_dir)
        with self.transaction():
            for k in list(self.cloud_dirs(mapping_id)):
                if k == key or _is_under(k, key):
                    self._exec("DELETE FROM cloud_dirs WHERE mapping_id=? AND rel_key=?", (mapping_id, k))

    def rename_prefix(self, mapping_id: str, old_rel: str, new_rel: str, new_pid: str | None = None) -> None:
        """NAS 端搬移/改名後，在 115 也搬移完成時，同步更新所有索引中的路徑。"""
        old_key = norm_key(old_rel)

        def moved(rel_path: str) -> str:
            return new_rel + rel_path[len(old_rel):]

        with self.transaction():
            for table in ("local_files", "sync_state", "cloud_files", "cloud_dirs"):
                rows = self._all(f"SELECT rowid AS rid, rel_key, rel_path FROM {table} WHERE mapping_id=?",
                                 (mapping_id,))
                for r in rows:
                    if r["rel_key"] == old_key or _is_under(r["rel_key"], old_key):
                        new_path = moved(r["rel_path"])
                        if table != "cloud_files":  # 目的地若有過期紀錄先移除，避免主鍵衝突
                            self._exec(f"DELETE FROM {table} WHERE mapping_id=? AND rel_key=? AND rowid!=?",
                                       (mapping_id, norm_key(new_path), r["rid"]))
                        self._exec(f"UPDATE {table} SET rel_key=?, rel_path=? WHERE rowid=?",
                                   (norm_key(new_path), new_path, r["rid"]))
                        if table == "cloud_files":
                            self._exec("UPDATE cloud_files SET name_key=? WHERE rowid=?",
                                       (norm_key(rel_name(new_path)), r["rid"]))
                            if new_pid is not None and r["rel_key"] == old_key:
                                self._exec("UPDATE cloud_files SET pid=? WHERE rowid=?", (new_pid, r["rid"]))
            for rv in self.list_reviews("pending", mapping_id):
                if rv.rel_key == old_key or _is_under(rv.rel_key, old_key):
                    self.resolve_review(rv.id, "superseded", status="superseded")

    def cloud_at(self, mapping_id: str, rel_key: str) -> list[CloudRow]:
        rows = self._all("SELECT * FROM cloud_files WHERE mapping_id=? AND rel_key=? ORDER BY mtime DESC",
                         (mapping_id, rel_key))
        return [self._cloud_row(r) for r in rows]

    def cloud_files(self, mapping_id: str) -> list[CloudRow]:
        return [self._cloud_row(r) for r in
                self._all("SELECT * FROM cloud_files WHERE mapping_id=? ORDER BY rel_key", (mapping_id,))]

    def cloud_by_sha1(self, sha1: str) -> list[CloudRow]:
        if not sha1:
            return []
        return [self._cloud_row(r) for r in self._all("SELECT * FROM cloud_files WHERE sha1=?", (sha1.upper(),))]

    def cloud_by_name(self, mapping_id: str, name: str) -> list[CloudRow]:
        rows = self._all("SELECT * FROM cloud_files WHERE mapping_id=? AND name_key=?",
                         (mapping_id, norm_key(name)))
        return [self._cloud_row(r) for r in rows]

    @staticmethod
    def _cloud_row(r: sqlite3.Row) -> CloudRow:
        d = dict(r)
        d.pop("name_key", None)
        return CloudRow(**d)

    def cloud_dir(self, mapping_id: str, rel_path: str) -> str | None:
        r = self._one("SELECT cid FROM cloud_dirs WHERE mapping_id=? AND rel_key=?",
                      (mapping_id, norm_key(rel_path)))
        return r["cid"] if r else None

    def cloud_dirs(self, mapping_id: str) -> dict[str, tuple[str, str]]:
        return {r["rel_key"]: (r["rel_path"], r["cid"]) for r in
                self._all("SELECT * FROM cloud_dirs WHERE mapping_id=?", (mapping_id,))}

    def set_cloud_dir(self, mapping_id: str, rel_path: str, cid: str) -> None:
        self._exec("INSERT OR REPLACE INTO cloud_dirs VALUES(?,?,?,?)",
                   (mapping_id, norm_key(rel_path), rel_path, cid))

    def forget_cloud_dirs(self, mapping_id: str) -> None:
        self._exec("DELETE FROM cloud_dirs WHERE mapping_id=?", (mapping_id,))

    # ---------------------------------------------------------------- 同步基準
    def get_state(self, mapping_id: str, rel_key: str) -> StateRow | None:
        r = self._one("SELECT * FROM sync_state WHERE mapping_id=? AND rel_key=?", (mapping_id, rel_key))
        return StateRow(**dict(r)) if r else None

    def states(self, mapping_id: str) -> dict[str, StateRow]:
        return {r["rel_key"]: StateRow(**dict(r)) for r in
                self._all("SELECT * FROM sync_state WHERE mapping_id=?", (mapping_id,))}

    def set_state(self, mapping_id: str, rel_path: str, h: FileHashes, mtime_ns: int,
                  cloud_fid: str, cloud_sha1: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO sync_state VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mapping_id, norm_key(rel_path), rel_path, h.size, mtime_ns, h.md5, h.sha1,
             cloud_fid, cloud_sha1.upper(), time.time()),
        )

    def delete_state(self, mapping_id: str, rel_key: str) -> None:
        self._exec("DELETE FROM sync_state WHERE mapping_id=? AND rel_key=?", (mapping_id, rel_key))

    def states_under(self, mapping_id: str, rel_path: str) -> list[StateRow]:
        """rel_path 本身（檔案）或其下所有檔案的同步基準。"""
        key = norm_key(rel_path)
        sql, args = _under("SELECT * FROM sync_state WHERE mapping_id=?", mapping_id, rel_path)
        rows = self._all(sql, args)
        if key:
            rows += self._all("SELECT * FROM sync_state WHERE mapping_id=? AND rel_key=?", (mapping_id, key))
        return [StateRow(**dict(r)) for r in rows]

    def states_by_md5(self, md5: str) -> list[StateRow]:
        if not md5:
            return []
        return [StateRow(**dict(r)) for r in self._all("SELECT * FROM sync_state WHERE local_md5=?", (md5.lower(),))]

    # ---------------------------------------------------------------- 待核對
    def add_review(self, mapping_id: str, kind: str, rel_path: str, details: dict, rel_key: str | None = None) -> int:
        key = norm_key(rel_path) if rel_key is None else rel_key
        payload = json.dumps(details, ensure_ascii=False)
        with self.transaction():
            r = self._one("SELECT id FROM reviews WHERE mapping_id=? AND kind=? AND rel_key=? AND status='pending'",
                          (mapping_id, kind, key))
            if r:
                self._exec("UPDATE reviews SET details=?, rel_path=? WHERE id=?", (payload, rel_path, r["id"]))
                return r["id"]
            cur = self._exec(
                "INSERT INTO reviews(mapping_id, kind, rel_key, rel_path, status, details, created_at)"
                " VALUES(?,?,?,?, 'pending', ?, ?)",
                (mapping_id, kind, key, rel_path, payload, time.time()),
            )
            return cur.lastrowid

    def get_review(self, review_id: int) -> Review | None:
        r = self._one("SELECT * FROM reviews WHERE id=?", (review_id,))
        return self._review(r) if r else None

    def list_reviews(self, status: str = "pending", mapping_id: str | None = None,
                     kind: str | None = None) -> list[Review]:
        sql, args = "SELECT * FROM reviews WHERE status=?", [status]
        if mapping_id:
            sql += " AND mapping_id=?"
            args.append(mapping_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id"
        return [self._review(r) for r in self._all(sql, args)]

    def pending_reviews_for(self, mapping_id: str, rel_key: str) -> list[Review]:
        return [self._review(r) for r in self._all(
            "SELECT * FROM reviews WHERE mapping_id=? AND rel_key=? AND status='pending'", (mapping_id, rel_key))]

    def resolve_review(self, review_id: int, decision: str, status: str = "resolved") -> None:
        self._exec("UPDATE reviews SET status=?, decision=?, resolved_at=? WHERE id=?",
                   (status, decision, time.time(), review_id))

    def clear_pending_for(self, mapping_id: str, rel_key: str, except_kinds: Iterable[str] = ()) -> None:
        for rv in self.pending_reviews_for(mapping_id, rel_key):
            if rv.kind not in except_kinds:
                self.resolve_review(rv.id, "superseded", status="superseded")

    def prune_pending(self, mapping_id: str, keep: set[tuple[str, str]], kinds: Iterable[str]) -> int:
        """完整掃描後，把已不成立的待核對項目標為 superseded。"""
        n = 0
        kinds = set(kinds)
        for rv in self.list_reviews("pending", mapping_id):
            if rv.kind in kinds and (rv.kind, rv.rel_key) not in keep:
                self.resolve_review(rv.id, "superseded", status="superseded")
                n += 1
        return n

    def count_pending(self) -> int:
        r = self._one("SELECT COUNT(*) AS n FROM reviews WHERE status='pending'")
        return r["n"] if r else 0

    @staticmethod
    def _review(r: sqlite3.Row) -> Review:
        d = dict(r)
        d["details"] = json.loads(d["details"] or "{}")
        return Review(**d)

    # ---------------------------------------------------------------- 永久忽略
    def add_ignore(self, mapping_id: str, rel_path: str, kind: str = "*") -> None:
        self._exec("INSERT OR REPLACE INTO ignores VALUES(?,?,?,?,?)",
                   (mapping_id, norm_key(rel_path), kind, rel_path, time.time()))

    def remove_ignore(self, mapping_id: str, rel_key: str, kind: str = "*") -> None:
        self._exec("DELETE FROM ignores WHERE mapping_id=? AND rel_key=? AND kind=?", (mapping_id, rel_key, kind))

    def is_ignored(self, mapping_id: str, rel_key: str, kind: str = "*") -> bool:
        r = self._one("SELECT 1 FROM ignores WHERE mapping_id=? AND rel_key=? AND kind IN ('*', ?)",
                      (mapping_id, rel_key, kind))
        return r is not None

    def list_ignores(self, mapping_id: str | None = None) -> list[dict]:
        if mapping_id:
            return [dict(r) for r in self._all("SELECT * FROM ignores WHERE mapping_id=?", (mapping_id,))]
        return [dict(r) for r in self._all("SELECT * FROM ignores")]

    # ---------------------------------------------------------------- 存檔群組
    def get_group(self, mapping_id: str, rel_dir: str) -> SavedataGroup | None:
        r = self._one("SELECT * FROM savedata_groups WHERE mapping_id=? AND rel_key=?",
                      (mapping_id, norm_key(rel_dir)))
        return self._group(r) if r else None

    def upsert_group(self, mapping_id: str, rel_dir: str, status: str | None = None,
                     process_names: list | None = None, last_upload: float | None = None) -> SavedataGroup:
        with self.transaction():
            g = self.get_group(mapping_id, rel_dir) or SavedataGroup(mapping_id, norm_key(rel_dir), rel_dir, "pending")
            if status is not None:
                g.status = status
            if process_names is not None:
                g.process_names = [p.strip() for p in process_names if p.strip()]
            if last_upload is not None:
                g.last_upload = last_upload
            self._exec("INSERT OR REPLACE INTO savedata_groups VALUES(?,?,?,?,?,?)",
                       (mapping_id, g.rel_key, g.rel_dir, g.status, json.dumps(g.process_names), g.last_upload))
            return g

    def list_groups(self, mapping_id: str | None = None) -> list[SavedataGroup]:
        if mapping_id:
            rows = self._all("SELECT * FROM savedata_groups WHERE mapping_id=? ORDER BY rel_key", (mapping_id,))
        else:
            rows = self._all("SELECT * FROM savedata_groups ORDER BY mapping_id, rel_key")
        return [self._group(r) for r in rows]

    @staticmethod
    def _group(r: sqlite3.Row) -> SavedataGroup:
        d = dict(r)
        d["process_names"] = json.loads(d["process_names"] or "[]")
        return SavedataGroup(**d)

    # ---------------------------------------------------------------- 紀錄
    def add_history(self, mapping_id: str, action: str, rel_path: str, ok: bool,
                    instant: bool = False, size: int = 0, detail: str = "") -> None:
        self._exec("INSERT INTO history(ts, mapping_id, action, rel_path, ok, instant, size, detail)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (time.time(), mapping_id, action, rel_path, int(ok), int(instant), size, detail))

    def recent_history(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))]

    def upload_stats(self) -> dict:
        r = self._one(
            "SELECT SUM(CASE WHEN instant=1 THEN 1 ELSE 0 END) AS instant_n,"
            " SUM(CASE WHEN instant=1 THEN size ELSE 0 END) AS instant_bytes,"
            " SUM(CASE WHEN instant=0 THEN 1 ELSE 0 END) AS real_n,"
            " SUM(CASE WHEN instant=0 THEN size ELSE 0 END) AS real_bytes"
            " FROM history WHERE ok=1 AND action LIKE 'upload%'"
        )
        return {k: (r[k] or 0) for k in ("instant_n", "instant_bytes", "real_n", "real_bytes")} if r else {}


def _is_under(key: str, dir_key: str) -> bool:
    return key != dir_key and (dir_key == "" or key.startswith(dir_key + "/"))


def _parent_key(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def _under(sql: str, mapping_id: str, rel_dir: str) -> tuple[str, list]:
    """rel_dir 底下（不含自身）的範圍查詢：'/' 的下一個字元是 '0'，可直接用主鍵範圍。"""
    key = norm_key(rel_dir)
    if not key:
        return sql, [mapping_id]
    return sql + " AND rel_key > ? AND rel_key < ?", [mapping_id, key + "/", key + "0"]


class _Tx:
    """可重入的交易：最外層才 BEGIN/COMMIT。"""

    def __init__(self, db: Database):
        self.db = db
        self._depth = db._tx_depth

    def __enter__(self):
        self.db._lock.acquire()
        depth = getattr(self._depth, "n", 0)
        if depth == 0:
            self.db._conn.execute("BEGIN")
        self._depth.n = depth + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._depth.n -= 1
            if self._depth.n == 0:
                self.db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db._lock.release()
        return False
