import os
import time

from nas115sync import compare as C
from nas115sync.hashing import hash_bytes
from nas115sync.watcher import RealtimeSync


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


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
