"""比對邏輯：以「相對路徑（檔名）＋ SHA-1 ＋ MD5 ＋ 同步基準」三方比對，決定每個檔案要自動處理或交給本人核對。

每個檔案同時看三樣東西：NAS 現況、115 現況、上次同步完成時的「同步基準」（兩邊當時的 SHA-1）。
有基準就能分辨「哪一邊改過」，不必像只看修改時間的工具那樣猜測。

對應的同步方向：
- upload  （NAS → 115）：只把 NAS 的新增/修改/刪除反映到 115
- download（115 → NAS）：只把 115 的新增/修改/刪除反映到 NAS
- two_way （雙向）     ：哪一邊改就同步到另一邊；兩邊都改 → 本人核對

需要本人核對的情況：同檔名不同內容且無基準、兩邊都改過、相同 MD5/SHA-1 出現在別處、
同檔名在其他資料夾、遊戲存檔群組第一次出現、刪除（預設）、大量刪除（一律）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import DOWNLOAD, TWO_WAY, UPLOAD, Mapping, Settings
from .db import CloudRow, Database, LocalRow, StateRow
from .paths import IgnoreMatcher, MappingFilter, cloud_join, local_join, norm_key, rel_parent
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
    MASS_DELETE: "大量刪除（疑似離線）",
    SAVEDATA_GROUP: "遊戲存檔群組",
    ERROR: "傳輸失敗",
}

_SKIP = ("skip", "暫時略過", frozenset())
_IGNORE = ("ignore", "永久忽略此檔", frozenset())
_UP, _DOWN = frozenset({"up"}), frozenset({"down"})

# (決定, 說明, 需要的條件)；條件：up=對應可上傳 down=對應可下載 local/cloud=新檔在哪一邊 move=可視為搬移
_DECISIONS: dict[str, list[tuple[str, str, frozenset]]] = {
    CONFLICT: [("upload_replace", "以 NAS 版覆蓋 115", _UP),
               ("upload_keep_both", "上傳 NAS 版，115 舊版改名保留", _UP),
               ("download_replace", "以 115 版覆蓋 NAS（NAS 舊版可復原）", _DOWN),
               ("download_keep_both", "下載 115 版，NAS 舊版改名保留", _DOWN),
               ("keep_cloud", "維持兩邊現狀（之後有變動才同步）", frozenset()), _SKIP, _IGNORE],
    CLOUD_CHANGED: [("download_replace", "以 115 版覆蓋 NAS（NAS 舊版可復原）", _DOWN),
                    ("upload_replace", "以 NAS 版覆蓋 115", _UP),
                    ("keep_cloud", "接受 115 版（之後有變動才同步）", frozenset()), _SKIP, _IGNORE],
    CLOUD_DELETED: [("delete_local", "刪除 NAS 檔案（移到可復原資料夾）", _DOWN),
                    ("upload", "重新上傳到 115", _UP),
                    ("forget", "不再同步此檔", frozenset()), _SKIP],
    DUPLICATE: [("upload", "仍然上傳（會觸發秒傳）", frozenset({"up", "local"})),
                ("move_cloud", "視為搬移：在 115 搬移原檔", frozenset({"up", "local", "move"})),
                ("download", "仍然下載（優先從 NAS 複製相同內容）", frozenset({"down", "cloud"})), _SKIP, _IGNORE],
    SAME_NAME: [("upload", "上傳", _UP), _SKIP, _IGNORE],
    CLOUD_DUPS: [("delete_extra", "刪除 115 多餘的同名檔", frozenset()), ("keep", "保留（不再提示）", frozenset())],
    LOCAL_DELETED: [("delete_cloud", "刪除 115 上的檔案（進 115 回收站）", _UP),
                    ("download", "從 115 下載還原到 NAS", _DOWN),
                    ("keep_cloud", "保留 115 上的檔案（不再同步此檔）", frozenset())],
    MASS_DELETE: [("delete_all", "確認全部刪除（可復原）", frozenset()), ("keep_all", "全部保留", frozenset())],
    SAVEDATA_GROUP: [("approve", "核可：像 Steam 雲存檔自動同步", frozenset()),
                     ("normal", "當一般檔案處理", frozenset()), ("exclude", "不同步此資料夾", frozenset())],
    ERROR: [("retry", "重試", frozenset()), _SKIP],
}

# 給介面列出所有種類用
DECISIONS = {k: [(d, label) for d, label, _ in v] for k, v in _DECISIONS.items()}


def available_decisions(kind: str, details: dict) -> list[tuple[str, str]]:
    direction = details.get("direction", UPLOAD)
    tags = {details.get("side", "local")}
    if direction in (UPLOAD, TWO_WAY):
        tags.add("up")
    if direction in (DOWNLOAD, TWO_WAY):
        tags.add("down")
    if details.get("move_candidate"):
        tags.add("move")
    return [(d, label) for d, label, req in _DECISIONS.get(kind, []) if req <= tags]


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
    # 被取代的 115 版本從未出現在 NAS 上（先前選擇「維持兩邊現狀」）：改名保留，不刪除
    keep_old: bool = False


@dataclass
class DownloadTask:
    mapping_id: str
    rel_path: str
    fid: str
    pick_code: str
    sha1: str
    size: int
    reason: str  # new | modified | savedata | review | restore
    replace_local: bool = False
    expected_local_sha1: str = ""  # 覆蓋前確認 NAS 檔仍是這個內容（避免蓋掉剛改的檔案）
    savedata_group: str | None = None
    # 被取代的 NAS 版本從未出現在 115 上：在原處改名保留，不移到可復原資料夾
    keep_old: bool = False


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
class LocalDeleteTask:
    mapping_id: str
    rel_path: str
    expected_sha1: str
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
    # skip | unreadable | group_pending | identical | in_sync | upload | download | review |
    # local_gone（NAS 刪除）| cloud_gone（115 刪除）| forget | local_only | cloud_only
    kind: str
    upload: UploadTask | None = None
    download: DownloadTask | None = None
    link: LinkTask | None = None
    review: ReviewSpec | None = None
    group: str | None = None
    state: StateRow | None = None
    base: list[CloudRow] = field(default_factory=list)
    local: LocalRow | None = None
    saved: bool = False
    item: object = None  # group_pending 時的檔案（LocalRow 或 CloudRow）


@dataclass
class Plan:
    mapping_id: str
    uploads: list[UploadTask] = field(default_factory=list)
    downloads: list[DownloadTask] = field(default_factory=list)
    links: list[LinkTask] = field(default_factory=list)
    deletes: list[DeleteTask] = field(default_factory=list)
    local_deletes: list[LocalDeleteTask] = field(default_factory=list)
    forgets: list[str] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)
    local_mkdirs: list[str] = field(default_factory=list)
    reviews: list[ReviewSpec] = field(default_factory=list)
    cloud_only: list[CloudRow] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    identical: int = 0
    skipped: int = 0
    unreadable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "uploads": len(self.uploads),
            "upload_bytes": sum(u.size for u in self.uploads),
            "downloads": len(self.downloads),
            "download_bytes": sum(d.size for d in self.downloads),
            "links": len(self.links),
            "identical": self.identical,
            "deletes": len(self.deletes),
            "local_deletes": len(self.local_deletes),
            "mkdirs": len(self.mkdirs) + len(self.local_mkdirs),
            "reviews": len(self.reviews),
            "cloud_only": len(self.cloud_only),
            "local_only": len(self.local_only),
            "skipped": self.skipped,
            "unreadable": len(self.unreadable),
        }


class Planner:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.detector = SavedataDetector(settings.savedata_dir_names, settings.savedata_extensions)
        self.ignore = IgnoreMatcher(list(settings.ignore_patterns)
                                    + [settings.savedata_versions_dir, settings.local_trash_dir, "*.115part"])
        self._roots: dict[str, str] = {}
        self._local_roots: dict[str, str] = {}
        self._filters: dict[str, MappingFilter] = {}

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
        """讓核對畫面能顯示其他對應中檔案的完整路徑。"""
        self._roots = {m.id: m.cloud_path for m in mappings}
        self._local_roots = {m.id: m.local_path for m in mappings}

    def _base_details(self, m: Mapping, message: str, side: str = "local", **extra) -> dict:
        d = {"message": message, "local_root": m.local_path, "cloud_root": m.cloud_path,
             "direction": m.direction, "side": side}
        d.update(extra)
        return d

    def _details(self, m: Mapping, lf: LocalRow | None, message: str, cloud: list[CloudRow] = (),
                 side: str = "local", **extra) -> dict:
        d = self._base_details(m, message, side, **extra)
        if lf is not None:
            d["local"] = self.local_info(m, lf)
        d["cloud"] = [self.cloud_info(c) for c in cloud]
        return d

    def filter_for(self, m: Mapping) -> MappingFilter:
        f = self._filters.get(m.id)
        if f is None or not f.same_as(m):
            f = self._filters[m.id] = MappingFilter(m)
        return f

    def is_excluded(self, m: Mapping, rel_path: str, size: int | None = None) -> bool:
        return (self.ignore.match_rel(rel_path) or self.db.is_ignored(m.id, norm_key(rel_path))
                or not self.filter_for(m).accepts_file(rel_path, size))

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
        """以 NAS 檔案為起點分類（即時同步用）。"""
        return self.classify_key(m, lf, self.db.cloud_at(m.id, lf.rel_key), self.db.get_state(m.id, lf.rel_key))

    def classify_rel(self, m: Mapping, rel_path: str) -> Outcome:
        key = norm_key(rel_path)
        return self.classify_key(m, self.db.local_at(m.id, key), self.db.cloud_at(m.id, key),
                                 self.db.get_state(m.id, key))

    def classify_key(self, m: Mapping, lf: LocalRow | None, cloud: list[CloudRow], state: StateRow | None,
                     protected: bool = False) -> Outcome:
        cloud = sorted(cloud, key=lambda c: c.mtime, reverse=True)
        rel = lf.rel_path if lf else cloud[0].rel_path if cloud else state.rel_path if state else ""
        if not rel:
            return Outcome("skip")
        size = lf.size if lf else cloud[0].size if cloud else state.local_size
        if self.is_excluded(m, rel, size):
            return Outcome("skip")
        if lf is not None and not lf.sha1:
            return Outcome("unreadable")
        if protected and lf is None:
            return Outcome("skip")  # NAS 該資料夾暫時讀不到，不可當成刪除或缺檔

        group, status = self.savedata_status(m, rel)
        saved = False
        if group is not None:
            if status in (None, "pending"):
                return Outcome("group_pending", group=group, item=lf or (cloud[0] if cloud else None))
            if status == "exclude":
                return Outcome("skip")
            saved = status == "approved"
            if not saved:
                group = None
        auto = saved or self.settings.auto_update_linked

        if lf is not None and cloud:
            return self._both(m, lf, cloud, state, group, saved, auto)
        if lf is not None:
            return self._local_only(m, lf, state, group, saved)
        if cloud:
            return self._cloud_only(m, cloud, state, group, saved)
        return Outcome("forget", state=state)

    def _both(self, m, lf, cloud, state, group, saved, auto) -> Outcome:
        same = [c for c in cloud if c.sha1 == lf.sha1]
        if same:
            target = next((c for c in same if state and c.fid == state.cloud_fid), same[0])
            others = [c for c in cloud if c.fid != target.fid]
            review = None
            if others and not self.db.is_ignored(m.id, lf.rel_key, CLOUD_DUPS):
                review = ReviewSpec(CLOUD_DUPS, lf.rel_path, self._details(
                    m, lf, f"115 同一資料夾內有 {len(cloud)} 個同名檔，其中一個與 NAS 相同。", cloud,
                    keep_fid=target.fid, extra_fids=[c.fid for c in others]))
            linked = (state and state.local_sha1 == lf.sha1 and state.cloud_fid == target.fid
                      and state.cloud_sha1 == lf.sha1)
            return Outcome("identical", link=None if linked else LinkTask(m.id, lf.rel_path, lf, target),
                           review=review, local=lf)

        if not state:
            return self._review(CONFLICT, m, lf, "相同檔名但 SHA-1 不同（尚未建立同步基準）。", cloud)
        base_cloud = [c for c in cloud if c.sha1 == state.cloud_sha1]
        local_changed = state.local_sha1 != lf.sha1
        cloud_changed = not base_cloud
        divergent = state.local_sha1 != state.cloud_sha1  # 基準時兩邊就不同：被取代的一邊是獨一無二的內容
        if not local_changed and not cloud_changed:
            return Outcome("in_sync", local=lf)  # 先前選擇「維持兩邊現狀」
        if local_changed and not cloud_changed:
            if m.can_upload and auto:
                return Outcome("upload", upload=UploadTask(m.id, lf.rel_path, "savedata" if saved else "modified",
                                                           [c.fid for c in base_cloud], group, lf.size,
                                                           keep_old=divergent))
            msg = ("已連結的檔案在 NAS 端被修改（設定為需本人核對）。" if m.can_upload
                   else "此對應為「115 → NAS」，但 NAS 上的檔案在上次同步後被修改。")
            return self._review(CONFLICT, m, lf, msg, cloud)
        if cloud_changed and not local_changed:
            newest = cloud[0]
            if m.can_download and auto:
                return Outcome("download", download=DownloadTask(
                    m.id, lf.rel_path, newest.fid, newest.pick_code, newest.sha1, newest.size,
                    "savedata" if saved else "modified", replace_local=True, expected_local_sha1=lf.sha1,
                    savedata_group=group, keep_old=divergent))
            return self._review(CLOUD_CHANGED, m, lf, "115 上的檔案在上次同步後被修改，NAS 版本未變。", cloud)
        return self._review(CONFLICT, m, lf, "NAS 與 115 兩邊在上次同步後都被修改。", cloud)

    def _local_only(self, m, lf, state, group, saved) -> Outcome:
        key = lf.rel_key
        if state:
            if m.can_download and state.local_sha1 == lf.sha1 and state.local_sha1 == state.cloud_sha1:
                return Outcome("cloud_gone", state=state, local=lf, saved=saved)
            msg = ("上次同步後，115 上的這個檔案已不存在（NAS 版本也被修改過）。" if state.local_sha1 != lf.sha1
                   else "上次同步後，115 上的這個檔案已不存在。")
            return self._review(CLOUD_DELETED, m, lf, msg)
        if not m.can_upload:
            return Outcome("local_only")
        if saved:
            return Outcome("upload", upload=UploadTask(m.id, lf.rel_path, "savedata", [], group, lf.size))

        dup_cloud = [c for c in self.db.cloud_by_sha1(lf.sha1) if not (c.mapping_id == m.id and c.rel_key == key)]
        dup_local = [x for x in self.db.local_by_hash(lf.sha1, lf.md5)
                     if not (x.mapping_id == m.id and x.rel_key == key)]
        dup_md5 = [s for s in self.db.states_by_md5(lf.md5) if not (s.mapping_id == m.id and s.rel_key == key)]
        if (dup_cloud or dup_local or dup_md5) and not self.db.is_ignored(m.id, key, DUPLICATE):
            matches = self._matches(lf.sha1, dup_cloud, dup_local, dup_md5)
            return Outcome("review", review=ReviewSpec(DUPLICATE, lf.rel_path, self._details(
                m, lf, f"新檔案的內容與其他 {len(matches)} 個檔案相同（MD5/SHA-1）。", [], matches=matches,
                move_candidate=self._move_candidate(m, dup_cloud))))

        if self.settings.review_same_name_elsewhere:
            same_name = [c for c in self.db.cloud_by_name(m.id, lf.name) if c.rel_key != key]
            if same_name and not self.db.is_ignored(m.id, key, SAME_NAME):
                return self._review(SAME_NAME, m, lf, "115 上其他資料夾已有同檔名的檔案（內容不同）。", same_name)

        return Outcome("upload", upload=UploadTask(m.id, lf.rel_path, "new", [], None, lf.size))

    def _cloud_only(self, m, cloud, state, group, saved) -> Outcome:
        c = cloud[0]
        if state:
            base = [x for x in cloud if x.sha1 == state.cloud_sha1]
            # 基準時兩邊不同：115 版本從未出現在 NAS 上，不可自動刪除，交給本人核對
            if base and m.can_upload and state.local_sha1 == state.cloud_sha1:
                return Outcome("local_gone", state=state, base=base, saved=saved)
            msg = ("NAS 上的檔案已刪除，115 上的版本也在上次同步後被修改。" if not base
                   else "NAS 上的檔案已刪除（此對應為「115 → NAS」）。")
            return Outcome("review", review=ReviewSpec(LOCAL_DELETED, c.rel_path, self._details(
                m, None, msg, cloud, local={"path": local_join(m.local_path, state.rel_path),
                                            "rel_path": state.rel_path, "size": state.local_size,
                                            "md5": state.local_md5, "sha1": state.local_sha1})))
        if not m.can_download:
            return Outcome("cloud_only")
        task = DownloadTask(m.id, c.rel_path, c.fid, c.pick_code, c.sha1, c.size, "savedata" if saved else "new",
                            savedata_group=group)
        if saved:
            return Outcome("download", download=task)
        dup_local = [x for x in self.db.local_by_hash(c.sha1) if not (x.mapping_id == m.id and x.rel_key == c.rel_key)]
        if dup_local and not self.db.is_ignored(m.id, c.rel_key, DUPLICATE):
            matches = self._matches(c.sha1, [], dup_local, [])
            return Outcome("review", review=ReviewSpec(DUPLICATE, c.rel_path, self._details(
                m, None, f"115 上的新檔案與 NAS 上 {len(matches)} 個檔案內容相同（SHA-1）。", cloud, side="cloud",
                matches=matches)))
        return Outcome("download", download=task)

    def _matches(self, sha1, dup_cloud, dup_local, dup_md5) -> list[dict]:
        matches = [{"where": "115", "hash": "SHA-1", **self.cloud_info(c)} for c in dup_cloud]
        for x in dup_local:
            root = self._local_roots.get(x.mapping_id)
            matches.append({"where": "NAS", "hash": "SHA-1" if x.sha1 == sha1 else "MD5", "rel_path": x.rel_path,
                            "mapping_id": x.mapping_id, "path": local_join(root, x.rel_path) if root else x.rel_path,
                            "size": x.size, "sha1": x.sha1, "md5": x.md5})
        for s in dup_md5:
            root = self._roots.get(s.mapping_id)
            matches.append({"where": "115(已同步紀錄)", "hash": "MD5", "rel_path": s.rel_path,
                            "path": cloud_join(root, s.rel_path) if root is not None else s.rel_path,
                            "mapping_id": s.mapping_id, "fid": s.cloud_fid, "sha1": s.cloud_sha1})
        return matches

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
    def plan_mapping(self, m: Mapping, local_dirs: list[str] | None = None, unreadable_dirs: list[str] = (),
                     hidden_keys: set[str] = frozenset()) -> Plan:
        """unreadable_dirs：掃描時無法讀取的資料夾；hidden_keys：因「略過隱藏檔」沒有掃描的檔案。
        這兩者底下的檔案都不可判定為「已刪除」。"""
        plan = Plan(m.id)
        local = {r.rel_key: r for r in self.db.local_files(m.id)}
        cloud: dict[str, list[CloudRow]] = {}
        for c in self.db.cloud_files(m.id):
            cloud.setdefault(c.rel_key, []).append(c)
        states = self.db.states(m.id)
        protected = [norm_key(d) for d in unreadable_dirs]
        pending_groups: dict[str, list] = {}
        gone_local, gone_cloud = [], []

        for key in sorted(set(local) | set(cloud) | set(states)):
            if key in hidden_keys:
                plan.skipped += 1
                continue
            prot = any(key.startswith(p + "/") for p in protected)
            out = self.classify_key(m, local.get(key), cloud.get(key, []), states.get(key), prot)
            k = out.kind
            if k == "skip":
                plan.skipped += 1
            elif k == "unreadable":
                plan.unreadable.append(local[key].rel_path)
            elif k == "group_pending":
                pending_groups.setdefault(out.group, []).append(out.item)
            elif k in ("identical", "in_sync"):
                plan.identical += 1
                if out.link:
                    plan.links.append(out.link)
            elif k == "upload":
                plan.uploads.append(out.upload)
            elif k == "download":
                plan.downloads.append(out.download)
            elif k == "local_gone":
                gone_local.append(out)
            elif k == "cloud_gone":
                gone_cloud.append(out)
            elif k == "forget":
                plan.forgets.append(key)
            elif k == "cloud_only":
                plan.cloud_only.append(cloud[key][0])
            elif k == "local_only":
                plan.local_only.append(local[key].rel_path)
            if out.review:
                plan.reviews.append(out.review)

        for group, items in sorted(pending_groups.items()):
            self.db.upsert_group(m.id, group)
            plan.reviews.append(self.group_review(m, group, [i for i in items if i is not None]))

        self.resolve_deletions(m, plan, gone_local, "local", len(states))
        self.resolve_deletions(m, plan, gone_cloud, "cloud", len(states))
        self._plan_mkdirs(m, plan, local_dirs or [])
        return plan

    def _dir_allowed(self, m: Mapping, d: str) -> bool:
        filt = self.filter_for(m)
        if self.ignore.match_rel(d) or self.db.is_ignored(m.id, norm_key(d)) or filt.excludes_dir(d):
            return False
        if filt.skip_hidden and any(part.startswith(".") for part in d.split("/")):
            return False
        # 以資料夾內的虛擬檔名判斷此資料夾是否屬於尚未核可的存檔群組
        group, status = self.savedata_status(m, d + "/x")
        return group is None or status in ("approved", "normal")

    def _plan_mkdirs(self, m: Mapping, plan: Plan, local_dirs: list[str]) -> None:
        cloud_dirs = self.db.cloud_dirs(m.id)
        local_keys = {norm_key(d) for d in local_dirs}
        if m.can_upload:
            for d in sorted(local_dirs, key=lambda x: x.count("/")):
                if norm_key(d) not in cloud_dirs and self._dir_allowed(m, d):
                    plan.mkdirs.append(d)
        if m.can_download:
            for key, (rel, _) in sorted(cloud_dirs.items(), key=lambda kv: kv[0].count("/")):
                if key and key not in local_keys and self._dir_allowed(m, rel):
                    plan.local_mkdirs.append(rel)

    def group_review(self, m: Mapping, group: str, items: list) -> ReviewSpec:
        return ReviewSpec(SAVEDATA_GROUP, group, self._base_details(
            m, "偵測到遊戲存檔資料夾，請核對是否以「Steam 雲存檔」方式自動同步。",
            local_dir=local_join(m.local_path, group), cloud_dir=cloud_join(m.cloud_path, group),
            file_count=len(items), total_size=sum(getattr(i, "size", 0) for i in items),
            samples=[i.rel_path for i in items[:20]]), rel_key=norm_key(group))

    def resolve_deletions(self, m: Mapping, plan: Plan, gone: list[Outcome], side: str, total: int) -> None:
        """side=local：NAS 刪除 → 是否刪除 115；side=cloud：115 刪除 → 是否刪除 NAS。"""
        if not gone:
            return
        s = self.settings
        if len(gone) >= s.mass_delete_guard_min and len(gone) >= s.mass_delete_guard_ratio * total:
            review = self.mass_delete_review(m, [o.state.rel_path for o in gone], f"*{side}", total, side)
            plan.warnings.append(review.details["message"])
            plan.reviews.append(review)
            return
        policy = m.delete_policy(s)
        for o in gone:
            st = o.state
            if (o.saved and s.savedata_mirror_deletes) or policy == "auto":
                reason = "savedata" if o.saved else "auto"
                if side == "local":
                    plan.deletes.append(DeleteTask(m.id, st.rel_path, [c.fid for c in o.base], o.base[0].pid, reason))
                else:
                    plan.local_deletes.append(LocalDeleteTask(m.id, st.rel_path, o.local.sha1, reason))
            elif policy == "ignore":
                plan.forgets.append(st.rel_key)
            elif side == "local":
                plan.reviews.append(self.local_deleted_review(m, st, o.base))
            else:
                plan.reviews.append(self._review(CLOUD_DELETED, m, o.local,
                                                 "上次同步後，115 上的這個檔案已被刪除，NAS 版本未變。").review)

    def mass_delete_review(self, m: Mapping, paths: list[str], rel_key: str, total: int,
                           side: str = "local") -> ReviewSpec:
        where, other = ("NAS", "115") if side == "local" else ("115", "NAS")
        msg = (f"{where} 上有 {len(paths)} 個已同步檔案不見了（占 {len(paths) * 100 // max(1, total)}%），"
               f"可能是{where}離線或資料夾被移動。已暫停自動刪除 {other} 上的檔案，請本人確認。")
        return ReviewSpec(MASS_DELETE, paths[0] if not rel_key.startswith("*") and paths else "",
                          self._base_details(m, msg, side, count=len(paths), paths=paths), rel_key=rel_key)

    def local_deleted_review(self, m: Mapping, st: StateRow, base: list[CloudRow]) -> ReviewSpec:
        return ReviewSpec(LOCAL_DELETED, st.rel_path, self._details(
            m, None, "NAS 上的檔案已刪除，115 上仍有同步過的版本。", base,
            local={"path": local_join(m.local_path, st.rel_path), "rel_path": st.rel_path,
                   "size": st.local_size, "md5": st.local_md5, "sha1": st.local_sha1},
            parent=rel_parent(st.rel_path)))
