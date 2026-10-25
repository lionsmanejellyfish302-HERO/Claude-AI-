"""排程：在指定的星期與時間自動「完整比對」或「完整比對＋同步」（類似 Air Explorer Pro 的排程工作）。

即時同步已能處理大部分變動；排程適合每天深夜做一次完整驗證，補抓網路磁碟或 115 端漏掉的變化。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]


def new_schedule(time_hhmm: str = "03:00", days=None, action: str = "sync", mapping_ids=None) -> dict:
    return {"id": uuid.uuid4().hex[:8], "enabled": True, "time": time_hhmm,
            "days": list(range(7)) if days is None else list(days), "action": action,
            "mapping_ids": list(mapping_ids or [])}


def describe(s: dict) -> str:
    days = s.get("days", [])
    when = "每天" if len(days) == 7 else "每週" + "、".join(WEEKDAYS[d] for d in sorted(days))
    what = "完整比對＋同步" if s.get("action") == "sync" else "只比對"
    scope = f"{len(s['mapping_ids'])} 個對應" if s.get("mapping_ids") else "所有對應"
    return f"{when} {s.get('time')} {what}（{scope}）{'' if s.get('enabled', True) else '［停用］'}"


class Scheduler:
    def __init__(self, engine):
        self.engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def due(self, now: time.struct_time | None = None) -> list[dict]:
        now = now or time.localtime()
        hhmm = time.strftime("%H:%M", now)
        stamp = time.strftime("%Y%m%d", now) + hhmm
        out = []
        for s in self.engine.settings.schedules:
            if (s.get("enabled", True) and s.get("time") == hhmm and now.tm_wday in s.get("days", range(7))
                    and self._last.get(s["id"]) != stamp):
                self._last[s["id"]] = stamp
                out.append(s)
        return out

    def _loop(self) -> None:
        while not self._stop.wait(20):
            for s in self.due():
                threading.Thread(target=self.run_schedule, args=(s,), name="schedule-run", daemon=True).start()

    def run_schedule(self, s: dict) -> dict | None:
        eng = self.engine
        ms = [m for m in eng.config.active_mappings() if not s.get("mapping_ids") or m.id in s["mapping_ids"]]
        if not ms:
            return None
        if eng.full_lock.locked():
            log.info("排程略過（另一個完整比對進行中）：%s", describe(s))
            return None
        log.info("排程開始：%s", describe(s))
        try:
            plans = eng.analyze(ms)
            stats = eng.execute(plans) if s.get("action") == "sync" else None
            eng.db.add_history("", "schedule", describe(s), True, detail=str(stats or "只比對"))
            eng.emit("schedule_done", schedule=s, stats=stats)
            return stats
        except Exception as e:  # noqa: BLE001
            log.exception("排程失敗：%s", describe(s))
            eng.db.add_history("", "schedule", describe(s), False, detail=str(e)[:500])
            return None
