import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nas115sync.cloud.fake import FakeCloud  # noqa: E402
from nas115sync.config import Config  # noqa: E402
from nas115sync.db import Database  # noqa: E402
from nas115sync.engine import SyncEngine  # noqa: E402

CLOUD_ROOT = "/備份/NAS"


class Env:
    def __init__(self, tmp_path: Path, rename_duplicates: bool = False):
        self.nas = tmp_path / "nas"
        self.nas.mkdir()
        self.config = Config(tmp_path / "config.json")
        self.config.settings.api_min_interval = 0
        self.config.settings.mass_delete_guard_min = 20
        self.db = Database(tmp_path / "state.db")
        self.cloud = FakeCloud(rename_duplicates=rename_duplicates)
        self.engine = SyncEngine(self.config, self.db, self.cloud)
        self.mapping = self.config.add_mapping(str(self.nas), CLOUD_ROOT)
        self._mtime = 1_700_000_000
        cid = self.cloud.ensure_path(CLOUD_ROOT)
        self.config.confirm_mapping(self.mapping.id, cid)

    def write(self, rel: str, content: bytes | str) -> Path:
        p = self.nas / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        p.write_bytes(data)
        self._mtime += 10  # 確保每次寫入的 mtime 都不同（不受檔案系統時間解析度影響）
        os.utime(p, (self._mtime, self._mtime))
        return p

    def cloud_files(self, rel_dir: str = "") -> dict:
        path = CLOUD_ROOT + ("/" + rel_dir if rel_dir else "")
        return self.cloud.files_in(path)

    def sync(self):
        plans = self.engine.analyze()
        stats = self.engine.execute(plans)
        return plans, stats

    def reviews(self, kind=None):
        return self.db.list_reviews("pending", kind=kind)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.db.close()


@pytest.fixture
def env_rename(tmp_path):
    e = Env(tmp_path, rename_duplicates=True)
    yield e
    e.db.close()
