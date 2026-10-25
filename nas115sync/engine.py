"""同步引擎：掃描 NAS 與 115、比對、執行（秒傳優先）、處理本人核對的決定、即時變動與遊戲存檔同步。"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Callable, Iterator

from .cloud.base import CloudClient, CloudError, NotFoundError, UploadResult
from .compare import (CLOUD_DUPS, ERROR, KIND_LABELS, REGENERATED_KINDS, SAVEDATA_GROUP, Plan, Planner,
                      UploadTask, available_decisions)
from .config import Config, Mapping
from .db import CloudRow, Database, LocalRow
from .hashing import FileHashes, HashCancelled, hash_file
from .paths import (cloud_join, cloud_parts, local_join, long_path, nfc, norm_key, rel_join, rel_name,
                    rel_parent)

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int, str], None]  # (階段, 完成數, 總數, 說明)


class ScanError(Exception):
    pass


class Cancelled(Exception):
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


class SyncEngine:
    def __init__(self, config: Config, db: Database, cloud: CloudClient):
        self.config = config
        self.db = db
        self.cloud = cloud
        self.cancel_event = threading.Event()
        self.listeners: list[Callable[[str, dict], None]] = []
        self.last_plans: list[Plan] = []
        self._dir_lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._busy: set[tuple[str, str]] = set()

    # ------------------------------------------------------------ 共用
    @property
    def settings(self):
        return self.config.settings

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
            h = hash_file(abs_path, cancel=self.cancel_event)
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
            return None
        lf = self._local_row(m, rel_path, h, st)
        self.db.put_local(lf)
        return lf

    # ------------------------------------------------------------ 掃描 NAS
    def _iter_local(self, m: Mapping, rel_dir: str = "", recursive: bool = True) -> Iterator[tuple]:
        """yield ("file", rel, abs, stat) / ("dir", rel) / ("bad", rel, error)。"""
        ignore = self.planner().ignore
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
                        yield ("dir", child_rel)
                        if recursive:
                            stack.append((child_rel, child_abs))
                    elif entry.is_file(follow_symlinks=False):
                        yield ("file", child_rel, child_abs, entry.stat(follow_symlinks=False))
                except OSError as e:
                    yield ("bad", child_rel, e)

    def scan_local(self, m: Mapping, progress: ProgressFn | None = None) -> tuple[list[LocalRow], list[str], list[str]]:
        if not self.local_root_ok(m):
            raise ScanError(f"無法存取 NAS 路徑：{m.local_path}（NAS 未開機、未連線或路徑錯誤）")
        found, dirs, bad = [], [], []
        for item in self._iter_local(m):
            if item[0] == "file":
                found.append(item[1:])
                if progress and len(found) % 500 == 0:
                    progress("掃描 NAS", len(found), 0, m.local_path)
            elif item[0] == "dir":
                dirs.append(item[1])
            else:
                if not item[1]:
                    raise ScanError(f"無法讀取 NAS 路徑：{m.local_path}：{item[2]}")
                log.warning("無法讀取 %s：%s", item[1], item[2])
                bad.append(item[1])

        total = sum(st.st_size for _, _, st in found)
        done = 0
        rows: list[LocalRow] = []

        def work(item) -> LocalRow:
            rel, abs_path, st = item
            try:
                h, st = self.hash_cached(abs_path, st)
                return self._local_row(m, rel, h, st)
            except (HashCancelled, Cancelled):
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
            except HashCancelled as e:
                for f in futures:
                    f.cancel()
                raise Cancelled("已取消") from e
        self.db.replace_local_index(m.id, rows)
        return rows, dirs, bad

    # ------------------------------------------------------------ 掃描 115
    def resolve_root(self, m: Mapping, create: bool = False) -> str:
        parts = cloud_parts(m.cloud_path)
        if not parts:
            cid = self.cloud.root_cid
        else:
            cid = None
            if m.cloud_cid:
                e = self.cloud.get_entry(m.cloud_cid)
                if e and e.is_dir and nfc(e.name) == nfc(parts[-1]):
                    cid = m.cloud_cid
            if cid is None:
                cid = self.cloud.resolve_path(m.cloud_path)
            if cid is None and create:
                cid = self.cloud.ensure_path(m.cloud_path)
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

    def scan_cloud(self, m: Mapping, progress: ProgressFn | None = None) -> int:
        root = self.resolve_root(m)
        ignore = self.planner().ignore
        files, dirs = [], [("", root)]
        n = 0
        for rel, cid, entries in self.cloud.walk(root, skip_dir=ignore.match_rel, cancel=self.cancel_event):
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
        files, dirs = [], []
        try:
            if recursive:
                for rel, sub_cid, entries in self.cloud.walk(cid, rel_dir, skip_dir=self.planner().ignore.match_rel):
                    f, d = self._cloud_rows(m, rel, sub_cid, entries)
                    files += f
                    dirs += d
            else:
                files, dirs = self._cloud_rows(m, rel_dir, cid, self.cloud.list_dir(cid))
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
            cid = self.cloud.ensure_child_dir(parent, rel_name(rel_dir))
            self.db.set_cloud_dir(m.id, rel_dir, cid)
            return cid

    # ------------------------------------------------------------ 比對
    def analyze(self, mappings: list[Mapping] | None = None, progress: ProgressFn | None = None) -> list[Plan]:
        ms = mappings if mappings is not None else self.config.active_mappings()
        plans: list[Plan] = []
        scanned = []
        for m in ms:
            if not m.active:
                continue
            try:
                _, dirs, bad = self.scan_local(m, progress)
                self.scan_cloud(m, progress)
                scanned.append((m, dirs, bad))
            except (ScanError, CloudError) as e:
                log.error("掃描失敗 %s：%s", m.local_path, e)
                p = Plan(m.id)
                p.warnings.append(str(e))
                plans.append(p)
        planner = self.planner()
        for m, dirs, bad in scanned:
            p = planner.plan_mapping(m, dirs, bad)
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
        stats = {"uploaded": 0, "instant": 0, "failed": 0, "skipped": 0, "linked": 0, "deleted": 0, "mkdirs": 0}
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
            tasks += [(m, t) for t in p.uploads]
        up = self.run_uploads(tasks, progress)
        for k in ("uploaded", "instant", "failed", "skipped"):
            stats[k] = up[k]
        self.last_plans = [p for p in self.last_plans if p not in plans]  # 已執行的計畫不再重複執行
        self.emit("executed", stats=stats)
        return stats

    def run_uploads(self, tasks: list[tuple[Mapping, UploadTask]], progress: ProgressFn | None = None) -> dict:
        res = {"uploaded": 0, "instant": 0, "failed": 0, "skipped": 0}
        total = len(tasks)

        def account(r) -> None:
            if r is SKIPPED or r is None:
                res["skipped"] += 1
            else:
                res["uploaded"] += 1
                res["instant"] += int(r.instant)

        remaining = list(tasks)
        if self.settings.instant_first and total > 1:
            # 第一輪：全部只嘗試秒傳，能秒傳的立刻完成
            remaining = []
            for i, (m, t) in enumerate(tasks, 1):
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
                    account(r)
                if progress:
                    progress("嘗試秒傳", i, total, t.rel_path)

        done = total - len(remaining)
        with ThreadPoolExecutor(max(1, self.settings.upload_workers)) as ex:
            futures = {ex.submit(self.upload_task, m, t): (m, t) for m, t in remaining}
            for fut in as_completed(futures):
                m, t = futures[fut]
                done += 1
                try:
                    account(fut.result())
                except Cancelled:
                    res["skipped"] += 1
                except Exception as e:  # noqa: BLE001
                    self._fail(m, t, e)
                    res["failed"] += 1
                if progress:
                    progress("上傳", done, total, t.rel_path)
        return res

    def upload_task(self, m: Mapping, t: UploadTask, instant_only: bool = False):
        """回傳 UploadResult；instant_only 且無法秒傳時回傳 None；不需上傳時回傳 SKIPPED。"""
        busy_key = (m.id, norm_key(t.rel_path))
        with self._busy_lock:
            if busy_key in self._busy:
                return SKIPPED
            self._busy.add(busy_key)
        try:
            return self._upload_task(m, t, instant_only)
        finally:
            with self._busy_lock:
                self._busy.discard(busy_key)

    def _upload_task(self, m: Mapping, t: UploadTask, instant_only: bool):
        self._check_cancel()
        abs_path = local_join(m.local_path, t.rel_path)
        try:
            h, st = self.hash_cached(abs_path)
        except FileNotFoundError:
            log.info("檔案已不存在，略過：%s", abs_path)
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
            res = self._do_upload(abs_path, cid, name, h, instant_only, t.rel_path)
        except CloudError:
            if self._dir_valid(cid):
                raise
            log.warning("115 資料夾已不存在，重新建立：%s", parent_rel or "/")
            self.db.forget_cloud_dir_prefix(m.id, parent_rel)
            cid = self.ensure_cloud_dir(m, parent_rel)
            res = self._do_upload(abs_path, cid, name, h, instant_only, t.rel_path)
        if res is None:
            return None
        self._after_upload(m, t, lf, cid, res)
        return res

    def _dir_valid(self, cid: str) -> bool:
        try:
            e = self.cloud.get_entry(cid)
        except CloudError:
            return True
        return e is not None and e.is_dir

    def _do_upload(self, abs_path: str, cid: str, name: str, h: FileHashes, instant_only: bool,
                   rel_path: str) -> UploadResult | None:
        if instant_only:
            return self.cloud.try_instant(abs_path, cid, name, h)
        return self.cloud.upload(
            abs_path, cid, name, h, cancel=self.cancel_event,
            progress=lambda done, total: self.emit("file_progress", rel_path=rel_path, done=done, total=total))

    def _after_upload(self, m: Mapping, t: UploadTask, lf: LocalRow, cid: str, res: UploadResult) -> None:
        parent_rel, name = rel_parent(t.rel_path), rel_name(t.rel_path)
        existing = {c.fid for c in self.db.cloud_at(m.id, lf.rel_key)}
        replace = [f for f in t.replace_fids if f != res.fid and f in existing]
        if replace:
            self._retire_old(m, parent_rel, cid, name, replace, t.savedata_group is not None)
            try:  # 115 遇到同名檔可能自動改名，刪除舊版後改回原檔名
                mine = next((e for e in self.cloud.list_dir(cid) if e.fid == res.fid), None)
                if mine and nfc(mine.name) != nfc(name):
                    self.cloud.rename(res.fid, name)
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
        """上傳新版後處理 115 舊版：預設刪除（進 115 回收站）；存檔可設定保留 N 份歷史版本。"""
        keep = self.settings.savedata_keep_versions if savedata else 0
        if keep > 0:
            vcid = self.cloud.ensure_child_dir(cid, self.settings.savedata_versions_dir)
            pat = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))?_" + re.escape(nfc(name)) + "$")
            taken = {nfc(e.name) for e in self.cloud.list_dir(vcid)}
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.cloud.move(fids, vcid)
            n = 0
            for fid in fids:
                new_name = f"{stamp}_{name}"
                while nfc(new_name) in taken:  # 同一秒內多次存檔
                    n += 1
                    new_name = f"{stamp}-{n}_{name}"
                taken.add(nfc(new_name))
                self.cloud.rename(fid, new_name)

            def order(e):
                mt = pat.match(nfc(e.name))
                return mt.group(1), int(mt.group(2) or 0)

            olds = sorted((e for e in self.cloud.list_dir(vcid) if not e.is_dir and pat.match(nfc(e.name))),
                          key=order, reverse=True)
            if len(olds) > keep:
                self.cloud.delete([e.fid for e in olds[keep:]], vcid)
            detail = f"舊版移到 {self.settings.savedata_versions_dir}（保留 {keep} 份）"
        else:
            self.cloud.delete(fids, cid)
            detail = "刪除 115 舊版"
        for fid in fids:
            self.db.remove_cloud_file(m.id, fid)
        self.db.add_history(m.id, "retire_old", rel_join(parent_rel, name), True, detail=detail)

    def _record_link(self, m: Mapping, lf: LocalRow, c: CloudRow) -> None:
        self.db.set_state(m.id, lf.rel_path, lf.hashes, lf.mtime_ns, c.fid, c.sha1)
        self.db.clear_pending_for(m.id, lf.rel_key, except_kinds=[CLOUD_DUPS])

    def _delete_cloud(self, m: Mapping, rel_path: str, fids: list[str], parent_cid: str, reason: str) -> None:
        self.cloud.delete(fids, parent_cid)
        for f in fids:
            self.db.remove_cloud_file(m.id, f)
        self.db.delete_state(m.id, norm_key(rel_path))
        self.db.add_history(m.id, "delete_cloud", rel_path, True, detail=reason)
        log.info("已刪除 115 檔案（%s）：%s", reason, rel_path)
        self.emit("deleted", mapping_id=m.id, rel_path=rel_path)

    def _fail(self, m: Mapping, t: UploadTask, exc: Exception) -> None:
        log.error("上傳失敗 %s：%s", t.rel_path, exc)
        self.db.add_review(m.id, ERROR, t.rel_path, {
            "message": f"上傳失敗：{exc}", "task": asdict(t),
            "local_root": m.local_path, "cloud_root": m.cloud_path,
            "local": {"path": local_join(m.local_path, t.rel_path), "rel_path": t.rel_path},
        })
        self.db.add_history(m.id, "upload", t.rel_path, False, detail=str(exc)[:500])
        self.emit("failed", mapping_id=m.id, rel_path=t.rel_path, error=str(exc))
        self.emit("reviews_changed")

    # ------------------------------------------------------------ 單檔處理（即時同步與核對共用）
    def reconcile_files(self, m: Mapping, rows: list[LocalRow]) -> None:
        planner = self.planner()
        groups: dict[str, list[LocalRow]] = {}
        for lf in rows:
            out = planner.classify(m, lf)
            if out.kind == "group_pending":
                groups.setdefault(out.group, []).append(lf)
                continue
            if out.link:
                self._record_link(m, out.link.local, out.link.cloud)
            if out.kind in ("identical", "in_sync"):
                self.db.clear_pending_for(m.id, lf.rel_key, except_kinds=[CLOUD_DUPS])
            if out.review:
                self.db.clear_pending_for(m.id, out.review.key, except_kinds=[out.review.kind, CLOUD_DUPS])
                self.db.add_review(m.id, out.review.kind, out.review.rel_path, out.review.details,
                                   rel_key=out.review.key)
                self.emit("reviews_changed")
            if out.upload:
                try:
                    self.upload_task(m, out.upload)
                except Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._fail(m, out.upload, e)
        for group in groups:
            self._ensure_group_review(m, group)

    def _ensure_group_review(self, m: Mapping, group: str) -> None:
        planner = self.planner()
        rows = [r for r in (self.db.local_under(m.id, group) if group else self.db.local_files(m.id))
                if planner.detector.group_of(r.rel_path, m.savedata) == group]
        self.db.upsert_group(m.id, group)
        r = planner.group_review(m, group, rows)
        self.db.add_review(m.id, r.kind, r.rel_path, r.details, rel_key=r.key)
        self.emit("reviews_changed")

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
            group, status = planner.savedata_status(m, rel_path + "/x")
            if group is None or status in ("approved", "normal"):
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
        s = self.settings
        if len(states) >= s.mass_delete_guard_min:
            r = planner.mass_delete_review(m, [st.rel_path for st in states], norm_key(rel_path) + "/*",
                                           len(self.db.states(m.id)))
            self.db.add_review(m.id, r.kind, r.rel_path, r.details, rel_key=r.key)
            self.emit("reviews_changed")
            return
        for st in states:
            group, status = planner.savedata_status(m, st.rel_path)
            if group is not None and status in (None, "pending", "exclude"):
                continue
            base = [c for c in self.db.cloud_at(m.id, st.rel_key) if c.sha1 == st.cloud_sha1]
            if not base:
                self.db.delete_state(m.id, st.rel_key)
                continue
            saved = status == "approved"
            if (saved and s.savedata_mirror_deletes) or s.deletion_policy == "auto":
                self._delete_cloud(m, st.rel_path, [c.fid for c in base], base[0].pid,
                                   "savedata" if saved else "auto")
            elif s.deletion_policy == "ignore":
                self.db.delete_state(m.id, st.rel_key)
            else:
                r = planner.local_deleted_review(m, st, base)
                self.db.add_review(m.id, r.kind, r.rel_path, r.details)
                self.emit("reviews_changed")

    def handle_local_move(self, m: Mapping, src_rel: str, dest_rel: str) -> None:
        """NAS 上搬移/改名：已同步的檔案或資料夾直接在 115 搬移/改名，不必重新上傳。"""
        dest_lp = long_path(local_join(m.local_path, dest_rel))
        if not os.path.exists(dest_lp):
            return self.handle_local_delete(m, src_rel)
        planner = self.planner()
        is_dir = os.path.isdir(dest_lp)
        probe = (lambda r: r + "/x") if is_dir else (lambda r: r)
        simple = (self.settings.auto_update_linked
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
        to_cid = self.ensure_cloud_dir(m, rel_parent(dest_rel))
        if norm_key(rel_parent(src_rel)) != norm_key(rel_parent(dest_rel)):
            self.cloud.move([fid], to_cid)
        if nfc(rel_name(src_rel)) != nfc(rel_name(dest_rel)):
            self.cloud.rename(fid, rel_name(dest_rel))
        return to_cid

    # ------------------------------------------------------------ 遊戲存檔（Steam 雲存檔式）
    def savedata_status(self, m: Mapping, rel_path: str) -> tuple[str | None, str | None]:
        return self.planner().savedata_status(m, rel_path)

    def sync_savedata_group(self, m: Mapping, group: str) -> None:
        """把整個存檔群組與 115 對齊：新/改的存檔上傳並刪除 115 舊版；NAS 刪掉的存檔同步刪除。"""
        if not self.local_root_ok(m):
            log.warning("NAS 無法存取，延後同步存檔：%s", group)
            return
        planner = self.planner()
        recursive = planner.detector.is_group_dir(group, m.savedata)
        rows = [r for r in self._index_local_tree(m, group, recursive)
                if planner.detector.group_of(r.rel_path, m.savedata) == group]
        self.ensure_cloud_dir(m, group)
        self.refresh_cloud_dir(m, group, recursive=recursive)
        present = {r.rel_key for r in rows}
        for st in self.db.states_under(m.id, group):
            if (st.rel_key not in present and planner.detector.group_of(st.rel_path, m.savedata) == group
                    and not os.path.exists(long_path(local_join(m.local_path, st.rel_path)))):
                self.handle_local_delete(m, st.rel_path)
        self.reconcile_files(m, rows)
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

    def _rv_conflict(self, m, rv, decision, extra) -> str:
        lf = self._fresh_local(m, rv.rel_path)
        if lf is None:
            return "NAS 檔案已不存在，未做任何變更"
        parent = rel_parent(rv.rel_path)
        self.refresh_cloud_dir(m, parent)
        cloud = self.db.cloud_at(m.id, lf.rel_key)
        group = self._approved_group(m, rv.rel_path)
        if decision == "keep_cloud":
            if not cloud:
                return "115 上已沒有這個檔案"
            self.db.set_state(m.id, lf.rel_path, lf.hashes, lf.mtime_ns, cloud[0].fid, cloud[0].sha1)
            return "已保留 115 版本；之後 NAS 端再修改時才會同步"
        if decision == "upload_keep_both":
            stamp = time.strftime("%Y%m%d-%H%M%S")
            for i, c in enumerate([c for c in cloud if c.sha1 != lf.sha1]):
                stem, ext = os.path.splitext(c.name)
                self.cloud.rename(c.fid, f"{stem} (115舊版 {stamp}{f'-{i}' if i else ''}){ext}")
            self.refresh_cloud_dir(m, parent)
            self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], group, lf.size))
            return "115 舊版已改名保留，NAS 版本已上傳"
        replace = [c.fid for c in cloud if c.sha1 != lf.sha1]
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", replace, group, lf.size))
        return "已上傳 NAS 版本並刪除 115 舊版"

    _rv_cloud_changed = _rv_conflict

    def _must_upload(self, m: Mapping, t: UploadTask) -> None:
        r = self.upload_task(m, t)
        if r is None:
            raise CloudError("上傳未完成")

    def _rv_cloud_deleted(self, m, rv, decision, extra) -> str:
        if decision == "forget":
            self.db.add_ignore(m.id, rv.rel_path)
            self.db.delete_state(m.id, norm_key(rv.rel_path))
            return "之後不再上傳此檔"
        lf = self._fresh_local(m, rv.rel_path)
        if lf is None:
            return "NAS 檔案已不存在"
        self._must_upload(m, UploadTask(m.id, rv.rel_path, "review", [], self._approved_group(m, rv.rel_path),
                                        lf.size))
        return "已重新上傳"

    def _rv_duplicate_hash(self, m, rv, decision, extra) -> str:
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
            self.cloud.delete(fids, existing[fids[0]].pid)
            for f in fids:
                self.db.remove_cloud_file(m.id, f)
        return f"已刪除 {len(fids)} 個多餘的同名檔"

    def _delete_if_still_gone(self, m: Mapping, rel_path: str, delete: bool) -> bool:
        if os.path.exists(long_path(local_join(m.local_path, rel_path))):
            return False
        key = norm_key(rel_path)
        st = self.db.get_state(m.id, key)
        if st is None:
            return False
        if not delete:
            self.db.delete_state(m.id, key)
            return True
        base = [c for c in self.db.cloud_at(m.id, key) if c.sha1 == st.cloud_sha1]
        if base:
            self._delete_cloud(m, rel_path, [c.fid for c in base], base[0].pid, "本人確認")
        else:
            self.db.delete_state(m.id, key)
        return True

    def _rv_local_deleted(self, m, rv, decision, extra) -> str:
        if not self.local_root_ok(m):
            raise ValueError("NAS 目前無法存取，請確認 NAS 連線後再處理")
        if not self._delete_if_still_gone(m, rv.rel_path, decision == "delete_cloud"):
            return "檔案已重新出現在 NAS 或已處理，未做變更"
        return "已刪除 115 上的檔案（可在 115 回收站復原）" if decision == "delete_cloud" else "已保留 115 上的檔案"

    def _rv_mass_delete(self, m, rv, decision, extra) -> str:
        if not self.local_root_ok(m):
            raise ValueError("NAS 目前無法存取，請確認 NAS 連線後再處理")
        n = sum(self._delete_if_still_gone(m, p, decision == "delete_all") for p in rv.details.get("paths", []))
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
            return "已核可，存檔將自動同步到 115"
        if decision == "normal":
            self.db.upsert_group(m.id, group, "normal")
            self.refresh_cloud_dir(m, group, recursive=True)
            self.reconcile_files(m, self.db.local_under(m.id, group) if group else self.db.local_files(m.id))
            return "已改為一般檔案處理"
        self.db.upsert_group(m.id, group, "exclude")
        return "此存檔資料夾將不同步"

    def _rv_error(self, m, rv, decision, extra) -> str:
        t = UploadTask(**rv.details["task"])
        self._must_upload(m, t)
        return "重試成功"
