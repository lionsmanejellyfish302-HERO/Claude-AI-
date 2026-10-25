"""即時同步：監看 NAS 資料夾的變動（類似 OneDrive 桌面同步）。

- watchdog 監看檔案事件；SMB 網路磁碟可能漏事件，因此另有定期完整比對作為保險
- 每個檔案等「穩定」（大小與修改時間在 stable_seconds 內不再變）才處理，避免上傳寫到一半的檔案
- 已核可的存檔群組：最後一次變動後靜置 savedata_quiet_seconds，且指定的遊戲程序已結束，才整組上傳
  （模擬 Steam 在遊戲結束後同步雲存檔）
- NAS 根目錄無法存取時（離線）暫停處理，絕不把離線誤判成刪除
- 115 端（115 電腦版、App、網頁）的變動：定期讀取 115「操作紀錄」，只重新讀取受影響的資料夾並同步到 NAS
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .cloud.base import CloudChange, CloudError
from .config import Mapping
from .engine import Cancelled, FileChanging, SyncEngine
from .paths import local_join, long_path
from .savedata import any_running

log = logging.getLogger(__name__)


class _Pending:
    __slots__ = ("kind", "ts", "src", "sig")

    def __init__(self, kind: str, ts: float, src: str | None = None):
        self.kind = kind  # changed | deleted | moved
        self.ts = ts
        self.src = src  # moved 的來源路徑
        self.sig = None  # (size, mtime_ns) 上次檢查結果


def _make_handler(sync: "RealtimeSync", m: Mapping):
    from watchdog.events import FileSystemEventHandler

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            try:
                sync.on_event(m, event)
            except Exception:  # noqa: BLE001
                log.exception("處理檔案事件失敗")

    return Handler()


class RealtimeSync:
    def __init__(self, engine: SyncEngine):
        self.engine = engine
        self.config = engine.config
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._dirty_groups: dict[tuple[str, str], float] = {}
        self._inflight: set[tuple[str, str]] = set()
        self._stop = threading.Event()
        self._observer = None
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._save_pool: ThreadPoolExecutor | None = None
        self._rescan_running = threading.Event()
        self._next_rescan = 0.0
        self._watches: dict[str, object] = {}
        self._next_watch_check = 0.0
        self._next_cloud_poll = 0.0
        self._next_change_poll = 0.0
        self._cursors: dict[str, str | None] = {}  # 每個 115 帳號的操作紀錄位置
        self._recent_seen: dict[str, int] = {}  # 不支援操作紀錄時：每個對應最近看過的修改時間
        self.running = False

    # ------------------------------------------------------------ 啟動／停止
    def start(self, initial_rescan: bool = True) -> None:
        if self.running:
            return
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver

        s = self.engine.settings
        self._stop.clear()
        self.engine.cancel_event.clear()
        self._observer = PollingObserver(timeout=s.polling_interval_seconds) if s.use_polling_observer else Observer()
        self._watches = {}
        self._observer.start()
        watched = self._ensure_watches()
        self._pool = ThreadPoolExecutor(max(1, s.upload_workers), thread_name_prefix="sync")
        self._save_pool = ThreadPoolExecutor(1, thread_name_prefix="savedata")
        self._next_rescan = time.time() if initial_rescan else time.time() + s.rescan_interval_minutes * 60
        self._cursors, self._recent_seen, self._next_change_poll = {}, {}, 0.0
        self._thread = threading.Thread(target=self._loop, name="realtime", daemon=True)
        self._thread.start()
        self.running = True
        log.info("即時同步已啟動，監看 %d 個資料夾", watched)
        self.engine.emit("realtime", running=True, watched=watched)

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self.engine.cancel_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=10)
        if self._thread:
            self._thread.join(timeout=10)
        for pool in (self._pool, self._save_pool):
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
        self.engine.cancel_event.clear()
        self.running = False
        log.info("即時同步已停止")
        self.engine.emit("realtime", running=False)

    def _ensure_watches(self) -> int:
        """NAS 離線時移除監看、恢復連線後重新監看（例如開機時網路磁碟機尚未連上）。"""
        active = {m.id: m for m in self.config.active_mappings()}
        for mid in list(self._watches):
            m = active.get(mid)
            if m is None or not self.engine.local_root_ok(m):
                try:
                    self._observer.unschedule(self._watches.pop(mid))
                except Exception:  # noqa: BLE001 - 監看可能已因錯誤失效
                    pass
                if m is not None:
                    log.warning("NAS 路徑無法存取，暫停監看：%s", m.local_path)
        for mid, m in active.items():
            if mid in self._watches:
                continue
            if not self.engine.local_root_ok(m):
                continue
            try:
                self._watches[mid] = self._observer.schedule(_make_handler(self, m), m.local_path, recursive=True)
                log.info("開始監看：%s", m.local_path)
                if self.running:  # 重新連上：補做一次完整比對
                    self._next_rescan = 0.0
            except OSError as e:
                log.warning("無法監看 %s：%s", m.local_path, e)
        self._next_watch_check = time.time() + 30
        return len(self._watches)

    # ------------------------------------------------------------ 事件
    def _rel(self, m: Mapping, path: str) -> str | None:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        try:
            rel = os.path.relpath(path, m.local_path)
        except ValueError:  # Windows 不同磁碟
            return None
        if rel == "." or rel.startswith(".."):
            return None
        rel = rel.replace(os.sep, "/")
        if self.engine.planner().ignore.match_rel(rel):
            return None
        return rel

    def on_event(self, m: Mapping, event) -> None:
        etype = event.event_type
        if etype in ("opened", "closed_no_write"):
            return
        src = self._rel(m, event.src_path)
        if etype == "moved":
            dest = self._rel(m, event.dest_path)
            if src and dest:
                self._push(m, dest, "moved", src)
            elif src:
                self._push(m, src, "deleted")
            elif dest:
                self._push(m, dest, "changed")
            return
        if src is None:
            return
        if etype == "deleted":
            self._push(m, src, "deleted")
        elif etype in ("created", "modified", "closed"):
            if event.is_directory and etype == "modified":
                return  # 資料夾 modified 只代表內容變動，個別檔案會另有事件
            self._push(m, src, "changed")

    def _push(self, m: Mapping, rel: str, kind: str, src: str | None = None) -> None:
        now = time.time()
        with self._lock:
            key = (m.id, rel)
            cur = self._pending.get(key)
            if kind == "moved":
                self._pending.pop((m.id, src), None)
                self._pending[key] = _Pending("moved", now, src)
            elif cur and cur.kind == "moved" and kind == "changed":
                cur.ts = now  # 搬移後又修改：保留搬移，處理時一併檢查內容
            else:
                self._pending[key] = _Pending(kind, now)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending) + len(self._dirty_groups) + len(self._inflight)

    # ------------------------------------------------------------ 主迴圈
    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("即時同步迴圈錯誤")

    def _tick(self) -> None:
        s = self.engine.settings
        now = time.time()
        if now >= self._next_watch_check:
            self._ensure_watches()
        due = []
        with self._lock:
            for key, p in list(self._pending.items()):
                if now - p.ts >= s.stable_seconds and key not in self._inflight:
                    due.append((key, p))
        for key, p in due:
            self._dispatch(key, p, now)

        for (mid, group), ts in list(self._dirty_groups.items()):
            if now - ts < s.savedata_quiet_seconds or (mid, "group:" + group) in self._inflight:
                continue
            m = self.config.get_mapping(mid)
            g = self.engine.db.get_group(mid, group)
            if m is None or g is None:
                with self._lock:
                    self._dirty_groups.pop((mid, group), None)
                continue
            if g.process_names and any_running(g.process_names):
                continue  # 遊戲還在執行，等結束後再上傳
            with self._lock:
                self._dirty_groups.pop((mid, group), None)
            self._submit(self._save_pool, (mid, "group:" + group), self.engine.sync_savedata_group, m, group)

        if now >= self._next_cloud_poll:
            self._next_cloud_poll = now + max(30, s.savedata_cloud_poll_seconds)
            self._poll_cloud_savedata()

        if now >= self._next_change_poll:
            self._next_change_poll = now + max(10, s.cloud_poll_seconds)
            self._poll_cloud_changes()

        if now >= self._next_rescan and not self._rescan_running.is_set() and not self.engine.full_lock.locked():
            self._next_rescan = now + max(1, s.rescan_interval_minutes) * 60
            self._rescan_running.set()
            threading.Thread(target=self._rescan, name="rescan", daemon=True).start()

    def _dispatch(self, key: tuple[str, str], p: _Pending, now: float) -> None:
        mid, rel = key
        m = self.config.get_mapping(mid)
        if m is None or not m.active:
            with self._lock:
                self._pending.pop(key, None)
            return
        if not self.engine.local_root_ok(m):
            with self._lock:
                p.ts = now  # NAS 離線：稍後再試，不當成刪除
            return

        abs_path = long_path(local_join(m.local_path, rel))
        if p.kind != "deleted":
            try:
                st = os.stat(abs_path)
                sig = (st.st_size, st.st_mtime_ns)
            except FileNotFoundError:
                sig = None
            except OSError:
                with self._lock:
                    p.ts = now
                return
            if sig is not None and sig != p.sig:
                with self._lock:  # 第一次檢查或仍在變動：再等一輪
                    p.sig = sig
                    p.ts = now
                return

        group, status = self.engine.savedata_status(m, rel)
        if group is None and p.src:
            group, status = self.engine.savedata_status(m, p.src)
        with self._lock:
            self._pending.pop(key, None)
        if group is not None and status == "approved":
            with self._lock:
                self._dirty_groups[(mid, group)] = now
            self.engine.emit("savedata_dirty", mapping_id=mid, group=group)
            return

        if p.kind == "moved":
            fn, args = self.engine.handle_local_move, (m, p.src, rel)
        elif p.kind == "deleted":
            fn, args = self.engine.handle_local_delete, (m, rel)
        else:
            fn, args = self.engine.handle_local_change, (m, rel)
        self._submit(self._pool, key, fn, *args)

    def _submit(self, pool: ThreadPoolExecutor | None, key, fn, *args) -> None:
        if pool is None:
            return
        with self._lock:
            self._inflight.add(key)

        def run():
            try:
                fn(*args)
            except FileChanging:
                m = args[0]
                rel = args[-1] if isinstance(args[-1], str) else None
                if rel:
                    self._push(m, rel, "changed")
            except Cancelled:
                pass
            except Exception:  # noqa: BLE001
                log.exception("即時同步處理失敗：%s", key)
            finally:
                with self._lock:
                    self._inflight.discard(key)

        try:
            pool.submit(run)
        except RuntimeError:  # 停止中
            with self._lock:
                self._inflight.discard(key)

    def _poll_cloud_savedata(self) -> None:
        """雙向／下載模式：定期檢查 115 上已核可的存檔資料夾，有別台電腦上傳的新存檔就下載（遊戲結束後）。"""
        for m in self.config.active_mappings():
            if not m.can_download or not self.engine.local_root_ok(m):
                continue
            for g in self.engine.db.list_groups(m.id):
                key = (m.id, "group:" + g.rel_dir)
                if g.status != "approved" or key in self._inflight or (m.id, g.rel_dir) in self._dirty_groups:
                    continue
                self._submit(self._save_pool, key, self.engine.sync_savedata_group, m, g.rel_dir)

    def _poll_cloud_changes(self) -> None:
        """115 端的變動（在 115 電腦版、App 或網頁上新增、修改、改名、搬移、刪除）同步到 NAS。"""
        mappings = [m for m in self.config.active_mappings() if m.can_download and self.engine.local_root_ok(m)]
        for aid in sorted({m.account_id for m in mappings}):
            key = ("cloud-changes", aid)
            if key in self._inflight:
                continue
            client = self.engine.client(aid)
            try:
                changes, cursor = client.changes(self._cursors.get(aid))
                self._cursors[aid] = cursor
            except NotImplementedError:
                changes = self._recent_changes(client, [m for m in mappings if m.account_id == aid])
            except CloudError as e:
                log.warning("讀取 115 操作紀錄失敗（稍後重試）：%s", e)
                continue
            if changes:
                self._submit(self._pool, key, self.engine.apply_cloud_changes, aid, changes)

    def _recent_changes(self, client, mappings: list[Mapping]) -> list[CloudChange]:
        """備用方式：比對每個對應底下最近修改的檔案（偵測新增與修改；刪除由定期完整比對處理）。"""
        out: list[CloudChange] = []
        for m in mappings:
            if not m.cloud_cid:
                continue
            try:
                recent = client.recent_files(m.cloud_cid)
            except (NotImplementedError, CloudError):
                continue
            newest = max((e.mtime for e in recent), default=0)
            seen = self._recent_seen.get(m.id)
            self._recent_seen[m.id] = max(newest, seen or 0)
            if seen is None:
                continue
            for e in recent:
                known = self.engine.db.cloud_file_by_fid(m.id, e.fid)
                if e.mtime > seen or known is None or known.sha1 != e.sha1:
                    out.append(CloudChange(e.fid, e.pid, "recent", e.name))
        return out

    def _rescan(self) -> None:
        try:
            log.info("定期完整比對開始")
            plans = self.engine.analyze()
            self.engine.execute(plans)
            log.info("定期完整比對完成")
        except Cancelled:
            pass
        except Exception:  # noqa: BLE001
            log.exception("定期完整比對失敗")
        finally:
            self._rescan_running.clear()

    def flush(self) -> None:
        """測試用：立即處理所有待辦事件（忽略等待時間）。"""
        with self._lock:
            for p in self._pending.values():
                p.ts = 0
            for k in self._dirty_groups:
                self._dirty_groups[k] = 0
