"""手動操作（雙窗格瀏覽器、重複檔案、報告）：不限於路徑對應，任何 NAS 資料夾 ⇄ 任何 115 資料夾。

上傳一樣「先秒傳、再實際上傳」並可續傳；下載一樣先找 NAS 上相同 SHA-1 的檔案直接複製，再下載並驗證 SHA-1。
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cloud.base import CloudClient, CloudEntry
from .compare import KIND_LABELS, Plan
from .paths import cloud_join, local_join, long_path, name_key, norm_key

log = logging.getLogger(__name__)

# 遇到同名但內容不同時
OVERWRITE, SKIP, KEEP_BOTH = "overwrite", "skip", "keep_both"


def _stamped(name: str, tag: str) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem} ({tag} {time.strftime('%Y%m%d-%H%M%S')}){ext}"


# ---------------------------------------------------------------- 上傳
def upload_paths(engine, account_id: str, paths: list[str], cid: str, conflict: str = KEEP_BOTH) -> dict:
    """把 NAS 上的檔案/資料夾上傳到 115 的 cid 資料夾。"""
    c: CloudClient = engine.client(account_id)
    stats = {"instant": 0, "uploaded": 0, "skipped": 0, "failed": 0}
    listing: dict[str, list[CloudEntry]] = {}
    jobs: list[tuple[str, str, str]] = []  # (本機路徑, 目的 cid, 檔名)

    def entries(target: str) -> list[CloudEntry]:
        if target not in listing:
            listing[target] = c.list_dir(target)
        return listing[target]

    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(long_path(p)):
            dir_cids = {p: c.ensure_child_dir(cid, os.path.basename(p.rstrip("\\/")))}
            for root, dirs, files in os.walk(p):
                parent_cid = dir_cids[root]
                for d in dirs:
                    dir_cids[os.path.join(root, d)] = c.ensure_child_dir(parent_cid, d)
                jobs += [(os.path.join(root, f), parent_cid, f) for f in files]
        elif os.path.isfile(long_path(p)):
            jobs.append((p, cid, os.path.basename(p)))

    pending = []
    for path, target, name in jobs:
        engine._check_cancel()
        try:
            h, _ = engine.hash_cached(path)
        except Exception as e:  # noqa: BLE001
            log.error("無法讀取 %s：%s", path, e)
            stats["failed"] += 1
            continue
        same_name = [e for e in entries(target) if not e.is_dir and name_key(e.name) == name_key(name)]
        replace = []
        if any(e.sha1 == h.sha1 for e in same_name):
            stats["skipped"] += 1  # 115 上已有完全相同的檔案
            continue
        if same_name:
            if conflict == SKIP:
                stats["skipped"] += 1
                continue
            if conflict == KEEP_BOTH:
                name = _stamped(name, "NAS")
            else:
                replace = [e.fid for e in same_name]
        res = c.try_instant(path, target, name, h)  # 先全部嘗試秒傳
        if res is not None:
            engine.transfers.start("upload", "", path, h.size).finish("done", instant=True)
            stats["instant"] += 1
            if replace:
                c.delete(replace, target)
            engine.db.add_history("", "upload(手動)", path, True, True, h.size, "秒傳")
        else:
            pending.append((path, target, name, h, replace))

    def work(job):
        path, target, name, h, replace = job
        engine.upload_with_control(c, "", path, path, target, name, h)
        if replace:
            c.delete(replace, target)
        engine.db.add_history("", "upload(手動)", path, True, False, h.size, "實際上傳")

    with ThreadPoolExecutor(max(1, engine.settings.upload_workers)) as ex:
        for fut in as_completed([ex.submit(work, j) for j in pending]):
            try:
                fut.result()
                stats["uploaded"] += 1
            except Exception as e:  # noqa: BLE001
                log.error("上傳失敗：%s", e)
                stats["failed"] += 1
    return stats


# ---------------------------------------------------------------- 下載
def download_entries(engine, account_id: str, items: list[CloudEntry], local_dir: str,
                     conflict: str = KEEP_BOTH) -> dict:
    """把 115 上的檔案/資料夾下載到 NAS 的 local_dir。"""
    c: CloudClient = engine.client(account_id)
    stats = {"downloaded": 0, "copied": 0, "skipped": 0, "failed": 0}
    jobs: list[tuple[CloudEntry, str]] = []
    for e in items:
        if e.is_dir:
            base = os.path.join(local_dir, e.name)
            for rel, _, entries in c.walk(e.fid, "", cancel=engine.cancel_event):
                d = local_join(base, rel)
                os.makedirs(long_path(d), exist_ok=True)
                jobs += [(x, os.path.join(d, x.name)) for x in entries if not x.is_dir]
        else:
            jobs.append((e, os.path.join(local_dir, e.name)))

    def work(job):
        e, dest = job
        lp = long_path(dest)
        keep_old = None
        if os.path.exists(lp):
            h, _ = engine.hash_cached(dest)
            if h.sha1 == e.sha1.upper():
                return "skipped"
            if conflict == SKIP:
                return "skipped"
            if conflict == KEEP_BOTH:
                dest = os.path.join(os.path.dirname(dest), _stamped(e.name, "115"))
            else:
                keep_old = lambda: send_to_recycle_bin(dest)  # noqa: E731
        h, how = engine.fetch_to(c, "", dest, e.fid, e.pick_code, e.sha1, e.size, dest, keep_old=keep_old)
        st = os.stat(long_path(dest))
        engine.db.put_cached_hash(dest, st.st_mtime_ns, h)
        engine.db.add_history("", "download(手動)", dest, True, how == "copied", h.size,
                              "從 NAS 相同檔案複製" if how == "copied" else "下載")
        return how

    with ThreadPoolExecutor(max(1, engine.settings.upload_workers)) as ex:
        for fut in as_completed([ex.submit(work, j) for j in jobs]):
            try:
                how = fut.result()
                if how == "skipped":
                    stats["skipped"] += 1
                else:
                    stats["downloaded"] += 1
                    stats["copied"] += int(how == "copied")
            except Exception as e:  # noqa: BLE001
                log.error("下載失敗：%s", e)
                stats["failed"] += 1
    return stats


# ---------------------------------------------------------------- 比較兩個資料夾
def compare_folders(engine, local_dir: str, account_id: str, cid: str) -> dict[str, str]:
    """比較 NAS 資料夾與 115 資料夾（第一層檔案）：回傳 {名稱鍵: same|different|nas_only|cloud_only}。"""
    c = engine.client(account_id)
    cloud = {name_key(e.name): e for e in c.list_dir(cid) if not e.is_dir}
    result: dict[str, str] = {}
    with os.scandir(long_path(local_dir)) as it:
        for entry in it:
            if not entry.is_file(follow_symlinks=False):
                continue
            k = name_key(entry.name)
            e = cloud.pop(k, None)
            if e is None:
                result[k] = "nas_only"
                continue
            try:
                h, _ = engine.hash_cached(os.path.join(local_dir, entry.name))
                result[k] = "same" if h.sha1 == e.sha1.upper() else "different"
            except Exception:  # noqa: BLE001
                result[k] = "different"
    for k in cloud:
        result[k] = "cloud_only"
    return result


# ---------------------------------------------------------------- 重複檔案
def duplicate_report(engine) -> list[dict]:
    roots = {m.id: m for m in engine.config.mappings}
    out = []
    for g in engine.db.duplicate_groups():
        for i in g["items"]:
            m = roots.get(i["mapping_id"])
            if m is None:
                i["path"] = i["rel_path"]
            elif i["where"] == "NAS":
                i["path"] = local_join(m.local_path, i["rel_path"])
            else:
                i["path"] = cloud_join(m.cloud_path, i["rel_path"])
                i["account_id"] = m.account_id
        out.append(g)
    return out


def delete_cloud_items(engine, items: list[dict]) -> int:
    """刪除重複檔案報告中選取的 115 檔案（進 115 回收站）。"""
    n = 0
    for i in items:
        m = engine.config.get_mapping(i["mapping_id"])
        if m is None or i.get("where") != "115":
            continue
        engine.cloud_for(m).delete([i["fid"]], i.get("pid", ""))
        engine.db.remove_cloud_file(m.id, i["fid"])
        st = engine.db.get_state(m.id, norm_key(i["rel_path"]))
        if st and st.cloud_fid == i["fid"]:
            engine.db.delete_state(m.id, st.rel_key)
        engine.db.add_history(m.id, "delete_cloud", i["rel_path"], True, detail="重複檔案")
        n += 1
    return n


# ---------------------------------------------------------------- 報告
def export_plans_csv(engine, plans: list[Plan], path: str) -> int:
    """把比對結果匯出成 CSV（Excel 可直接開啟），方便離線逐項核對。"""
    rows = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["NAS 根目錄", "115 根目錄", "項目", "相對路徑", "大小", "說明"])
        for p in plans:
            m = engine.config.get_mapping(p.mapping_id)
            lr, cr = (m.local_path, m.cloud_path) if m else ("", "")
            out = [("待上傳", u.rel_path, u.size, u.reason) for u in p.uploads]
            out += [("待下載", d.rel_path, d.size, d.reason) for d in p.downloads]
            out += [("待刪除 115", d.rel_path, "", d.reason) for d in p.deletes]
            out += [("待刪除 NAS（可復原）", d.rel_path, "", d.reason) for d in p.local_deletes]
            out += [("待核對：" + KIND_LABELS.get(r.kind, r.kind), r.rel_path, "", r.details.get("message", ""))
                    for r in p.reviews]
            out += [("僅 115 有", c.rel_path, c.size, "") for c in p.cloud_only]
            out += [("僅 NAS 有", r, "", "") for r in p.local_only]
            out += [("暫時無法讀取", r, "", "") for r in p.unreadable]
            out += [("警告", "", "", w_) for w_ in p.warnings]
            for row in out:
                w.writerow([lr, cr, *row])
                rows += 1
    return rows


# ---------------------------------------------------------------- 資源回收筒
def send_to_recycle_bin(path: str) -> None:
    """Windows：移到資源回收筒（網路磁碟上的檔案 Windows 會直接刪除）；其他系統移到使用者暫存垃圾桶。"""
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise FileNotFoundError(f"找不到：{path}")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
        op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0\0", None,
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
        if rc != 0 or op.fAnyOperationsAborted:
            raise OSError(f"無法移到資源回收筒（代碼 {rc}）：{path}")
        return
    trash = os.path.join(os.path.expanduser("~"), ".cache", "nas115sync_trash", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(trash, exist_ok=True)
    shutil.move(path, os.path.join(trash, os.path.basename(path)))

