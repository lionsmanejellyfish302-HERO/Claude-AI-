"""隨機壓力測試：兩邊隨機新增／修改／刪除／改名後反覆同步，檢查「不遺失資料」與「兩邊一致」。"""
import itertools
import os
import random

import pytest

from nas115sync import compare as C
from nas115sync.config import DOWNLOAD, TWO_WAY, UPLOAD
from nas115sync.hashing import hash_bytes, hash_file

ROOT = "/備份/NAS"
PATHS = ["a.txt", "b.txt", "子資料夾/c.txt", "子資料夾/d.bin", "深/層/e.txt", "深/f 檔案.txt"]
SAFE = {  # 不會破壞任何一邊最新內容的決定
    C.CONFLICT: ["keep_cloud", "skip"],
    C.CLOUD_CHANGED: ["keep_cloud", "skip"],
    C.CLOUD_DELETED: ["upload", "skip", "forget"],
    C.DUPLICATE: ["upload", "move_cloud", "download", "skip"],
    C.SAME_NAME: ["upload", "skip"],
    C.LOCAL_DELETED: ["download", "keep_cloud"],
    C.CLOUD_DUPS: ["keep"],
    C.ERROR: ["retry", "skip"],
}
_counter = itertools.count()


class World:
    def __init__(self, env, rng):
        self.env, self.rng = env, rng
        self.written: set[str] = set()  # 使用者寫過的內容（SHA-1）
        self.released: set[str] = set()  # 使用者自己覆蓋或刪除的內容

    def _content(self) -> bytes:
        return f"內容-{next(_counter)}-{self.rng.random()}".encode() * self.rng.randint(1, 50)

    def _local_sha1(self, rel):
        p = self.env.nas / rel
        return hash_file(str(p)).sha1 if p.is_file() else None

    def _cloud_entries(self, rel):
        parent = os.path.dirname(rel)
        cid = self.env.cloud.resolve_path(ROOT + ("/" + parent if parent else ""))
        if cid is None:
            return []
        return [e for e in self.env.cloud.list_dir(cid) if not e.is_dir and e.name == os.path.basename(rel)]

    def local_write(self, rel):
        old = self._local_sha1(rel)
        if old:
            self.released.add(old)
        data = self._content()
        self.env.write(rel, data)
        self.written.add(hash_bytes(data).sha1)

    def local_delete(self, rel):
        old = self._local_sha1(rel)
        if old:
            self.released.add(old)
            os.remove(self.env.nas / rel)

    def local_rename(self, src, dst):
        if (self.env.nas / src).is_file() and not (self.env.nas / dst).exists():
            (self.env.nas / dst).parent.mkdir(parents=True, exist_ok=True)
            os.rename(self.env.nas / src, self.env.nas / dst)

    def cloud_write(self, rel):
        for e in self._cloud_entries(rel):
            self.released.add(e.sha1)
            self.env.cloud.delete([e.fid])
        data = self._content()
        self.env.cloud.put_file(f"{ROOT}/{rel}", data)
        self.written.add(hash_bytes(data).sha1)

    def cloud_rename(self, src, dst):
        if src == dst or self._cloud_entries(dst):
            return
        entries = self._cloud_entries(src)
        if not entries:
            return
        parent = os.path.dirname(dst)
        cid = self.env.cloud.ensure_path(ROOT + ("/" + parent if parent else ""))
        self.env.cloud.move([entries[0].fid], cid)
        self.env.cloud.rename(entries[0].fid, os.path.basename(dst))

    def cloud_delete(self, rel):
        for e in self._cloud_entries(rel):
            self.released.add(e.sha1)
            self.env.cloud.delete([e.fid])

    def random_ops(self, mode):
        ops = []
        if mode in (UPLOAD, TWO_WAY):
            ops += ["local_write"] * 4 + ["local_delete", "local_rename"]
        if mode in (DOWNLOAD, TWO_WAY):
            ops += ["cloud_write"] * 4 + ["cloud_delete", "cloud_rename"]
        if mode == UPLOAD:
            ops += ["cloud_write"]  # 雲端也可能被別人改
        if mode == DOWNLOAD:
            ops += ["local_write"]
        for _ in range(self.rng.randint(1, 4)):
            op = self.rng.choice(ops)
            if op in ("local_rename", "cloud_rename"):
                getattr(self, op)(self.rng.choice(PATHS), self.rng.choice(PATHS))
            else:
                getattr(self, op)(self.rng.choice(PATHS))

    # ------------------------------------------------------------ 檢查
    def existing(self) -> set[str]:
        out = set()
        for p in self.env.nas.rglob("*"):
            if p.is_file() and not p.name.endswith(".115part"):
                out.add(hash_file(str(p)).sha1)
        out |= {n["sha1"] for n in self.env.cloud.nodes.values() if not n["is_dir"] and n.get("sha1")}
        return out

    def check_no_loss(self, where):
        missing = (self.written - self.released) - self.existing()
        assert not missing, f"{where}：資料遺失 {missing}"

    def resolve_some_reviews(self):
        for rv in self.env.reviews():
            if self.rng.random() < 0.5:
                continue
            opts = [d for d, _ in C.available_decisions(rv.kind, rv.details) if d in SAFE.get(rv.kind, [])]
            if not opts:
                continue
            try:
                self.env.engine.apply_review(rv.id, self.rng.choice(opts))
            except (ValueError, Exception) as e:  # noqa: BLE001 - 情況已改變時決定可能不適用
                assert "已處理" in str(e) or "不存在" in str(e) or "請重新比對" in str(e) or "仍存在" in str(e), e

    def check_converged(self, mode):
        pending = {r.rel_key for r in self.env.reviews()}
        if any(r.kind == C.MASS_DELETE for r in self.env.reviews()):
            return
        for rel in PATHS:
            key = rel.casefold()
            if key in pending or self.env.db.is_ignored(self.env.mapping.id, key):
                continue
            local = self._local_sha1(rel)
            cloud = {e.sha1 for e in self._cloud_entries(rel)}
            st = self.env.db.get_state(self.env.mapping.id, key)
            if st and st.local_sha1 == local and st.cloud_sha1 in cloud and local not in cloud:
                continue  # 本人選擇「維持兩邊現狀」的刻意差異
            if mode == TWO_WAY:
                ok = (local is None and not cloud) or (local is not None and cloud == {local})
            elif mode == UPLOAD:
                ok = local is None or local in cloud
            else:
                ok = not cloud or local in cloud
            hist = [(h["action"], h["rel_path"], h["detail"]) for h in self.env.db.recent_history(30)]
            assert ok, f"{mode} 未收斂：{rel} NAS={local} 115={cloud} 基準={st} 歷史={hist}"


@pytest.mark.parametrize("mode,policy", [(TWO_WAY, "auto"), (TWO_WAY, "review"), (UPLOAD, "auto"),
                                         (UPLOAD, "review"), (DOWNLOAD, "auto"), (DOWNLOAD, "review")])
@pytest.mark.parametrize("seed", range(4))
def test_random_sync_never_loses_data(env, mode, policy, seed):
    rng = random.Random(f"{mode}-{policy}-{seed}")
    env.mapping.direction = mode
    env.config.settings.deletion_policy = policy
    env.config.save()
    w = World(env, rng)
    cursor = env.cloud.changes(None)[1]
    for rnd in range(25):
        w.random_ops(mode)
        if rng.random() < 0.5:  # 即時同步：只依 115 操作紀錄處理受影響的資料夾
            changes, cursor = env.cloud.changes(cursor)
            env.engine.apply_cloud_changes("default", changes)
            w.check_no_loss(f"第 {rnd} 輪即時處理 115 變動後")
        env.sync()
        w.check_no_loss(f"第 {rnd} 輪同步後")
        w.resolve_some_reviews()
        w.check_no_loss(f"第 {rnd} 輪處理核對後")
        env.sync()  # 第二次同步讓處理過的項目收斂
        w.check_no_loss(f"第 {rnd} 輪再同步後")
        w.check_converged(mode)
