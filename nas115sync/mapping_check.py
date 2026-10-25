"""路徑對應的核對資訊與自動建議（GUI 與指令列共用）。程式只「建議」，一律由本人確認後才同步。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .cloud.base import CloudClient, CloudError
from .config import Mapping
from .paths import cloud_join, long_path, name_key

SAMPLE = 30


@dataclass
class MappingInspection:
    local_ok: bool = False
    local_error: str = ""
    local_dirs: list[str] = field(default_factory=list)
    local_files: list[str] = field(default_factory=list)
    cloud_cid: str | None = None
    cloud_error: str = ""
    cloud_dirs: list[str] = field(default_factory=list)
    cloud_files: list[str] = field(default_factory=list)
    common: list[str] = field(default_factory=list)  # 兩邊第一層同名項目
    overlaps: list[str] = field(default_factory=list)  # 與其他對應重疊的警告

    def lines(self, m: Mapping) -> list[str]:
        out = [f"NAS 路徑：{m.local_path}"]
        if self.local_ok:
            out.append(f"  ✔ 可存取；第一層有 {len(self.local_dirs)} 個資料夾、{len(self.local_files)} 個檔案")
            out += [f"    📁 {d}" for d in self.local_dirs[:SAMPLE]]
            out += [f"    📄 {f}" for f in self.local_files[:10]]
        else:
            out.append(f"  ✘ 無法存取：{self.local_error}")
        out.append(f"115 路徑：{m.cloud_path}")
        if self.cloud_cid is not None:
            out.append(f"  ✔ 已存在（cid={self.cloud_cid}）；第一層有 {len(self.cloud_dirs)} 個資料夾、"
                       f"{len(self.cloud_files)} 個檔案")
            out += [f"    📁 {d}" for d in self.cloud_dirs[:SAMPLE]]
            out += [f"    📄 {f}" for f in self.cloud_files[:10]]
        else:
            out.append(f"  ⚠ {self.cloud_error or '115 上尚無此資料夾（確認時可建立）'}")
        if self.common:
            out.append(f"兩邊第一層同名的項目：{len(self.common)} 個（{', '.join(self.common[:10])}）")
        out += [f"⚠ {w}" for w in self.overlaps]
        return out


def _norm_local(p: str) -> str:
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def inspect_mapping(m: Mapping, cloud: CloudClient, others: list[Mapping] = ()) -> MappingInspection:
    r = MappingInspection()
    try:
        with os.scandir(long_path(m.local_path)) as it:
            for e in it:
                (r.local_dirs if e.is_dir(follow_symlinks=False) else r.local_files).append(e.name)
        r.local_ok = True
    except OSError as e:
        r.local_error = str(e)
    try:
        r.cloud_cid = cloud.resolve_path(m.cloud_path)
        if r.cloud_cid is not None:
            for e in cloud.list_dir(r.cloud_cid):
                (r.cloud_dirs if e.is_dir else r.cloud_files).append(e.name)
    except CloudError as e:
        r.cloud_cid = None
        r.cloud_error = f"無法讀取 115：{e}"
    local_names = {name_key(n) for n in r.local_dirs + r.local_files}
    r.common = sorted(n for n in r.cloud_dirs + r.cloud_files if name_key(n) in local_names)
    r.local_dirs.sort()
    r.local_files.sort()
    mine = _norm_local(m.local_path)
    for o in others:
        if o.id == m.id:
            continue
        theirs = _norm_local(o.local_path)
        if mine == theirs or mine.startswith(theirs + os.sep) or theirs.startswith(mine + os.sep):
            r.overlaps.append(f"NAS 路徑與另一個對應重疊：{o.local_path}")
        a, b = name_key(m.cloud_path).rstrip("/"), name_key(o.cloud_path).rstrip("/")
        if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
            r.overlaps.append(f"115 路徑與另一個對應重疊：{o.cloud_path}")
    return r


@dataclass
class Suggestion:
    local_path: str
    cloud_path: str
    cloud_exists: bool


def suggest_mappings(nas_root: str, cloud_root: str, cloud: CloudClient) -> list[Suggestion]:
    """把 NAS 根目錄下每個資料夾配對到 115 根目錄下同名資料夾（沒有則建議新建同名資料夾）。"""
    with os.scandir(long_path(nas_root)) as it:
        local = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
    cloud_names: dict[str, str] = {}
    cid = cloud.resolve_path(cloud_root)
    if cid is not None:
        cloud_names = {name_key(e.name): e.name for e in cloud.list_dir(cid) if e.is_dir}
    out = []
    for name in local:
        existing = cloud_names.get(name_key(name))
        out.append(Suggestion(os.path.join(nas_root, name), cloud_join(cloud_root, existing or name),
                              existing is not None))
    return out
