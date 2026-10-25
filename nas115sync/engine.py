"""同步引擎：掃描 NAS 與 115、三方比對、執行（秒傳優先）、處理本人核對的決定、即時變動與遊戲存檔同步。"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Callable, Iterator

from .cloud.base import CloudClient, CloudError, NotFoundError, SessionStore, UploadResult
from .compare import (CLOUD_DUPS, ERROR, KIND_LABELS, REGENERATED_KINDS, SAVEDATA_GROUP, DownloadTask, Outcome,
                      Plan, Planner, UploadTask, available_decisions)
from .config import Config, Mapping
from .db import CloudRow, Database, LocalRow
from .hashing import FileHashes, HashCancelled, hash_file
from .paths import (cloud_join, cloud_parts, local_join, long_path, nfc, norm_key, rel_join, rel_name,
                    rel_parent)
from .savedata import any_running
from .transfers import Cancelled, TransferControl, TransferManager

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int, str], None]  # (階段, 完成數, 總數, 說明)
TRASH_STAMP = re.compile(r"^\d{8}-\d{6}$")

__all__ = ["SyncEngine", "ScanError", "Cancelled", "FileChanging", "SKIPPED"]


class ScanError(Exception):
    pass


class FileChanging(Exception):
    """檔案正在被寫入（雜湊期間大小或修改時間改變）。"""


class _Skipped:
    def __repr__(self) -> str:
        return "SKIPPED"


SKIPPED = _Skipped()


def _is_link(entry: os.DirEntry) -> bool:
    try:
        return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())
    except OSError:
        return True


class DbSessionStore(SessionStore):
    """斷點續傳資訊存在資料庫，程式重開後仍可續傳。"""

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> dict | None:
        return self.db.session_get(key)

    def put(self, key: str, data: dict) -> None:
        self.db.session_put(key, data)

    def delete(self, key: str) -> None:
        self.db.session_delete(key)


class SyncEngine:
    def __init__(self, config: Config, db: Database, cloud: CloudClient | None = None,
                 clouds: dict[str, CloudClient] | None = None, transfers: TransferManager | None = None):
        self.config = config
        self.db = db
        self.clouds: dict[str, CloudClient] = dict(clouds or {})
        if cloud is not None:
            self.clouds.setdefault("default", cloud)
        s = config.settings
        self.transfers = transfers or TransferManager(s.upload_limit_kb, s.download_limit_kb)
        self.sessions = DbSessionStore(db)
        self.cancel_event = threading.Event()
        self.listeners: list[Callable[[str, dict], None]] = []
        self.last_plans: list[Plan] = []
        self.full_lock = threading.Lock()  # 完整比對／執行不重疊（排程、定期比對、手動）
        self._dir_lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._busy: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ 共用
    @property
    def settings(self):
        return self.config.settings

    @property
    def cloud(self) -> CloudClient:
        return self.clouds.get("default") or next(iter(self.clouds.values()))

    def client(self, account_id: str) -> CloudClient:
        c = self.clouds.get(account_id or "default")
        if c is None:
            raise CloudError(f"找不到 115 帳號：{account_id}")
        return c

    def cloud_for(self, m: Mapping) -> CloudClient:
        return self.client(m.account_id)

    def planner(self) -> Planner:
        p = Planner(self.db, self.settings)
        p.set_roots(self.config.mappings)
        return p

    def emit(self, event: str, **data) -> None:
        for fn in list(self.listeners):
            try:
                fn(event, data)
            except Exception:  # noqa: BLE001 - 監聽者錯誤不可中斷同步
                log.exception("事件處理失敗：%s", event)

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled("已取消")

    def local_root_ok(self, m: Mapping) -> bool:
        return os.path.isdir(long_path(m.local_path))

    def hash_cached(self, abs_path: str, st: os.stat_result | None = None) -> tuple[FileHashes, os.stat_result]:
        st = st or os.stat(long_path(abs_path))
        h = self.db.get_cached_hash(abs_path, st.st_size, st.st_mtime_ns)
        if h is None:
            try:
                h = hash_file(abs_path, cancel=self.cancel_event)
            except HashCancelled as e:
                raise Cancelled("已取消") from e
            st2 = os.stat(long_path(abs_path))
            if h.size != st.st_size or (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise FileChanging(abs_path)
            self.db.put_cached_hash(abs_path, st.st_mtime_ns, h)
        return h, st

    def _local_row(self, m: Mapping, rel_path: str, h: FileHashes, st: os.stat_result) -> LocalRow:
        return LocalRow(m.id, norm_key(rel_path), rel_path, st.st_size, st.st_mtime_ns, h.md5, h.sha1, h.preid)

    def _fresh_local(self, m: Mapping, rel_path: str) -> LocalRow | None:
        try:
            h, st = self.hash_cached(local_join(m.local_path, rel_path))
        except FileNotFoundError:
            self.db.delete_local(m.id, norm_key(rel_path))
            return None
        lf = self._local_row(m, rel_path, h, st)
        self.db.put_local(lf)
        return lf

    # ------------------------------------------------------------ 掃描 NAS
    def _iter_local(self, m: Mapping, rel_dir: str = "", recursive: bool = True) -> Iterator[tuple]:
        """yield ("file", rel, abs, stat) / ("dir", rel) / ("hidden", rel) / ("bad", rel, error)。"""
        planner = self.planner()
        ignore, filt = planner.ignore, planner.filter_for(m)
        stack = [(rel_dir, local_join(m.local_path, rel_dir))]
        while stack:
            self._check_cancel()
            rel, abs_dir = stack.pop()
            try:
                with os.scandir(long_path(abs_dir)) as it:
                    entries = list(it)
            except OSError as e:
                yield ("bad", rel, e)
                continue
            for entry in entries:
                if ignore.match_name(entry.name) or _is_link(entry):
                    continue
                child_rel = rel_join(rel, entry.name)
                child_abs = os.path.join(abs_dir, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if filt.excludes_dir(child_rel):
                            continue
                        if filt.is_hidden(entry.name, entry.stat(follow_symlinks=False)):
                            yield ("hidden", child_rel)
                            continue
                        yield ("dir", child_rel)
                        if recursive:
                            stack.append((child_rel, child_abs))
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        if not filt.accepts_file(child_rel):
                            continue
                        if filt.is_hidden(entry.name, st):
                            yield ("hidden", child_rel)
                            continue
                        yield ("file", child_rel, child_abs, st)
                except OSError as e:
                    yield ("bad", child_rel, e)

    def scan_local(self, m: Mapping, progress: ProgressFn | None = None):
        """回傳 (NAS 檔案, 資料夾, 無法讀取的資料夾, 略過的隱藏項目鍵值)。"""
        if not self.local_root_ok(m):
            raise ScanError(f"無法存取 NAS 路徑：{m.local_path}（NAS 未開機、未連線或路徑錯誤）")
        found, dirs, bad, hidden = [], [], [], set()
        for item in self._iter_local(m):
            if item[0] == "file":
                found.append(item[1:])
                if progress and len(found) % 500 == 0:
                    progress("掃描 NAS", len(found), 0, m.local_path)
            elif item[0] == "dir":
                dirs.append(item[1])
            elif item[0] == "hidden":
                hidden.add(norm_key(item[1]))
            else:
                if not item[1]:
                    raise ScanError(f"無法讀取 NAS 路徑：{m.local_path}：{item[2]}")
                log.warning("無法讀取 %s：%s", item[1], item[2])
                bad.append(item[1])
        filt = self.planner().filter_for(m)
        total = sum(st.st_size for _, _, st in found)
        done = 0
        rows: list[LocalRow] = []

        def work(item) -> LocalRow:
            rel, abs_path, st = item
            if not filt.accepts_file(rel, st.st_size):  # 超出大小篩選：不必計算雜湊
                return LocalRow(m.id, norm_key(rel), rel, st.st_size, st.st_mtime_ns, "", "", "")
            try:
                h, st = self.hash_cached(abs_path, st)
                return self._local_row(m, rel, h, st)
            except Cancelled:
                raise
            except (OSError, FileChanging) as e:
                log.warning("暫時無法讀取（稍後重試）%s：%s", abs_path, e)
                return LocalRow(m.id, norm_key(rel), rel, st.st_size, st.st_mtime_ns, "", "", "")

        with ThreadPoolExecutor(max(1, self.settings.hash_workers)) as ex:
            futures = [ex.submit(work, it) for it in found]
            try:
                for fut in as_completed(futures):
                    row = fut.result()
                    rows.append(row)
                    done += row.size
                    if progress:
                        progress("計算 MD5/SHA-1", done, total, row.rel_path)
            except Cancelled:
                for f in futures:
                    f.cancel()
                raise
        self.db.replace_local_index(m.id, rows)
        # 被略過的隱藏資料夾也當成「讀不到」保護：略過不等於刪除
        return rows, dirs, bad + sorted(hidden), hidden

    # ------------------------------------------------------------ 掃描 115
    def resolve_root(self, m: Mapping, create: bool = False) -> str:
        c = self.cloud_for(m)
        parts = cloud_parts(m.cloud_path)
        if not parts:
            cid = c.root_cid
        else:
            cid = None
            if m.cloud_cid:
                e = c.get_entry(m.cloud_cid)
                if e and e.is_dir and nfc(e.name) == nfc(parts[-1]):
                    cid = m.cloud_cid
            if cid is None:
                cid = c.resolve_path(m.cloud_path)
            if cid is None and create:
                cid = c.ensure_path(m.cloud_path)
            if cid is None:
                raise ScanError(f"115 路徑不存在：{m.cloud_path}（請重新核對路徑）")
        if cid != m.cloud_cid:
            m.cloud_cid = cid
            self.config.save()
            self.db.forget_cloud_dirs(m.id)
        self.db.set_cloud_dir(m.id, "", cid)
        return cid

    def _cloud_rows(self, m: Mapping, rel: str, cid: str, entries) -> tuple[list[CloudRow], list[tuple[str, str]]]:
        ignore = self.planner().ignore
        files, dirs = [], []
        for e in entries:
            child = rel_join(rel, e.name)
            if ignore.match_rel(child):
                continue
            if e.is_dir:
                dirs.append((child, e.fid))
            else:
                files.append(CloudRow(m.id, e.fid, norm_key(child), child, cid, e.size, e.sha1, e.pick_code, e.mtime))
        return files, dirs

    def _skip_cloud_dir(self, m: Mapping):
        planner = self.planner()
        filt = planner.filter_for(m)
        return lambda rel: planner.ignore.match_rel(rel) or filt.excludes_dir(rel)

    def scan_cloud(self, m: Mapping, progress: ProgressFn | None = None) -> int:
        root = self.resolve_root(m)
        files, dirs = [], [("", root)]
        n = 0
        for rel, cid, entries in self.cloud_for(m).walk(root, skip_dir=self._skip_cloud_dir(m),
                                                        cancel=self.cancel_event):
            f, d = self._cloud_rows(m, rel, cid, entries)
            files += f
            dirs += d
            n += 1
            if progress:
                progress("讀取 115 目錄", n, 0, cloud_join(m.cloud_path, rel))
        self._check_cancel()
        self.db.replace_cloud_index(m.id, files, dirs)
        return len(files)

    def refresh_cloud_dir(self, m: Mapping, rel_dir: str, recursive: bool = False) -> None:
        """重新讀取某個 115 資料夾，避免用過期的索引做判斷。"""
        cid = self.db.cloud_dir(m.id, rel_dir)
        if cid is None:
            return
        c = self.cloud_for(m)
        files, dirs = [], []
        try:
            if recursive:
                for rel, sub_cid, entries in c.walk(cid, rel_dir, skip_dir=self._skip_cloud_dir(m)):
                    f, d = self._cloud_rows(m, rel, sub_cid, entries)
                    files += f
                    dirs += d
            else:
                files, dirs = self._cloud_rows(m, rel_dir, cid, c.list_dir(cid))
        except NotFoundError:
            self.db.forget_cloud_dir_prefix(m.id, rel_dir)
            return
        self.db.replace_cloud_listing(m.id, rel_dir, cid, files, dirs, recursive)

    def ensure_cloud_dir(self, m: Mapping, rel_dir: str) -> str:
        cid = self.db.cloud_dir(m.id, rel_dir)
        if cid:
            return cid
        if not rel_dir:
            return self.resolve_root(m)
        with self._dir_lock:
            cid = self.db.cloud_dir(m.id, rel_dir)
            if cid:
                return cid
            parent = self.ensure_cloud_dir(m, rel_parent(rel_dir))
            cid = self.cloud_for(m).ensure_child_dir(parent, rel_name(rel_dir))
            self.db.set_cloud_dir(m.id, rel_dir, cid)
            return cid

    # ------------------------------------------------------------ 比對
    def analyze(self, mappings: list[Mapping] | None = None, progress: ProgressFn | None = None) -> list[Plan]:
        with self.full_lock:
            return self._analyze(mappings, progress)

    def _analyze(self, mappings, progress) -> list[Plan]:
        ms = mappings if mappings is not None else self.config.active_mappings()
        plans: list[Plan] = []
        scanned = []
        self.db.purge_sessions()
        for m in ms:
            if not m.active:
                continue
            try:
                self.purge_local_trash(m)
                _, dirs, bad, hidden = self.scan_local(m, progress)
                self.scan_cloud(m, progress)
                scanned.append((m, dirs, bad, hidden))
            except (ScanError, CloudError) as e:
                log.error("掃描失敗 %s：%s", m.local_path, e)
                p = Plan(m.id)
                p.warnings.append(str(e))
                plans.append(p)
        planner = self.planner()
        for m, dirs, bad, hidden in scanned:
            p = planner.plan_mapping(m, dirs, bad, hidden)
            self._store_reviews(m, p, prune=True)
            plans.append(p)
        self.last_plans = plans
        self.emit("plans", plans=plans)
        return plans

    def _store_reviews(self, m: Mapping, plan: Plan, prune: bool) -> None:
        keep = set()
        for r in plan.reviews:
            self.db.add_review(m.id, r.kind, r.rel_path, r.details, rel_key=r.key)
            keep.add((r.kind, r.key))
        if prune:
            self.db.prune_pending(m.id, keep, REGENERATED_KINDS)
        self.emit("reviews_changed")

    # ------------------------------------------------------------ 執行
    def execute(self, plans: list[Plan], progress: ProgressFn | None = None) -> dict:
        with self.full_lock:
            return self._execute(plans, progress)

    def _execute(self, plans: list[Plan], progress: ProgressFn | None) -> dict:
        stats = {"uploaded": 0, "instant": 0, "downloaded": 0, "copied": 0, "failed": 0, "skipped": 0,
                 "linked": 0, "deleted": 0, "local_deleted": 0, "mkdirs": 0}
        tasks = []
        for p in plans:
            m = self.config.get_mapping(p.mapping_id)
            if not m or not m.active:
                continue
            for d in p.mkdirs:
                try:
                    self.ensure_cloud_dir(m, d)
                    stats["mkdirs"] += 1
                except CloudError as e:
                    log.error("建立 115 資料夾失敗 %s：%s", d, e)
            for d in p.local_mkdirs:
                try:
                    os.makedirs(long_path(local_join(m.local_path, d)), exist_ok=True)
                    stats["mkdirs"] += 1
                except OSError as e:
                    log.error("建立 NAS 資料夾失敗 %s：%s", d, e)
            for link in p.links:
                self._record_link(m, link.local, link.cloud)
                stats["linked"] += 1
            for key in p.forgets:
                self.db.delete_state(m.id, key)
            for d in p.deletes:
                # 比對後可能已過一段時間：NAS 離線或檔案又出現時不刪除
                if not self.local_root_ok(m) or os.path.exists(long_path(local_join(m.local_path, d.rel_path))):
                    continue
                try:
                    self._delete_cloud(m, d.rel_path, d.fids, d.parent_cid, d.reason)
                    stats["deleted"] += 1
                except CloudError as e:
                    log.error("刪除 115 檔案失敗 %s：%s", d.rel_path, e)
            for d in p.local_deletes:
                try:
                    stats["local_deleted"] += int(self._delete_local(m, d.rel_path, d.expected_sha1, d.reason))
                except OSError as e:
                    log.error("刪除 NAS 檔案失敗 %s：%s", d.rel_path, e)
            tasks += [(m, t) for t in p.uploads] + [(m, t) for t in p.downloads]
        res = self.run_tasks(tasks, progress)
        for k, v in res.items():
            stats[k] = v
        self.last_plans = [p for p in self.last_plans if p not in plans]  # 已執行的計畫不再重複執行
        self.emit("executed", stats=stats)
        return stats

    def run_tasks(self, tasks: list[tuple[Mapping, object]], progress: ProgressFn | None = None) -> dict:
        res = {"uploaded": 0, "instant": 0, "downloaded": 0, "copied": 0, "failed": 0, "skipped": 0}
        total = len(tasks)

        def account(t, r) -> None:
            if r is SKIPPED or r is None:
                res["skipped"] += 1
            elif isinstance(t, DownloadTask):
                res["downloaded"] += 1
                res["copied"] += int(r == "copied")
            else:
                res["uploaded"] += 1
                res["instant"] += int(r.instant)

        remaining = list(tasks)
        uploads = [(m, t) for m, t in tasks if isinstance(t, UploadTask)]
        if self.settings.instant_first and len(uploads) > 1:
            # 第一輪：全部上傳只嘗試秒傳，能秒傳的立刻完成
            remaining = [(m, t) for m, t in tasks if not isinstance(t, UploadTask)]
            for i, (m, t) in enumerate(uploads, 1):
                self._check_cancel()
                try:
                    r = self.upload_task(m, t, instant_only=True)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._fail(m, t, e)
                    res["failed"] += 1
                    continue
                if r is None:
                    remaining.append((m, t))
                else:
                    account(t, r)
                if progress:
                    progress("嘗試秒傳", i, len(uploads), t.rel_path)

        done = total - len(remaining)
        with ThreadPoolExecutor(max(1, self.settings.upload_workers)) as ex:
            futures = {ex.submit(self.download_task if isinstance(t, DownloadTask) else self.upload_task, m, t): (m, t)
                       for m, t in remaining}
            for fut in as_completed(futures):
                m, t = futures[fut]
                done += 1
                try:
                    account(t, fut.result())
                except Cancelled:
                    res["skipped"] += 1
                except Exception as e:  # noqa: BLE001
                    self._fail(m, t, e)
                    res["failed"] += 1
                if progress:
                    progress("傳輸", done, total, t.rel_path)
        return res

    # 相容舊名稱
    run_uploads = run_tasks

    def _claim(self, m: Mapping, rel_path: str) -> tuple[str, str] | None:
        key = (m.id, norm_key(rel_path))
        with self._busy_lock:
            if key in self._busy:
                return None
            self._busy.add(key)
        return key

    def _release(self, key) -> None:
        with self._busy_lock:
            self._busy.discard(key)

    # ------------------------------------------------------------ 上傳
    def upload_task(self, m: Mapping, t: UploadTask, instant_only: bool = False):
        """回傳 UploadResult；instant_only 且無法秒傳時回傳 None；不需上傳時回傳 SKIPPED。"""
        key = self._claim(m, t.rel_path)
        if key is None:
            return SKIPPED
        try:
            return self._upload_task(m, t, instant_only)
        finally:
            self._release(key)

    def _upload_task(self, m: Mapping, t: UploadTask, instant_only: bool):
        self._check_cancel()
        abs_path = local_join(m.local_path, t.rel_path)
        try:
            h, st = self.hash_cached(abs_path)
        except FileNotFoundError:
            log.info("檔案已不存在，略過：%s", abs_path)
            return SKIPPED
        except FileChanging:
            log.info("檔案仍在寫入，稍後再同步：%s", abs_path)
            return SKIPPED
        lf = self._local_row(m, t.rel_path, h, st)
        self.db.put_local(lf)
        parent_rel, name = rel_parent(t.rel_path), rel_name(t.rel_path)

        # 已有相同內容（可能剛被即時同步或別的工作處理過）→ 只建立連結
        cloud_now = self.db.cloud_at(m.id, lf.rel_key)
        same = [c for c in cloud_now if c.sha1 == h.sha1]
        if same:
            self._record_link(m, lf, same[0])
            stale = [f for f in t.replace_fids if f != same[0].fid and any(c.fid == f for c in cloud_now)]
            if stale:
                self._retire_old(m, parent_rel, same[0].pid, name, stale, t.savedata_group is not None)
            return SKIPPED

        cid = self.ensure_cloud_dir(m, parent_rel)
        try:
            res = self._do_upload(m, abs_path, cid, name, h, instant_only, t.rel_path)
        except CloudError:
            if self._dir_valid(m, cid):
                raise
            log.warning("115 資料夾已不存在，重新建立：%s", parent_rel or "/")
            self.db.forget_cloud_dir_prefix(m.id, parent_rel)
            cid = self.ensure_cloud_dir(m, parent_rel)
            res = self._do_upload(m, abs_path, cid, name, h, instant_only, t.rel_path)
        if res is None:
            return None
        self._after_upload(m, t, lf, cid, res)
        return res

    def _dir_valid(self, m: Mapping, cid: str) -> bool:
        try:
            e = self.cloud_for(m).get_entry(cid)
        except CloudError:
            return True
        return e is not None and e.is_dir

    def _do_upload(self, m: Mapping, abs_path: str, cid: str, name: str, h: FileHashes, instant_only: bool,
                   rel_path: str) -> UploadResult | None:
        c = self.cloud_for(m)
        if instant_only:
            res = c.try_instant(abs_path, cid, name, h)
            if res is not None:
                self.transfers.start("upload", m.id, rel_path, h.size).finish("done", instant=True)
            return res
        return self.upload_with_control(c, m.id, rel_path, abs_path, cid, name, h)

    def upload_with_control(self, c: CloudClient, mapping_id: str, rel_path: str, abs_path: str, cid: str,
                            name: str, h: FileHashes) -> UploadResult:
        ctrl = self.transfers.start("upload", mapping_id, rel_path, h.size, self.cancel_event)
        try:
            res = c.upload(abs_path, cid, name, h, control=ctrl, sessions=self.sessions)
        except Cancelled:
            ctrl.finish("cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            ctrl.finish("failed", str(e))
            raise
        ctrl.finish("done", instant=res.instant)
        self.emit("file_progress", rel_path=rel_path, done=h.size, total=h.size)
        return res

    def _after_upload(self, m: Mapping, t: UploadTask, lf: LocalRow, cid: str, res: UploadResult) -> None:
        c = self.cloud_for(m)
        parent_rel, name = rel_parent(t.rel_path), rel_name(t.rel_path)
        if not res.instant and self.settings.verify_uploads:
            try:
                e = c.get_entry(res.fid)
            except CloudError as err:
                e = None
                log.warning("無法向 115 驗證 %s：%s", t.rel_path, err)
            if e is not None and e.sha1 and e.sha1.upper() != lf.sha1.upper():
                raise CloudError(f"驗證失敗：115 上的 SHA-1（{e.sha1}）與 NAS（{lf.sha1}）不符")
        existing = {x.fid for x in self.db.cloud_at(m.id, lf.rel_key)}
        replace = [f for f in t.replace_fids if f != res.fid and f in existing]
        if replace:
            self._retire_old(m, parent_rel, cid, name, replace, t.savedata_group is not None)
            try:  # 115 遇到同名檔可能自動改名，刪除舊版後改回原檔名
                mine = next((e for e in c.list_dir(cid) if e.fid == res.fid), None)
                if mine and nfc(mine.name) != nfc(name):
                    c.rename(res.fid, name)
            except CloudError as e:
                log.warning("確認 115 檔名失敗 %s：%s", t.rel_path, e)
        self.db.put_cloud_file(CloudRow(m.id, res.fid, lf.rel_key, t.rel_path, cid, res.size, res.sha1,
                                        res.pick_code, int(time.time())))
        self.db.set_state(m.id, t.rel_path, lf.hashes, lf.mtime_ns, res.fid, res.sha1)
        self.db.clear_pending_for(m.id, lf.rel_key)
        how = "秒傳" if res.instant else "實際上傳"
        self.db.add_history(m.id, "upload_replace" if replace else "upload", t.rel_path, True, res.instant,
                            res.size, how)
        if t.savedata_group is not None:
            self.db.upsert_group(m.id, t.savedata_group, last_upload=time.time())
        log.info("%s完成：%s%s", how, t.rel_path, f"（已處理 {len(replace)} 個 115 舊版）" if replace else "")
        self.emit("uploaded", mapping_id=m.id, rel_path=t.rel_path, instant=res.instant, size=res.size,
                  replaced=len(replace))

    def _retire_old(self, m: Mapping, parent_rel: str, cid: str, name: str, fids: list[str], savedata: bool) -> None:
        """上傳新版後處理 115 舊版：預設刪除（進 115 回收站）；可設定保留 N 份歷史版本。"""
        c = self.cloud_for(m)
        keep = self.settings.savedata_keep_versions if savedata else self.settings.keep_versions
        if keep > 0:
            vcid = c.ensure_child_dir(cid, self.settings.savedata_versions_dir)
            pat = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?_" + re.escape(nfc(name)) + "$")
            taken = {nfc(e.name) for e in c.list_dir(vcid)}
            stamp = time.strftime("%Y%m%d-%H%M%S")
            c.move(fids, vcid)
            n = 0
            for fid in fids:
                new_name = f"{stamp}_{name}"
                while nfc(new_name) in taken:  # 同一秒內多次存檔
                    n += 1
                    new_name = f"{stamp}-{n}_{name}"
                taken.add(nfc(new_name))
                c.rename(fid, new_name)

            def order(e):
                mt = pat.match(nfc(e.name))
                return mt.group(1), int(mt.group(2) or 0)

            olds = sorted((e for e in c.list_dir(vcid) if not e.is_dir and pat.match(nfc(e.name))),
                          key=order, reverse=True)
            if len(olds) > keep:
                c.delete([e.fid for e in olds[keep:]], vcid)
            detail = f"舊版移到 {self.settings.savedata_versions_dir}（保留 {keep} 份）"
        else:
            c.delete(fids, cid)
            detail = "刪除 115 舊版"
        for fid in fids:
            self.db.remove_cloud_file(m.id, fid)
        self.db.add_history(m.id, "retire_old", rel_join(parent_rel, name), True, detail=detail)

    # ------------------------------------------------------------ 下載
    def download_task(self, m: Mapping, t: DownloadTask):
        """回傳 "downloaded" / "copied"（從 NAS 上相同內容的檔案複製）或 SKIPPED。"""
        key = self._claim(m, t.rel_path)
        if key is None:
            return SKIPPED
        try:
            return self._download_task(m, t)
        finally:
            self._release(key)

    def _download_task(self, m: Mapping, t: DownloadTask):
        self._check_cancel()
        if not self.local_root_ok(m):
            raise ScanError(f"NAS 無法存取：{m.local_path}")
        dest = local_join(m.local_path, t.rel_path)
        lp = long_path(dest)
        key = norm_key(t.rel_path)
        if os.path.exists(lp):
            try:
                lf = self._fresh_local(m, t.rel_path)
            except FileChanging:
                return SKIPPED
            if lf is not None and lf.sha1 == t.sha1.upper():
                cloud = next((c for c in self.db.cloud_at(m.id, key) if c.fid == t.fid), None)
                if cloud:
                    self._record_link(m, lf, cloud)
                return SKIPPED
            if lf is not None and (not t.replace_local or (t.expected_local_sha1 and lf.sha1 != t.expected_local_sha1)):
                log.info("NAS 檔案在比對後被修改，改為重新判斷：%s", t.rel_path)
                self.reconcile_keys(m, [key])
                return SKIPPED
        g = self.db.get_group(m.id, t.savedata_group) if t.savedata_group is not None else None
        if g and g.process_names and any_running(g.process_names):
            log.info("遊戲執行中，延後下載存檔：%s", t.rel_path)
            return SKIPPED

        h, how = self.fetch_to(self.cloud_for(m), m.id, t.rel_path, t.fid, t.pick_code, t.sha1, t.size, dest,
                               keep_old=lambda: self._trash_local(m, t.rel_path))
        st = os.stat(lp)
        self.db.put_cached_hash(dest, st.st_mtime_ns, h)
        lf = self._local_row(m, t.rel_path, h, st)
        self.db.put_local(lf)
        self.db.set_state(m.id, t.rel_path, h, st.st_mtime_ns, t.fid, t.sha1 or h.sha1)
        self.db.clear_pending_for(m.id, key)
        self.db.add_history(m.id, "download", t.rel_path, True, how == "copied", h.size,
                            "從 NAS 相同檔案複製" if how == "copied" else "下載")
        if t.savedata_group is not None:
            self.db.upsert_group(m.id, t.savedata_group, last_upload=time.time())
        log.info("%s完成：%s", "複製" if how == "copied" else "下載", t.rel_path)
        self.emit("downloaded", mapping_id=m.id, rel_path=t.rel_path, size=h.size, copied=how == "copied")
        return how

    def fetch_to(self, c: CloudClient, mapping_id: str, label: str, fid: str, pick_code: str, sha1: str, size: int,
                 dest: str, keep_old: Callable[[], object] | None = None) -> tuple[FileHashes, str]:
        """取得 115 檔案到 dest：先找 NAS 上相同 SHA-1 的檔案直接複製，否則下載（可續傳）；
        完成後驗證 SHA-1，最後才取代目的地檔案（舊檔交給 keep_old 處理）。"""
        lp = long_path(dest)
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        tmp = lp + ".115part"
        ctrl = self.transfers.start("download", mapping_id, label, size, self.cancel_event)
        try:
            how = "copied" if self._copy_local_duplicate(sha1, tmp, ctrl) else "downloaded"
            for attempt in range(2):
                if how == "downloaded":
                    c.download(fid, pick_code, tmp, ctrl)
                h = hash_file(tmp, cancel=self.cancel_event)
                if not sha1 or h.sha1 == sha1.upper():
                    break
                os.remove(tmp)
                how = "downloaded"
                if attempt == 1:
                    raise CloudError(f"下載的檔案 SHA-1 與 115 不符：{label}")
                log.warning("SHA-1 不符，重新下載：%s", label)
            if os.path.exists(lp):
                if keep_old is not None:
                    keep_old()
                else:
                    os.remove(lp)
            os.replace(tmp, lp)
        except (Cancelled, HashCancelled):
            ctrl.finish("cancelled")
            raise Cancelled("已取消") from None
        except Exception as e:  # noqa: BLE001
            ctrl.finish("failed", str(e))
            raise
        ctrl.finish("done", instant=how == "copied")
        return h, how

    def _copy_local_duplicate(self, sha1: str, tmp: str, ctrl: TransferControl) -> bool:
        """NAS 上已有相同 SHA-1 的檔案時直接複製，不必下載（下載版的「秒傳」）。"""
        if not sha1:
            return False
        for x in self.db.local_by_hash(sha1):
            if x.sha1 != sha1.upper():
                continue
            m2 = self.config.get_mapping(x.mapping_id)
            if m2 is None:
                continue
            src = local_join(m2.local_path, x.rel_path)
            try:
                h, _ = self.hash_cached(src)
                if h.sha1 != sha1.upper():
                    continue
                with open(long_path(src), "rb") as fi, open(tmp, "wb") as fo:
                    done = 0
                    while True:
                        ctrl.check()
                        buf = fi.read(4 * 1024 * 1024)
                        if not buf:
                            break
                        fo.write(buf)
                        done += len(buf)
                        ctrl.set_done(done)
                log.info("從 NAS 上相同內容的檔案複製，免下載：%s", src)
                return True
            except (OSError, FileChanging):
                continue
        return False

    # ------------------------------------------------------------ NAS 端可復原刪除
    def _trash_local(self, m: Mapping, rel_path: str) -> str:
        """把 NAS 檔案移到對應根目錄下的可復原資料夾（以時間分組，定期清除）。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = local_join(m.local_path, f"{self.settings.local_trash_dir}/{stamp}/{rel_path}")
        base, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(long_path(dest)):
            dest = f"{base} ({n}){ext}"
            n += 1
        os.makedirs(long_path(os.path.dirname(dest)), exist_ok=True)
        os.replace(long_path(local_join(m.local_path, rel_path)), long_path(dest))
        self.db.delete_local(m.id, norm_key(rel_path))
        return dest

    def purge_local_trash(self, m: Mapping) -> None:
        root = long_path(local_join(m.local_path, self.settings.local_trash_dir))
        if not os.path.isdir(root):
            return
        limit = time.time() - max(1, self.settings.local_trash_days) * 86400
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if TRASH_STAMP.match(name) and os.path.isdir(path):
                try:
                    if time.mktime(time.strptime(name, "%Y%m%d-%H%M%S")) < limit:
                        shutil.rmtree(path, ignore_errors=True)
                except ValueError:
                    pass

    def _delete_local(self, m: Mapping, rel_path: str, expected_sha1: str, reason: str) -> bool:
        """115 端刪除 → NAS 端「移到可復原資料夾」。內容與預期不同（剛被修改）就不刪除。"""
        key = norm_key(rel_path)
        if not self.local_root_ok(m):
            return False
        abs_path = local_join(m.local_path, rel_path)
        if not os.path.exists(long_path(abs_path)):
            self.db.delete_state(m.id, key)
            return False
        try:
            h, _ = self.hash_cached(abs_path)
        except FileChanging:
            return False
        if expected_sha1 and h.sha1 != expected_sha1.upper():
            log.info("NAS 檔案已被修改，不刪除：%s", rel_path)
            self.reconcile_keys(m, [key])
            return False
        if self.db.cloud_at(m.id, key):
            return False  # 115 上又出現了
        dest = self._trash_local(m, rel_path)
        self.db.delete_state(m.id, key)
        self.db.add_history(m.id, "delete_local", rel_path, True, detail=f"{reason}：移到 {dest}")
        log.info("已將 NAS 檔案移到可復原資料夾（%s）：%s", reason, rel_path)
        self.emit("deleted_local", mapping_id=m.id, rel_path=rel_path)
        return True

    # ------------------------------------------------------------ 其他動作
    def _record_link(self, m: Mapping, lf: LocalRow, c: CloudRow) -> None:
        self.db.set_state(m.id, lf.rel_path, lf.hashes, lf.mtime_ns, c.fid, c.sha1)
        self.db.clear_pending_for(m.id, lf.rel_key, except_kinds=[CLOUD_DUPS])

    def _delete_cloud(self, m: Mapping, rel_path: str, fids: list[str], parent_cid: str, reason: str) -> None:
        self.cloud_for(m).delete(fids, parent_cid)
        for f in fids:
            self.db.remove_cloud_file(m.id, f)
        self.db.delete_state(m.id, norm_key(rel_path))
        self.db.add_history(m.id, "delete_cloud", rel_path, True, detail=reason)
        log.info("已刪除 115 檔案（%s）：%s", reason, rel_path)
        self.emit("deleted", mapping_id=m.id, rel_path=rel_path)

    def _fail(self, m: Mapping, t, exc: Exception) -> None:
        down = isinstance(t, DownloadTask)
        word = "下載" if down else "上傳"
        log.error("%s失敗 %s：%s", word, t.rel_path, exc)
        self.db.add_review(m.id, ERROR, t.rel_path, {
            "message": f"{word}失敗：{exc}", "task": asdict(t), "task_type": "download" if down else "upload",
            "local_root": m.local_path, "cloud_root": m.cloud_path, "direction": m.direction,
            "local": {"path": local_join(m.local_path, t.rel_path), "rel_path": t.rel_path},
        })
        self.db.add_history(m.id, "download" if down else "upload", t.rel_path, False, detail=str(exc)[:500])
        self.emit("failed", mapping_id=m.id, rel_path=t.rel_path, error=str(exc))
        self.emit("reviews_changed")

    # ------------------------------------------------------------ 單檔處理（即時同步與核對共用）
    def reconcile_files(self, m: Mapping, rows: list[LocalRow]) -> None:
        planner = self.planner()
        self.reconcile_outcomes(m, [planner.classify(m, lf) for lf in rows], planner)

    def reconcile_keys(self, m: Mapping, keys) -> None:
        planner = self.planner()
        outs = [planner.classify_key(m, self.db.local_at(m.id, k), self.db.cloud_at(m.id, k),
                                     self.db.get_state(m.id, k)) for k in keys]
        self.reconcile_outcomes(m, outs, planner)

    def reconcile_outcomes(self, m: Mapping, outs: list[Outcome], planner: Planner | None = None) -> None:
        planner = planner or self.planner()
        groups: set[str] = set()
        gone_local, gone_cloud = [], []
        for out in outs:
            k = out.kind
            if k == "group_pending":
                groups.add(out.group)
                continue
            if out.link:
                self._record_link(m, out.link.local, out.link.cloud)
            if k in ("identical", "in_sync") and out.link is None and out.local is not None:
                self.db.clear_pending_for(m.id, out.local.rel_key, except_kinds=[CLOUD_DUPS])
            if out.review:
                self._add_review(m, out.review)
            if k == "forget" and out.state:
                self.db.delete_state(m.id, out.state.rel_key)
            elif k == "local_gone":
                gone_local.append(out)
            elif k == "cloud_gone":
                gone_cloud.append(out)
            if out.upload:
                self._run_now(m, out.upload)
            if out.download:
                self._run_now(m, out.download)
        if gone_local or gone_cloud:
            plan = Plan(m.id)
            total = len(self.db.states(m.id))
            planner.resolve_deletions(m, plan, gone_local, "local", total)
            planner.resolve_deletions(m, plan, gone_cloud, "cloud", total)
            for r in plan.reviews:
                self._add_review(m, r)
            for key in plan.forgets:
                self.db.delete_state(m.id, key)
            for d in plan.deletes:
                try:
                    self._delete_cloud(m, d.rel_path, d.fids, d.parent_cid, d.reason)
                except CloudError as e:
                    log.error("刪除 115 檔案失敗 %s：%s", d.rel_path, e)
            for d in plan.local_deletes:
                self._delete_local(m, d.rel_path, d.expected_sha1, d.reason)
        for group in groups:
            self._ensure_group_review(m, group)

    def _add_review(self, m: Mapping, r) -> None:
        self.db.clear_pending_for(m.id, r.key, except_kinds=[r.kind, CLOUD_DUPS])
        self.db.add_review(m.id, r.kind, r.rel_path, r.details, rel_key=r.key)
        self.emit("reviews_changed")

    def _run_now(self, m: Mapping, t) -> None:
        try:
            if isinstance(t, DownloadTask):
                self.download_task(m, t)
            else:
                self.upload_task(m, t)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            self._fail(m, t, e)

    def _ensure_group_review(self, m: Mapping, group: str) -> None:
        planner = self.planner()
        items = [r for r in (self.db.local_under(m.id, group) if group else self.db.local_files(m.id))
                 if planner.detector.group_of(r.rel_path, m.savedata) == group]
        if not items:
            items = [c for c in self.db.cloud_under(m.id, group)
                     if planner.detector.group_of(c.rel_path, m.savedata) == group]
        self.db.upsert_group(m.id, group)
        self._add_review(m, planner.group_review(m, group, items))

    def _index_local_tree(self, m: Mapping, rel_dir: str, recursive: bool = True) -> list[LocalRow]:
        rows = []
        for item in self._iter_local(m, rel_dir, recursive):
            if item[0] != "file":
                continue
            _, rel, abs_path, st = item
            try:
                h, st = self.hash_cached(abs_path, st)
            except (OSError, FileChanging) as e:
                log.info("暫時無法讀取 %s：%s", abs_path, e)
                continue
            lf = self._local_row(m, rel, h, st)
            self.db.put_local(lf)
            rows.append(lf)
        return rows

    def handle_local_change(self, m: Mapping, rel_path: str) -> None:
        """NAS 上的檔案或資料夾新增/修改。"""
        abs_path = local_join(m.local_path, rel_path)
        lp = long_path(abs_path)
        if not os.path.exists(lp):
            return self.handle_local_delete(m, rel_path)
        planner = self.planner()
        if planner.ignore.match_rel(rel_path):
            return
        if os.path.isdir(lp):
            if planner.filter_for(m).excludes_dir(rel_path):
                return
            group, status = planner.savedata_status(m, rel_path + "/x")
            if m.can_upload and (group is None or status in ("approved", "normal")):
                self.ensure_cloud_dir(m, rel_path)
            # 整個資料夾被搬進來時 Windows 只回報資料夾本身，需掃描其內容
            rows = self._index_local_tree(m, rel_path)
            self.refresh_cloud_dir(m, rel_path, recursive=True)
            self.reconcile_files(m, rows)
            return
        h, st = self.hash_cached(abs_path)
        lf = self._local_row(m, rel_path, h, st)
        self.db.put_local(lf)
        self.refresh_cloud_dir(m, rel_parent(rel_path))
        self.reconcile_files(m, [lf])

    def handle_local_delete(self, m: Mapping, rel_path: str) -> None:
        if not self.local_root_ok(m):
            log.warning("NAS 根目錄無法存取，忽略刪除事件：%s", rel_path)
            return
        if os.path.exists(long_path(local_join(m.local_path, rel_path))):
            return self.handle_local_change(m, rel_path)  # 例如存檔以「刪除再寫入」方式更新
        self.db.delete_local_under(m.id, rel_path)
        states = self.db.states_under(m.id, rel_path)
        if not states:
            return
        planner = self.planner()
        if len(states) >= self.settings.mass_delete_guard_min:
            self._add_review(m, planner.mass_delete_review(m, [st.rel_path for st in states],
                                                           norm_key(rel_path) + "/*", len(self.db.states(m.id))))
            return
        outs = [planner.classify_key(m, None, self.db.cloud_at(m.id, st.rel_key), st) for st in states]
        self.reconcile_outcomes(m, outs, planner)

    def handle_local_move(self, m: Mapping, src_rel: str, dest_rel: str) -> None:
        """NAS 上搬移/改名：已同步的檔案或資料夾直接在 115 搬移/改名，不必重新上傳。"""
        dest_lp = long_path(local_join(m.local_path, dest_rel))
        if not os.path.exists(dest_lp):
            return self.handle_local_delete(m, src_rel)
        planner = self.planner()
        is_dir = os.path.isdir(dest_lp)
        probe = (lambda r: r + "/x") if is_dir else (lambda r: r)
        simple = (self.settings.auto_update_linked and m.can_upload
                  and planner.savedata_status(m, probe(src_rel))[0] is None
                  and planner.savedata_status(m, probe(dest_rel))[0] is None
                  and not planner.ignore.match_rel(src_rel) and not planner.ignore.match_rel(dest_rel))
        if simple:
            try:
                if is_dir:
                    src_cid = self.db.cloud_dir(m.id, src_rel)
                    if src_cid and self.db.cloud_dir(m.id, dest_rel) is None:
                        self._cloud_move(m, src_cid, src_rel, dest_rel)
                        self.db.rename_prefix(m.id, src_rel, dest_rel)
                        self.db.add_history(m.id, "move", dest_rel, True, detail=f"由 {src_rel}")
                        self.reconcile_files(m, self._index_local_tree(m, dest_rel))
                        return
                else:
                    st = self.db.get_state(m.id, norm_key(src_rel))
                    base = [c for c in self.db.cloud_at(m.id, norm_key(src_rel)) if st and c.sha1 == st.cloud_sha1]
                    if st and base and not self.db.cloud_at(m.id, norm_key(dest_rel)):
                        to_cid = self._cloud_move(m, base[0].fid, src_rel, dest_rel)
                        self.db.rename_prefix(m.id, src_rel, dest_rel, new_pid=to_cid)
                        self.db.add_history(m.id, "move", dest_rel, True, detail=f"由 {src_rel}")
                        self.handle_local_change(m, dest_rel)
                        return
            except CloudError as e:
                log.warning("115 搬移失敗，改用刪除＋上傳：%s → %s：%s", src_rel, dest_rel, e)
        self.handle_local_delete(m, src_rel)
        self.handle_local_change(m, dest_rel)

    def _cloud_move(self, m: Mapping, fid: str, src_rel: str, dest_rel: str) -> str:
        c = self.cloud_for(m)
        to_cid = self.ensure_cloud_dir(m, rel_parent(dest_rel))
        if norm_key(rel_parent(src_rel)) != norm_key(rel_parent(dest_rel)):
            c.move([fid], to_cid)
        if nfc(rel_name(src_rel)) != nfc(rel_name(dest_rel)):
            c.rename(fid, rel_name(dest_rel))
        return to_cid

    # ------------------------------------------------------------ 遊戲存檔（Steam 雲存檔式）
    def savedata_status(self, m: Mapping, rel_path: str) -> tuple[str | None, str | None]:
        return self.planner().savedata_status(m, rel_path)

    def sync_savedata_group(self, m: Mapping, group: str) -> None:
        """把整個存檔群組與 115 對齊：新/改的存檔上傳並處理 115 舊版；雙向時也下載別台電腦上傳的新存檔；
        刪除依設定同步。遊戲執行中不動作。"""
        if not self.local_root_ok(m):
            log.warning("NAS 無法存取，延後同步存檔：%s", group)
            return
        g = self.db.get_group(m.id, group)
        if g and g.process_names and any_running(g.process_names):
            log.info("遊戲執行中，延後同步存檔：%s", group)
            return
        planner = self.planner()
        in_group = lambda rel: planner.detector.group_of(rel, m.savedata) == group  # noqa: E731
        recursive = planner.detector.is_group_dir(group, m.savedata)
        rows = [r for r in self._index_local_tree(m, group, recursive) if in_group(r.rel_path)]
        present = {r.rel_key for r in rows}
        for stale in self.db.local_under(m.id, group) if group else self.db.local_files(m.id):
            if in_group(stale.rel_path) and stale.rel_key not in present:
                self.db.delete_local(m.id, stale.rel_key)
        if m.can_upload:
            self.ensure_cloud_dir(m, group)
        self.refresh_cloud_dir(m, group, recursive=recursive)
        keys = set(present)
        keys |= {c.rel_key for c in self.db.cloud_under(m.id, group) if in_group(c.rel_path)}
        keys |= {st.rel_key for st in self.db.states_under(m.id, group) if in_group(st.rel_path)}
        self.reconcile_keys(m, sorted(keys))
        self.db.upsert_group(m.id, group, last_upload=time.time())
        self.emit("savedata_synced", mapping_id=m.id, group=group)

    # ------------------------------------------------------------ 本人核對的決定
    def apply_review(self, review_id: int, decision: str, extra: dict | None = None) -> str:
        rv = self.db.get_review(review_id)
        if rv is None or rv.status != "pending":
            raise ValueError("此核對項目已處理或已失效")
        m = self.config.get_mapping(rv.mapping_id)
        if m is None:
            self.db.resolve_review(rv.id, "obsolete", status="superseded")
            return "對應已刪除，項目作廢"
        if decision not in {d for d, _ in available_decisions(rv.kind, rv.details)}:
            raise ValueError(f"「{KIND_LABELS.get(rv.kind, rv.kind)}」不支援的決定：{decision}")
        if decision == "skip":
            msg = "已暫時略過（下次完整比對會再提示）"
        elif decision == "ignore":
            self.db.add_ignore(m.id, rv.rel_path)
            msg = "已永久忽略此檔"
        else:
            msg = getattr(self, f"_rv_{rv.kind}")(m, rv, decision, extra or {})
        self.db.resolve_review(rv.id, decision)
        self.db.add_history(m.id, f"review:{decision}", rv.rel_path, True, detail=KIND_LABELS.get(rv.kind, ""))
        self.emit("reviews_changed")
        return msg

    def _approved_group(self, m: Mapping, rel_path: str) -> str | None:
        group, status = self.savedata_status(m, rel_path)
        return group if status == "approved" else None

    def _must_upload(self, m: Mapping, t: UploadTask) -> None:
        if self.upload_task(m, t) is None:
            raise CloudError("上傳未完成")

    def _must_download(self, m: Mapping, c: CloudRow, replace: bool, reason: str = "review") -> None:
        t = DownloadTask(m.id, c.rel_path, c.fid, c.pick_code, c.sha1, c.size, reason, replace_local=replace,
                         savedata_group=self._approved_group(m, c.rel_path))
        if self.download_task(m, t) is SKIPPED and not self._in_sync(m, c.rel_path):
            raise CloudError("下載未完成（NAS 檔案可能正在使用或剛被修改）")

    def _in_sync(self, m: Mapping, rel_path: str) -> bool:
        st = self.db.get_state(m.id, norm_key(rel_path))
        lf = self.db.local_at(m.id, norm_key(rel_path))
        return bool(st and lf and st.local_sha1 == lf.sha1)

    def _rv_conflict(self, m, rv, decision, extra) -> str:
        lf = self._fresh_local(m, rv.rel_path)
        parent = rel_parent(rv.rel_path)
        self.refresh_cloud_dir(m, parent)
        cloud = self.db.cloud_at(m.id, norm_key(rv.rel_path))
        group = self._approved_group(m, rv.rel_path)
        if decision.startswith("download"):
            if not cloud:
                raise ValueError("115 上已沒有這個檔案")
            if lf is not None and decision == "download_keep_both":
                stem, ext = os.path.splitext(lf.rel_path)
                new_rel = f"{stem} (NAS舊版 {time.strftime('%Y%m%d-%H%M%S')}){ext}"
                os.replace(long_path(local_join(m.local_path, lf.rel_path)),
                           long_path(local_join(m.local_path, new_rel)))
                self.db.delete_local(m.id, lf.rel_key)
                self._must_download(m, cloud[0], replace=False)
                return f"NAS 舊版已改名為 {rel_name(new_rel)}，115 版本已下載"
            self._must_download(m, cloud[0], replace=True)
            return "已下載 115 版本（NAS 舊版已移到可復原資料夾）"
        if lf is None:
            return "NAS 檔案已不存在，未做任何變更"
        if decision == "keep_cloud":
            if not cloud:
                return "115 上已沒有這個檔案"
            self.db.set_state(m.id, lf.rel_path, lf.hashes, lf.mtime_ns, cloud[0].fid, cloud[0].sha1)
            return "已維持兩邊現狀；之後任一邊再修改時才會同步"
        c = self.cloud_for(m)
        if decision == "upload_keep_both":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for i, x in enumerate([x for x in cloud if x.sha1 != lf.sha1]):
                stem, ext = os.path.splitext(x.name)
                c.rename(x.fid, f"{stem} (115舊版 {stamp}{f'-{i}' if i else ''}){ext}")
            self.refresh_cloud_dir(m, parent)
            self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], group, lf.size))
            return "115 舊版已改名保留，NAS 版本已上傳"
        replace = [x.fid for x in cloud if x.sha1 != lf.sha1]
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", replace, group, lf.size))
        return "已上傳 NAS 版本並處理 115 舊版"

    _rv_cloud_changed = _rv_conflict

    def _rv_cloud_deleted(self, m, rv, decision, extra) -> str:
        if decision == "forget":
            self.db.add_ignore(m.id, rv.rel_path)
            self.db.delete_state(m.id, norm_key(rv.rel_path))
            return "之後不再同步此檔"
        if decision == "delete_local":
            if not self.local_root_ok(m):
                raise ValueError("NAS 目前無法存取")
            if not os.path.exists(long_path(local_join(m.local_path, rv.rel_path))):
                self.db.delete_state(m.id, norm_key(rv.rel_path))
                return "NAS 檔案已不存在"
            dest = self._trash_local(m, rv.rel_path)
            self.db.delete_state(m.id, norm_key(rv.rel_path))
            self.db.add_history(m.id, "delete_local", rv.rel_path, True, detail=f"本人確認：移到 {dest}")
            return f"已移到可復原資料夾：{dest}"
        lf = self._fresh_local(m, rv.rel_path)
        if lf is None:
            return "NAS 檔案已不存在"
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], self._approved_group(m, rv.rel_path),
                                        lf.size))
        return "已重新上傳"

    def _rv_duplicate_hash(self, m, rv, decision, extra) -> str:
        if decision == "download":
            cloud = self.db.cloud_at(m.id, norm_key(rv.rel_path))
            if not cloud:
                raise ValueError("115 上已沒有這個檔案，請重新比對")
            self._must_download(m, cloud[0], replace=False)
            return "已取得檔案（優先從 NAS 上相同內容的檔案複製）"
        lf = self._fresh_local(m, rv.rel_path)
        if lf is None:
            return "NAS 檔案已不存在"
        if decision == "move_cloud":
            mc = rv.details["move_candidate"]
            from_rel, fid = mc["from_rel"], mc["fid"]
            if os.path.exists(long_path(local_join(m.local_path, from_rel))):
                raise ValueError(f"原檔仍存在於 NAS：{from_rel}，不能視為搬移")
            if not any(c.fid == fid for c in self.db.cloud_at(m.id, norm_key(from_rel))):
                raise ValueError("115 上的原檔已不在原位置，請重新比對")
            to_cid = self._cloud_move(m, fid, from_rel, rv.rel_path)
            self.db.rename_prefix(m.id, from_rel, rv.rel_path, new_pid=to_cid)
            self.db.set_state(m.id, rv.rel_path, lf.hashes, lf.mtime_ns, fid, lf.sha1)
            self.db.add_history(m.id, "move", rv.rel_path, True, detail=f"由 {from_rel}")
            return f"已在 115 將 {from_rel} 搬移到 {rv.rel_path}"
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], None, lf.size))
        return "已上傳"

    def _rv_same_name(self, m, rv, decision, extra) -> str:
        lf = self._fresh_local(m, rv.rel_path)
        if lf is None:
            return "NAS 檔案已不存在"
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], None, lf.size))
        return "已上傳"

    def _rv_cloud_duplicates(self, m, rv, decision, extra) -> str:
        if decision == "keep":
            self.db.add_ignore(m.id, rv.rel_path, CLOUD_DUPS)
            return "已保留，不再提示"
        existing = {c.fid: c for c in self.db.cloud_at(m.id, rv.rel_key)}
        fids = [f for f in rv.details.get("extra_fids", []) if f in existing]
        if fids:
            self.cloud_for(m).delete(fids, existing[fids[0]].pid)
            for f in fids:
                self.db.remove_cloud_file(m.id, f)
        return f"已刪除 {len(fids)} 個多餘的同名檔"

    def _resolve_local_deleted(self, m: Mapping, rel_path: str, decision: str) -> bool:
        """NAS 刪除的檔案：delete_cloud / download（還原）/ keep_cloud（不再同步）。"""
        if os.path.exists(long_path(local_join(m.local_path, rel_path))):
            return False
        key = norm_key(rel_path)
        st = self.db.get_state(m.id, key)
        cloud = self.db.cloud_at(m.id, key)
        if decision == "keep_cloud":
            self.db.delete_state(m.id, key)
            return True
        if decision == "download":
            if not cloud:
                return False
            self._must_download(m, cloud[0], replace=False, reason="restore")
            return True
        base = [c for c in cloud if st and c.sha1 == st.cloud_sha1] or cloud
        if base:
            self._delete_cloud(m, rel_path, [c.fid for c in base], base[0].pid, "本人確認")
        else:
            self.db.delete_state(m.id, key)
        return True

    def _rv_local_deleted(self, m, rv, decision, extra) -> str:
        if not self.local_root_ok(m):
            raise ValueError("NAS 目前無法存取，請確認 NAS 連線後再處理")
        if not self._resolve_local_deleted(m, rv.rel_path, decision):
            return "檔案已重新出現在 NAS 或已處理，未做變更"
        return {"delete_cloud": "已刪除 115 上的檔案（可在 115 回收站復原）", "download": "已從 115 還原到 NAS",
                "keep_cloud": "已保留 115 上的檔案"}[decision]

    def _rv_mass_delete(self, m, rv, decision, extra) -> str:
        if not self.local_root_ok(m):
            raise ValueError("NAS 目前無法存取，請確認 NAS 連線後再處理")
        n = 0
        side = rv.details.get("side", "local")
        for p in rv.details.get("paths", []):
            if side == "local":
                n += self._resolve_local_deleted(m, p, "delete_cloud" if decision == "delete_all" else "keep_cloud")
            else:
                st = self.db.get_state(m.id, norm_key(p))
                if decision == "delete_all":
                    n += self._delete_local(m, p, st.local_sha1 if st else "", "本人確認")
                else:
                    self.db.delete_state(m.id, norm_key(p))
                    n += 1
        return f"已處理 {n} 個檔案"

    def change_savedata_group(self, m: Mapping, group: str, decision: str, process_names: list | None = None) -> str:
        """從「遊戲存檔」頁變更群組狀態；有待核對項目時一併結案。"""
        for rv in self.db.pending_reviews_for(m.id, norm_key(group)):
            if rv.kind == SAVEDATA_GROUP:
                return self.apply_review(rv.id, decision, {"process_names": process_names})
        return self._set_group_status(m, group, decision, process_names)

    def _rv_savedata_group(self, m, rv, decision, extra) -> str:
        return self._set_group_status(m, rv.rel_path, decision, extra.get("process_names"))

    def _set_group_status(self, m: Mapping, group: str, decision: str, process_names: list | None) -> str:
        if decision == "approve":
            self.db.upsert_group(m.id, group, "approved", process_names=process_names)
            self.sync_savedata_group(m, group)
            return "已核可，存檔將自動同步"
        if decision == "normal":
            self.db.upsert_group(m.id, group, "normal")
            self.refresh_cloud_dir(m, group, recursive=True)
            keys = {r.rel_key for r in (self.db.local_under(m.id, group) if group else self.db.local_files(m.id))}
            keys |= {c.rel_key for c in self.db.cloud_under(m.id, group)}
            self.reconcile_keys(m, sorted(keys))
            return "已改為一般檔案處理"
        self.db.upsert_group(m.id, group, "exclude")
        return "此存檔資料夾將不同步"

    def _rv_error(self, m, rv, decision, extra) -> str:
        if rv.details.get("task_type") == "download":
            t = DownloadTask(**rv.details["task"])
            if self.download_task(m, t) is SKIPPED and not self._in_sync(m, t.rel_path):
                raise CloudError("下載未完成")
        else:
            self._must_upload(m, UploadTask(**rv.details["task"]))
        return "重試成功"
