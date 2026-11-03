import os
import time

from nas115sync import compare as C
from nas115sync.config import TWO_WAY
from nas115sync.hashing import hash_bytes
from nas115sync.watcher import RealtimeSync


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_nas_offline_while_watching_never_deletes_and_resumes(env):
    """即時同步中 NAS 斷線（或開機時網路磁碟尚未連上）：不會當成刪除；恢復連線後自動重新監看。"""
    s = env.config.settings
    s.stable_seconds, s.deletion_policy = 0.3, "auto"
    env.mapping.direction = TWO_WAY
    env.write("docs/a.txt", "A")
    env.sync()
    rt = RealtimeSync(env.engine)
    rt.start(initial_rescan=False)

    def watching():
        rt._next_watch_check = 0  # 立即重新檢查 NAS 是否可存取（平常每 30 秒一次）
        return env.mapping.id in rt._watches

    try:
        offline = env.nas.with_name("nas_offline")
        env.nas.rename(offline)
        assert wait_for(lambda: not watching())
        time.sleep(1.5)
        assert "a.txt" in env.cloud_files("docs") and not env.reviews(C.LOCAL_DELETED)
        offline.rename(env.nas)
        assert wait_for(watching)
        env.write("docs/b.txt", "B")
        assert wait_for(lambda: "b.txt" in env.cloud_files("docs"))
        assert "a.txt" in env.cloud_files("docs") and (env.nas / "docs/a.txt").read_text() == "A"
    finally:
        rt.stop()


def test_realtime_watcher_end_to_end(env):
    s = env.config.settings
    s.stable_seconds = 0.3
    s.savedata_quiet_seconds = 1
    env.write("Saves/slot.sav", "v1")
    env.sync()
    [rv] = env.reviews(C.SAVEDATA_GROUP)
    env.engine.apply_review(rv.id, "approve")

    rt = RealtimeSync(env.engine)
    rt.start(initial_rescan=False)
    try:
        env.write("docs/new.txt", "hello")
        assert wait_for(lambda: "new.txt" in env.cloud_files("docs"))

        env.write("docs/new.txt", "hello v2")
        assert wait_for(lambda: env.cloud_files("docs").get("new.txt") is not None
                        and env.cloud_files("docs")["new.txt"].sha1 == hash_bytes(b"hello v2").sha1)
        assert list(env.cloud_files("docs")) == ["new.txt"]

        env.write("Saves/slot.sav", "v2")
        assert wait_for(lambda: env.cloud_files("Saves")["slot.sav"].sha1 == hash_bytes(b"v2").sha1)
        assert list(env.cloud_files("Saves")) == ["slot.sav"]

        os.rename(env.nas / "docs/new.txt", env.nas / "docs/renamed.txt")
        assert wait_for(lambda: list(env.cloud_files("docs")) == ["renamed.txt"])

        os.remove(env.nas / "docs/renamed.txt")
        assert wait_for(lambda: len(env.reviews(C.LOCAL_DELETED)) == 1)
    finally:
        rt.stop()
