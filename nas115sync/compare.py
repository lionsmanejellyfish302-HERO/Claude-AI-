"""比對邏輯：以「相對路徑（檔名）＋ SHA-1 ＋ MD5 ＋ 同步基準」決定每個檔案要自動處理或交給本人核對。

規則摘要（細節見 README）：
- 同路徑、SHA-1 相同              → 已同步（記錄同步基準）
- 同路徑、內容不同、沒有同步基準  → 待核對（conflict）
- 已連結檔案在 NAS 端修改          → 自動上傳新版並刪除雲端舊版（可設定改為待核對）
- 雲端被別人改過                    → 待核對（cloud_changed）
- 新檔案，但 SHA-1/MD5 和別處相同  → 待核對（duplicate_hash）
- 新檔案，但同檔名出現在別的資料夾 → 待核對（same_name）
- 遊戲存檔群組第一次出現            → 待核對（savedata_group），核可後即像 Steam 雲存檔自動同步
- NAS 端刪除                        → 依設定（預設待核對）；大量刪除一律暫停待核對
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Mapping, Settings
from .db import CloudRow, Database, LocalRow, StateRow
from .paths import IgnoreMatcher, cloud_join, local_join, norm_key
from .savedata import SavedataDetector

CONFLICT = "conflict"
CLOUD_CHANGED = "cloud_changed"
CLOUD_DELETED = "cloud_deleted"
DUPLICATE = "duplicate_hash"
SAME_NAME = "same_name"
CLOUD_DUPS = "cloud_duplicates"
LOCAL_DELETED = "local_deleted"
MASS_DELETE = "mass_delete"
SAVEDATA_GROUP = "savedata_group"
ERROR = "error"

KIND_LABELS = {
    CONFLICT: "同檔名內容不同",
    CLOUD_CHANGED: "115 端已被修改",
    CLOUD_DELETED: "115 端已被刪除",
    DUPLICATE: "相同 MD5/SHA-1",
    SAME_NAME: "同檔名在其他資料夾",
    CLOUD_DUPS: "115 同資料夾有重複同名檔",
    LOCAL_DELETED: "NAS 端已刪除",
    MASS_DELETE: "大量刪除（疑似 NAS 離線）",
    SAVEDATA_GROUP: "遊戲存檔群組",
    ERROR: "上傳失敗",
}

DECISIONS: dict[str, list[tuple[str, str]]] = {
    CONFLICT: [("upload_replace", "以 NAS 版覆蓋（刪除 115 舊版）"), ("upload_keep_both", "保留兩者（115 舊版改名）"),
               ("keep_cloud", "保留 115 版（之後 NAS 修改才同步）"), ("skip", "暫時略過"), ("ignore", "永久忽略此檔")],
    CLOUD_CHANGED: [("upload_replace", "以 NAS 版覆蓋"), ("keep_cloud", "接受 115 版"), ("skip", "暫時略過"),
                    ("ignore", "永久忽略此檔")],
    CLOUD_DELETED: [("upload", "重新上傳"), ("forget", "不再上傳（忽略此檔）"), ("skip", "暫時略過")],
    DUPLICATE: [("upload", "仍然上傳（會觸發秒傳）"), ("move_cloud", "視為搬移：在 115 搬移原檔"), ("skip", "暫時略過"),
                ("ignore", "永久忽略此檔")],
    SAME_NAME: [("upload", "上傳"), ("skip", "暫時略過"), ("ignore", "永久忽略此檔")],
    CLOUD_DUPS: [("delete_extra", "刪除 115 多餘的同名檔"), ("keep", "保留（不再提示）")],
    LOCAL_DELETED: [("delete_cloud", "刪除 115 上的檔案（進回收站）"), ("keep_cloud", "保留 115 上的檔案")],
    MASS_DELETE: [("delete_all", "確認全部刪除 115 檔案"), ("keep_all", "全部保留 115 檔案")],
    SAVEDATA_GROUP: [("approve", "核可：像 Steam 雲存檔自動同步"), ("normal", "當一般檔案處理"),
                     ("exclude", "不同步此資料夾")],
    ERROR: [("retry", "重試"), ("skip", "略過")],
}

def available_decisions(kind: str, details: dict) -> list[tuple[str, str]]:
    opts = DECISIONS.get(kind, [])
    if kind == DUPLICATE and not details.get("move_candidate"):
        opts = [o for o in opts if o[0] != "move_cloud"]
    return opts


# 完整掃描時會重新產生的種類（不再成立就自動撤銷）
REGENERATED_KINDS = {CONFLICT, CLOUD_CHANGED, CLOUD_DELETED, DUPLICATE, SAME_NAME, CLOUD_DUPS, LOCAL_DELETED,
                     MASS_DELETE, SAVEDATA_GROUP}


@dataclass
class UploadTask:
    mapping_id: str
    rel_path: str
    reason: str  # new | modified | savedata | review | retry
    replace_fids: list[str] = field(default_factory=list)
    savedata_group: str | None = None
    size: int = 0


@dataclass
class LinkTask:
    mapping_id: str
    rel_path: str
    local: LocalRow
    cloud: CloudRow


@dataclass
class DeleteTask:
    mapping_id: str
    rel_path: str
    fids: list[str]
    parent_cid: str
    reason: str


@dataclass
class ReviewSpec:
    kind: str
    rel_path: str
    details: dict
    rel_key: str | None = None

    @property
    def key(self) -> str:
        return norm_key(self.rel_path) if self.rel_key is None else self.rel_key


@dataclass
class Outcome:
    kind: str  # skip | unreadable | group_pending | identical | in_sync | upload | review
    upload: UploadTask | None = None
    link: LinkTask | None = None
    review: ReviewSpec | None = None
    group: str | None = None


@dataclass
class Plan:
    mapping_id: str
    uploads: list[UploadTask] = field(default_factory=list)
    links: list[LinkTask] = field(default_factory=list)
    deletes: list[DeleteTask] = field(default_factory=list)
    forgets: list[str] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)
    reviews: list[ReviewSpec] = field(default_factory=list)
    cloud_only: list[CloudRow] = field(default_factory=list)
    identical: int = 0
    skipped: int = 0
    unreadable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "uploads": len(self.uploads),
            "upload_bytes": sum(u.size for u in self.uploads),
            "links": len(self.links),
            "identical": self.identical,
            "deletes": len(self.deletes),
            "mkdirs": len(self.mkdirs),
            "reviews": len(self.reviews),
            "cloud_only": len(self.cloud_only),
            "skipped": self.skipped,
            "unreadable": len(self.unreadable),
        }


class Planner:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.detector = SavedataDetector(settings.savedata_dir_names, settings.savedata_extensions)
        self.ignore = IgnoreMatcher(list(settings.ignore_patterns) + [settings.savedata_versions_dir])
        self._roots: dict[str, str] = {}
        self._local_roots: dict[str, str] = {}

    # ------------------------------------------------------------ 細節資訊（給核對畫面顯示）
    @staticmethod
    def local_info(m: Mapping, lf: LocalRow) -> dict:
        return {"path": local_join(m.local_path, lf.rel_path), "rel_path": lf.rel_path, "size": lf.size,
                "mtime": lf.mtime_ns / 1e9, "md5": lf.md5, "sha1": lf.sha1}

    def cloud_info(self, c: CloudRow) -> dict:
        info = c.brief()
        root = self._roots.get(c.mapping_id)
        info["path"] = cloud_join(root, c.rel_path) if root is not None else c.rel_path
        return info

    def set_roots(self, mappings) -> None:
        """讓核對畫面能顯示其他對應中檔案的完整 115 路徑。"""
        self._roots = {m.id: m.cloud_path for m in mappings}
        self._local_roots = {m.id: m.local_path for m in mappings}

    def _details(self, m: Mapping, lf: LocalRow, message: str, cloud: list[CloudRow] = (), **extra) -> dict:
        d = {"message": message, "local_root": m.local_path, "cloud_root": m.cloud_path,
             "local": self.local_info(m, lf), "cloud": [self.cloud_info(c) for c in cloud]}
        d.update(extra)
        return d

    # ------------------------------------------------------------ 存檔群組
    def savedata_status(self, m: Mapping, rel_path: str) -> tuple[str | None, str | None]:
        """回傳 (群組相對路徑, 狀態)；不是存檔回傳 (None, None)。狀態 None 表示尚未核對。"""
        group = self.detector.group_of(rel_path, m.savedata)
        if group is None:
            return None, None
        g = self.db.get_group(m.id, group)
        return group, (g.status if g else None)

    # ------------------------------------------------------------ 單一檔案分類
    def classify(self, m: Mapping, lf: LocalRow) -> Outcome:
        key = lf.rel_key
        if self.ignore.match_rel(lf.rel_path) or self.db.is_ignored(m.id, key):
            return Outcome("skip")
        if not lf.sha1:
            return Outcome("unreadable")
        group, status = self.savedata_status(m, lf.rel_path)
        saved = False
        if group is not None:
            if status in (None, "pending"):
                return Outcome("group_pending", group=group)
            if status == "exclude":
                return Outcome("skip")
            saved = status == "approved"
            if not saved:
                group = None

        cloud = self.db.cloud_at(m.id, key)
        state = self.db.get_state(m.id, key)
        sha1 = lf.sha1

        same = [c for c in cloud if c.sha1 == sha1]
        if same:
            target = next((c for c in same if state and c.fid == state.cloud_fid), same[0])
            others = [c for c in cloud if c.fid != target.fid]
            review = None
            if others and not self.db.is_ignored(m.id, key, CLOUD_DUPS):
                review = ReviewSpec(CLOUD_DUPS, lf.rel_path, self._details(
                    m, lf, f"115 同一資料夾內有 {len(cloud)} 個同名檔，其中一個與 NAS 相同。", cloud,
                    keep_fid=target.fid, extra_fids=[c.fid for c in others]))
            linked = (state and state.local_sha1 == sha1 and state.cloud_fid == target.fid
                      and state.cloud_sha1 == sha1)
            link = None if linked else LinkTask(m.id, lf.rel_path, lf, target)
            return Outcome("identical", link=link, review=review)

        if cloud:
            if state:
                base_cloud = [c for c in cloud if c.sha1 == state.cloud_sha1]
                local_changed = state.local_sha1 != sha1
                cloud_changed = not base_cloud
                if not local_changed and not cloud_changed:
                    return Outcome("in_sync")  # 先前選擇「保留 115 版」
                if local_changed and not cloud_changed:
                    if saved or self.settings.auto_update_linked:
                        return Outcome("upload", upload=UploadTask(
                            m.id, lf.rel_path, "savedata" if saved else "modified",
                            [c.fid for c in base_cloud], group, lf.size))
                    return self._review(CONFLICT, m, lf, "已連結的檔案在 NAS 端被修改（設定為需本人核對）。", cloud)
                if not local_changed:
                    return self._review(CLOUD_CHANGED, m, lf, "115 上的檔案在上次同步後被修改，NAS 版本未變。", cloud)
                return self._review(CONFLICT, m, lf, "NAS 與 115 兩邊在上次同步後都被修改。", cloud)
            return self._review(CONFLICT, m, lf, "相同檔名但 SHA-1 不同（尚未建立同步基準）。", cloud)

        if state:
            return self._review(CLOUD_DELETED, m, lf, "上次同步後，115 上的這個檔案已不存在。")

        if saved:
            return Outcome("upload", upload=UploadTask(m.id, lf.rel_path, "savedata", [], group, lf.size))

        dup_cloud = [c for c in self.db.cloud_by_sha1(sha1) if not (c.mapping_id == m.id and c.rel_key == key)]
        dup_local = [x for x in self.db.local_by_hash(sha1, lf.md5)
                     if not (x.mapping_id == m.id and x.rel_key == key)]
        dup_md5 = [s for s in self.db.states_by_md5(lf.md5) if not (s.mapping_id == m.id and s.rel_key == key)]
        if (dup_cloud or dup_local or dup_md5) and not self.db.is_ignored(m.id, key, DUPLICATE):
            matches = []
            for c in dup_cloud:
                matches.append({"where": "115", "hash": "SHA-1", **self.cloud_info(c)})
            for x in dup_local:
                which = "SHA-1" if x.sha1 == sha1 else "MD5"
                root = self._local_roots.get(x.mapping_id)
                matches.append({"where": "NAS", "hash": which, "rel_path": x.rel_path, "mapping_id": x.mapping_id,
                                "path": local_join(root, x.rel_path) if root else x.rel_path,
                                "size": x.size, "sha1": x.sha1, "md5": x.md5})
            for s in dup_md5:
                root = self._roots.get(s.mapping_id)
                matches.append({"where": "115(已同步紀錄)", "hash": "MD5", "rel_path": s.rel_path,
                                "path": cloud_join(root, s.rel_path) if root is not None else s.rel_path,
                                "mapping_id": s.mapping_id, "fid": s.cloud_fid, "sha1": s.cloud_sha1})
            move = self._move_candidate(m, dup_cloud)
            return Outcome("review", review=ReviewSpec(DUPLICATE, lf.rel_path, self._details(
                m, lf, f"新檔案的內容與其他 {len(matches)} 個檔案相同（MD5/SHA-1）。", [], matches=matches,
                move_candidate=move)))

        if self.settings.review_same_name_elsewhere:
            same_name = [c for c in self.db.cloud_by_name(m.id, lf.name) if c.rel_key != key]
            if same_name and not self.db.is_ignored(m.id, key, SAME_NAME):
                return self._review(SAME_NAME, m, lf, "115 上其他資料夾已有同檔名的檔案（內容不同）。", same_name)

        return Outcome("upload", upload=UploadTask(m.id, lf.rel_path, "new", [], None, lf.size))

    def _review(self, kind: str, m: Mapping, lf: LocalRow, message: str, cloud: list[CloudRow] = ()) -> Outcome:
        return Outcome("review", review=ReviewSpec(kind, lf.rel_path, self._details(m, lf, message, list(cloud))))

    def _move_candidate(self, m: Mapping, dup_cloud: list[CloudRow]) -> dict | None:
        """同內容的 115 檔案若對應的 NAS 檔已不存在，代表檔案在 NAS 上被搬移/改名。"""
        for c in dup_cloud:
            if c.mapping_id != m.id:
                continue
            st = self.db.get_state(m.id, c.rel_key)
            if st and st.cloud_fid == c.fid and self.db.local_at(m.id, c.rel_key) is None:
                return {"fid": c.fid, "from_rel": c.rel_path}
        return None

    # ------------------------------------------------------------ 整個對應
    def plan_mapping(self, m: Mapping, local_dirs: list[str] | None = None,
                     unreadable_dirs: list[str] = ()) -> Plan:
        """unreadable_dirs：掃描時無法讀取的資料夾，其下檔案不可判定為「已刪除」。"""
        plan = Plan(m.id)
        local_rows = self.db.local_files(m.id)
        local_keys = {r.rel_key for r in local_rows}
        pending_groups: dict[str, list[LocalRow]] = {}

        for lf in local_rows:
            out = self.classify(m, lf)
            if out.kind == "skip":
                plan.skipped += 1
            elif out.kind == "unreadable":
                plan.unreadable.append(lf.rel_path)
            elif out.kind == "group_pending":
                pending_groups.setdefault(out.group, []).append(lf)
            elif out.kind in ("identical", "in_sync"):
                plan.identical += 1
                if out.link:
                    plan.links.append(out.link)
            elif out.kind == "upload":
                plan.uploads.append(out.upload)
            if out.review:
                plan.reviews.append(out.review)

        for group, rows in sorted(pending_groups.items()):
            self.db.upsert_group(m.id, group)
            plan.reviews.append(self.group_review(m, group, rows))

        self._plan_deletions(m, plan, local_keys, [norm_key(d) for d in unreadable_dirs])

        cloud_dirs = self.db.cloud_dirs(m.id)
        for d in sorted(local_dirs or [], key=lambda x: x.count("/")):
            if norm_key(d) in cloud_dirs or self.ignore.match_rel(d) or self.db.is_ignored(m.id, norm_key(d)):
                continue
            # 以資料夾內的虛擬檔名判斷此資料夾是否屬於存檔群組
            group, status = self.savedata_status(m, d + "/x")
            if group is not None and status != "approved" and status != "normal":
                continue
            plan.mkdirs.append(d)

        states = self.db.states(m.id)
        for c in self.db.cloud_files(m.id):
            if c.rel_key not in local_keys and c.rel_key not in states:
                plan.cloud_only.append(c)
        return plan

    def group_review(self, m: Mapping, group: str, rows: list[LocalRow]) -> ReviewSpec:
        return ReviewSpec(SAVEDATA_GROUP, group, {
            "message": "偵測到遊戲存檔資料夾，請核對是否以「Steam 雲存檔」方式自動同步。",
            "local_root": m.local_path, "cloud_root": m.cloud_path,
            "local_dir": local_join(m.local_path, group), "cloud_dir": cloud_join(m.cloud_path, group),
            "file_count": len(rows), "total_size": sum(r.size for r in rows),
            "samples": [r.rel_path for r in rows[:20]],
        }, rel_key=norm_key(group))

    def _plan_deletions(self, m: Mapping, plan: Plan, local_keys: set[str], protected: list[str]) -> None:
        states = self.db.states(m.id)
        deleted = []
        for key, st in states.items():
            if key in local_keys or self.ignore.match_rel(st.rel_path) or self.db.is_ignored(m.id, key):
                continue
            if any(key.startswith(p + "/") for p in protected):
                continue
            base = [c for c in self.db.cloud_at(m.id, key) if c.sha1 == st.cloud_sha1]
            if not base:
                plan.forgets.append(key)  # 兩邊都已不存在
                continue
            group, status = self.savedata_status(m, st.rel_path)
            if group is not None and status in (None, "pending", "exclude"):
                continue
            deleted.append((st, base, status == "approved"))

        if not deleted:
            return
        s = self.settings
        if len(deleted) >= s.mass_delete_guard_min and len(deleted) >= s.mass_delete_guard_ratio * len(states):
            review = self.mass_delete_review(m, [st.rel_path for st, _, _ in deleted], "*", len(states))
            plan.warnings.append(review.details["message"])
            plan.reviews.append(review)
            return
        for st, base, saved in deleted:
            if (saved and s.savedata_mirror_deletes) or s.deletion_policy == "auto":
                plan.deletes.append(DeleteTask(m.id, st.rel_path, [c.fid for c in base], base[0].pid,
                                               "savedata" if saved else "auto"))
            elif s.deletion_policy == "ignore":
                plan.forgets.append(st.rel_key)
            else:
                plan.reviews.append(self.local_deleted_review(m, st, base))

    def mass_delete_review(self, m: Mapping, paths: list[str], rel_key: str, total: int) -> ReviewSpec:
        msg = (f"NAS 上有 {len(paths)} 個已同步檔案不見了（占 {len(paths) * 100 // max(1, total)}%），"
               "可能是 NAS 離線或資料夾被移動。已暫停自動刪除，請本人確認。")
        return ReviewSpec(MASS_DELETE, paths[0] if rel_key != "*" and paths else "", {
            "message": msg, "local_root": m.local_path, "cloud_root": m.cloud_path,
            "count": len(paths), "paths": paths,
        }, rel_key=rel_key)

    def local_deleted_review(self, m: Mapping, st: StateRow, base: list[CloudRow]) -> ReviewSpec:
        return ReviewSpec(LOCAL_DELETED, st.rel_path, {
            "message": "NAS 上的檔案已刪除，115 上仍有同步過的版本。",
            "local_root": m.local_path, "cloud_root": m.cloud_path,
            "local": {"path": local_join(m.local_path, st.rel_path), "rel_path": st.rel_path,
                      "size": st.local_size, "md5": st.local_md5, "sha1": st.local_sha1},
            "cloud": [self.cloud_info(c) for c in base],
        })
